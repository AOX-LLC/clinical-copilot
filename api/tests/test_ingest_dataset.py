"""The whole committed dataset through ingest, twice, as the application role.

One module-scoped run does the expensive part; the tests read what it left behind.
"""

import json
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.crypto.keyring import FieldSealer
from app.crypto.runtime import FieldCrypto
from app.ingest.run import IngestSummary, run_ingest
from app.timeline.ingest import SealContext
from app.timeline.patient_identity import read_identity
from app.timeline.vocabulary import ImportStatus
from tests.conftest import alembic_config, temporary_database
from tests.dataset import RESOURCE_COUNTS, patient_resources
from tests.dataset_server import DatasetFhirTransport
from tests.ingest_support import (
    adapter_over,
    app_role_engine,
    new_key_material,
    real_crypto,
    table_digests,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio(loop_scope="module")]

CLINIC_ZONE = ZoneInfo("America/New_York")
# Timeline rows per kind that the dataset yields (ADRs 0015 and 0016).
TIMELINE_COUNTS = {
    "lab": 6385,
    "vital": 1784,
    "medication": 855,
    "procedure": 2202,
    "encounter": 789,
    "condition": 695,
    "immunization": 147,
    "allergy": 12,
    "care_plan": 70,
    "supplement": 73,
    "protocol": 22,
}
MIN_NEEDLE_BYTES = 5  # a shorter needle would match random ciphertext by chance


@dataclass
class Ingested:
    database_url: str
    crypto: FieldCrypto
    first: IngestSummary
    second: IngestSummary
    digests_between: dict[str, str]
    digests_after: dict[str, str]
    heads_seen_between: str
    heads_seen_after: str


@pytest.fixture(scope="module")
def dataset_database_url() -> Iterator[str]:
    with temporary_database() as database_url:
        command.upgrade(alembic_config(database_url), "head")
        yield database_url


