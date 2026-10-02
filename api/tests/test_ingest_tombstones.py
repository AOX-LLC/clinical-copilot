"""A record deleted at the source is tombstoned only by a complete read, and can come back."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crypto.runtime import FieldCrypto
from app.ingest import run as ingest_run
from app.ingest.run import run_ingest
from app.timeline.ingest import tombstone_head
from tests.dataset import patient_resources
from tests.dataset_server import DatasetFhirTransport
from tests.ingest_support import (
    adapter_over,
    app_role_engine,
    new_key_material,
    real_crypto,
    table_digests,
)

pytestmark = pytest.mark.db

CLINIC_ZONE = ZoneInfo("America/New_York")
SLICE = 3
SECOND_PATIENT = "0b879479-3d66-a073-a9d5-8ba6677556e2"
ONE_MEDICATION = ("MedicationRequest", "0b879479-3d66-a073-11a1-e749dce77bfb")


def _ids_of(resource_type: str, patient_id: str) -> set[tuple[str, str]]:
    for resources in patient_resources():
        if resources[0]["id"] == patient_id:
            return {
                (r["resourceType"], r["id"])
                for r in resources
                if r["resourceType"] == resource_type
            }
    raise AssertionError("patient not in the dataset")


async def _ingest(url: str, crypto: FieldCrypto, transport: DatasetFhirTransport) -> Any:
    adapter, client = adapter_over(transport)
    try:
        async with app_role_engine(url) as app_engine:
            return await run_ingest(app_engine, adapter, crypto, CLINIC_ZONE, 4)
    finally:
        await client.aclose()


def _source(**kwargs: Any) -> DatasetFhirTransport:
    return DatasetFhirTransport(patient_limit=SLICE, **kwargs)


async def _scalar(engine: AsyncEngine, statement: str, **parameters: object) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(statement), parameters)


async def _tombstoned(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT resource_type, resource_id FROM source_resource_head"
                " WHERE deleted_at IS NOT NULL ORDER BY 1, 2"
            )
        )
        return [(row.resource_type, row.resource_id) for row in rows]


async def _current_rows_of(engine: AsyncEngine, resource_type: str, resource_id: str) -> int:
    return int(
        await _scalar(
            engine,
            "SELECT count(*) FROM timeline_event t JOIN source_resource_head h"
            " ON h.source_record_id = t.source_record_id"
            " WHERE h.resource_type = :t AND h.resource_id = :i AND t.superseded_at IS NULL",
            t=resource_type,
            i=resource_id,
        )
    )


async def test_a_record_the_source_no_longer_returns_is_tombstoned(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    snapshots = await _scalar(engine, "SELECT count(*) FROM source_record")
    assert await _current_rows_of(engine, *ONE_MEDICATION) == 1

    second = await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))

    assert second.tombstoned == 1
    assert await _tombstoned(engine) == [ONE_MEDICATION]
    assert await _current_rows_of(engine, *ONE_MEDICATION) == 0
    assert await _scalar(engine, "SELECT count(*) FROM source_record") == snapshots  # history kept


async def test_a_second_run_does_not_tombstone_it_again(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))
    before = await table_digests(engine)

    third = await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))

    assert third.tombstoned == 0
    assert await table_digests(engine) == before


async def test_a_record_that_comes_back_is_current_again_without_a_new_snapshot(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    original = await table_digests(engine)
    await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))

    back = await _ingest(migrated_database_url, crypto, _source())

    assert back.snapshots_created == 0
    assert back.tombstoned == 0
    assert back.revived == 1
    assert await _tombstoned(engine) == []
    assert await _current_rows_of(engine, *ONE_MEDICATION) == 1
    # A revived row is sealed again under a fresh nonce, so its ciphertext differs; every
    # other table is exactly as it was before the record went missing.
    after = await table_digests(engine)
    assert {k: v for k, v in after.items() if k != "timeline_event"} == {
        k: v for k, v in original.items() if k != "timeline_event"
    }


async def test_a_record_that_comes_back_changed_is_current_with_its_new_content(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))

    def edited(resource: dict[str, Any]) -> dict[str, Any]:
        resource["status"] = "stopped"
        return resource

    back = await _ingest(migrated_database_url, crypto, _source(mutations={ONE_MEDICATION: edited}))

    assert back.snapshots_created == 1
    assert await _tombstoned(engine) == []
    assert await _current_rows_of(engine, *ONE_MEDICATION) == 1


async def test_a_listing_that_failed_tombstones_nothing(
    migrated_database_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ingest_run, "_sleep", no_wait)
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())

    failed = await _ingest(
        migrated_database_url,
        crypto,
        _source(omit={ONE_MEDICATION}, failing_types={"Procedure"}),
    )

    assert failed.failures
    assert failed.tombstoned == 0
    assert await _tombstoned(engine) == []


async def test_a_type_that_came_back_empty_is_not_tombstoned(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    every_medication = _ids_of("MedicationRequest", SECOND_PATIENT)
    assert len(every_medication) > 1

    second = await _ingest(migrated_database_url, crypto, _source(omit=every_medication))

    assert second.tombstoned == 0
    assert await _tombstoned(engine) == []


async def test_a_wiped_source_tombstones_nothing(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    wiped = DatasetFhirTransport(
        patient_limit=SLICE, omit=_everything(), stamped_at=datetime(2026, 10, 3, tzinfo=UTC)
    )

    second = await _ingest(migrated_database_url, crypto, wiped)

    assert second.tombstoned == 0
    assert await _tombstoned(engine) == []


async def test_only_the_patient_whose_listing_lacks_the_record_is_affected(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())

    await _ingest(migrated_database_url, crypto, _source(omit={ONE_MEDICATION}))

    other_heads = await _scalar(
        engine,
        "SELECT count(*) FROM source_resource_head h"
        " JOIN source_record r ON r.id = h.source_record_id"
        " WHERE h.deleted_at IS NOT NULL AND r.patient_id <> ("
        "  SELECT patient_id FROM source_record WHERE resource_id = :i LIMIT 1)",
        i=ONE_MEDICATION[1],
    )
    assert other_heads == 0


def _everything() -> set[tuple[str, str]]:
    return {
        (r["resourceType"], r["id"])
        for resources in list(patient_resources())[:SLICE]
        for r in resources
        if r["resourceType"] != "Patient"
    }


async def test_a_head_that_moved_after_it_was_read_is_not_tombstoned(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    # Two patients ingest at once and a record moves between them: one transaction read the head
    # while it still pointed at the old snapshot, and the other has since re-pointed it.
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, _source())
    stale_snapshot = await _scalar(
        engine,
        "SELECT id FROM source_record WHERE resource_type = :t AND resource_id = :i",
        t=ONE_MEDICATION[0],
        i=ONE_MEDICATION[1],
    )

    def edited(resource: dict[str, Any]) -> dict[str, Any]:
        resource["status"] = "stopped"
        return resource

    await _ingest(migrated_database_url, crypto, _source(mutations={ONE_MEDICATION: edited}))
    source_system_id = await _scalar(
        engine, "SELECT id FROM source_system WHERE code = 'fhir-local'"
    )

    async with (
        app_role_engine(migrated_database_url) as app_engine,
        AsyncSession(app_engine) as session,
        session.begin(),
    ):
        tombstoned = await tombstone_head(
            session, source_system_id, *ONE_MEDICATION, stale_snapshot, datetime.now(UTC)
        )

    assert tombstoned is False
    assert await _tombstoned(engine) == []
    assert await _current_rows_of(engine, *ONE_MEDICATION) == 1