async def _heads_last_seen(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                text(
                    "SELECT md5(string_agg(last_seen_at::text, ',' ORDER BY resource_id))"
                    " FROM source_resource_head"
                )
            )
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ingested(dataset_database_url: str) -> AsyncIterator[Ingested]:
    crypto = real_crypto(new_key_material())
    owner = create_async_engine(dataset_database_url, poolclass=NullPool)
    adapter, client = adapter_over(DatasetFhirTransport())
    try:
        async with app_role_engine(dataset_database_url) as engine:
            first = await run_ingest(engine, adapter, crypto, CLINIC_ZONE)
            between = await table_digests(owner)
            seen_between = await _heads_last_seen(owner)
            second = await run_ingest(engine, adapter, crypto, CLINIC_ZONE)
        yield Ingested(
            dataset_database_url,
            crypto,
            first,
            second,
            between,
            await table_digests(owner),
            seen_between,
            await _heads_last_seen(owner),
        )
    finally:
        await client.aclose()
        await owner.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def owner_engine(dataset_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(dataset_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def _count(engine: AsyncEngine, statement: str) -> int:
    async with engine.connect() as connection:
        return int((await connection.scalar(text(statement))) or 0)


async def test_every_record_of_every_patient_is_ingested(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    summary = ingested.first

    assert summary.status is ImportStatus.SUCCEEDED
    assert summary.failures == []
    assert summary.patients == 28
    assert summary.patients_created == 28
    assert summary.records_by_type == Counter(RESOURCE_COUNTS)
    assert summary.records_seen == sum(RESOURCE_COUNTS.values()) == 13_803
    assert summary.snapshots_created == 13_803
    assert await _count(owner_engine, "SELECT count(*) FROM source_record") == 13_803
    assert await _count(owner_engine, "SELECT count(*) FROM source_resource_head") == 13_803
    assert await _count(owner_engine, "SELECT count(*) FROM patient") == 28
    assert await _count(owner_engine, "SELECT count(*) FROM patient_source_link") == 28
    assert await _count(owner_engine, "SELECT count(*) FROM data_key") == 28


async def test_snapshots_per_resource_type_match_the_dataset(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT resource_type, count(*) AS n FROM source_record GROUP BY 1")
        )
        counts = {row.resource_type: row.n for row in rows}

    assert counts == RESOURCE_COUNTS


async def test_timeline_rows_per_kind_match_the_dataset(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT kind::text AS kind, count(*) AS n FROM timeline_event"
                " WHERE superseded_at IS NULL GROUP BY 1"
            )
        )
        counts = {row.kind: row.n for row in rows}

    assert counts == TIMELINE_COUNTS
    assert sum(counts.values()) == 13_034
    assert await _count(owner_engine, "SELECT count(*) FROM timeline_event") == 13_034


async def test_the_run_is_recorded(ingested: Ingested, owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        runs = (
            await connection.execute(
                text(
                    "SELECT status::text AS status, records_seen, snapshots_created, error_code,"
                    " finished_at IS NOT NULL AS finished FROM import_run ORDER BY started_at"
                )
            )
        ).all()

    assert [tuple(run) for run in runs] == [
        ("succeeded", 13_803, 13_803, None, True),
        ("succeeded", 13_803, 0, None, True),
    ]


async def test_running_ingest_again_changes_nothing(ingested: Ingested) -> None:
    second = ingested.second

    assert second.status is ImportStatus.SUCCEEDED
    assert second.records_seen == 13_803
    assert second.snapshots_created == 0
    assert second.heads_moved == 0
    assert second.patients_created == 0
    assert ingested.digests_after == ingested.digests_between


async def test_a_second_run_only_touches_last_seen_at(ingested: Ingested) -> None:
    assert ingested.heads_seen_after != ingested.heads_seen_between


async def test_each_patients_identity_round_trips(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        links = (
            await connection.execute(
                text("SELECT external_id, patient_id FROM patient_source_link")
            )
        ).all()
    patient_by_external_id = {row.external_id: row.patient_id for row in links}
    checked = 0
    async with AsyncSession(owner_engine) as session:
        await ingested.crypto.keystore.load(session, list(patient_by_external_id.values()))
        for resources in patient_resources():
            record = next(r for r in resources if r["resourceType"] == "Patient")
            identity = await read_identity(
                session, patient_by_external_id[record["id"]], ingested.crypto.sealer
            )
            official = next(n for n in record["name"] if n.get("use") == "official")
            assert identity.family_name == official["family"]
            assert identity.birth_date == date.fromisoformat(record["birthDate"])
            checked += 1

    assert checked == 28


def _identifying_values(*, plaintext_columns: bool = False) -> set[bytes]:
    """Every name, birth date and identifier of every seeded patient, as UTF-8 bytes.

    ``plaintext_columns`` is the set that must not appear in a plaintext column: names, and
    identifiers other than a patient's own id. Birth dates are left out because the timeline's
    plaintext times can legitimately contain one. Synthea uses a patient's id as one of their
    record numbers, and the source link and resource id hold that id in plaintext by design.
    """
    values: set[str] = set()
    for resources in patient_resources():
        patient = next(r for r in resources if r["resourceType"] == "Patient")
        for name in patient["name"]:
            values.update(name.get("given", []))
            values.add(name["family"])
            values.add(" ".join([*name.get("given", []), name["family"]]))
        if not plaintext_columns:
            values.add(patient["birthDate"])
        values.update(
            item["value"]
            for item in patient["identifier"]
            if not plaintext_columns or item["value"] != patient["id"]
        )
        for extension in patient.get("extension", []):
            if "valueString" in extension:  # the mother's maiden name
                values.add(extension["valueString"])
    return {v.encode() for v in values if len(v.encode()) >= MIN_NEEDLE_BYTES}


async def _column_blobs(engine: AsyncEngine, pattern: str, as_text: bool) -> dict[str, bytes]:
    """All values of every column whose name matches, joined: one blob per table.column."""
    blobs: dict[str, bytes] = {}
    async with engine.connect() as connection:
        columns = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                    " AND column_name LIKE :pattern AND data_type = :type"
                ),
                {"pattern": pattern, "type": "text" if as_text else "bytea"},
            )
        ).all()
        for table, column in columns:
            values = (
                await connection.execute(text(f'SELECT "{column}" FROM {table}'))  # noqa: S608
            ).scalars()
            blobs[f"{table}.{column}"] = b"\x00".join(
                v.encode() if isinstance(v, str) else bytes(v) for v in values if v is not None
            )
    return blobs


async def test_no_identifying_value_is_in_any_encrypted_column(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    needles = _identifying_values()
    blobs = await _column_blobs(owner_engine, "%\\_enc", as_text=False)

    assert {
        "patient.given_name_enc",
        "patient.family_name_enc",
        "patient.birth_date_enc",
        "patient.identifiers_enc",
        "source_record.payload_enc",
        "timeline_event.value_text_enc",
        "timeline_event.detail_enc",
    } <= blobs.keys(), "the scan must cover every sealed column"
    assert sum(len(blob) for blob in blobs.values()) > 1_000_000, "the scan read too little"
    assert len(needles) > 28 * 4
    leaks = sorted(
        f"{column}: {needle[:3]!r}..."
        for column, blob in blobs.items()
        for needle in needles
        if needle in blob
    )
    assert leaks == []


async def test_the_scan_would_find_plaintext_if_it_were_there(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    # A negative scan proves nothing unless the same needles do occur in the plaintext payloads.
    needles = _identifying_values()
    sealer: FieldSealer = ingested.crypto.sealer
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT id, patient_id, payload_enc FROM source_record"
                    " WHERE resource_type = 'Patient' LIMIT 1"
                )
            )
        ).one()
    async with AsyncSession(owner_engine) as session:
        await ingested.crypto.keystore.load(session, [row.patient_id])
    plaintext = sealer.open(
        bytes(row.payload_enc), SealContext("source_record", "payload_enc", row.id, row.patient_id)
    )

    assert json.loads(plaintext)["resourceType"] == "Patient"
    assert sum(1 for needle in needles if needle in plaintext) >= 4


async def test_no_patient_name_or_private_identifier_is_in_a_plaintext_column(
    ingested: Ingested, owner_engine: AsyncEngine
) -> None:
    needles = _identifying_values(plaintext_columns=True)
    blobs = await _column_blobs(owner_engine, "%", as_text=True)

    assert "timeline_event.code_display" in blobs
    leaks = sorted(
        f"{column}: {needle[:3]!r}..."
        for column, blob in blobs.items()
        for needle in needles
        if needle in blob
    )
    assert leaks == []
