"""Ingest behaviour on a three-patient slice of the dataset, as the application role."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crypto.runtime import FieldCrypto
from app.ingest import run as ingest_run
from app.ingest.__main__ import main
from app.ingest.run import ADVISORY_LOCK_KEY, IngestBusyError, run_ingest
from app.timeline.patient_identity import find_patients, read_identity
from app.timeline.vocabulary import ImportStatus
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
LEUKOCYTES = ("Observation", "0b56998e-f475-39ed-9bdc-fdefe1ba5e05")
FIRST_PATIENT = ("Patient", "0b56998e-f475-39ed-f31e-e0e5f79c9aee")
SECOND_PATIENTS_MEDICATION = ("MedicationRequest", "0b879479-3d66-a073-11a1-e749dce77bfb")
SECOND_PATIENT = "0b879479-3d66-a073-a9d5-8ba6677556e2"
RENAMED = "Zzyzx-Renamed"


def _wrong_shape(resource: dict[str, Any]) -> dict[str, Any]:
    """A contained medication whose coding is an object where FHIR has a list."""
    resource["contained"][0]["code"]["coding"] = {"not": "a list"}
    return resource


def _set(path: tuple[str, ...], value: Any) -> Any:
    def mutate(resource: dict[str, Any]) -> dict[str, Any]:
        target = resource
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return resource

    return mutate


async def _ingest(
    url: str, crypto: FieldCrypto, transport: DatasetFhirTransport, concurrency: int = 4
) -> Any:
    adapter, client = adapter_over(transport)
    try:
        async with app_role_engine(url) as app_engine:
            return await run_ingest(app_engine, adapter, crypto, CLINIC_ZONE, concurrency)
    finally:
        await client.aclose()


async def _scalar(engine: AsyncEngine, statement: str, **parameters: object) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(statement), parameters)


async def test_a_wiped_and_reloaded_source_yields_no_new_snapshots(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    first = await _ingest(migrated_database_url, crypto, DatasetFhirTransport(patient_limit=SLICE))
    before = await table_digests(engine)
    # Same content, new version stamps: what a reset and reseeded FHIR server serves.
    reloaded = DatasetFhirTransport(
        stamped_at=datetime(2026, 10, 3, 9, 30, tzinfo=UTC), patient_limit=SLICE
    )
    second = await _ingest(migrated_database_url, crypto, reloaded)

    assert first.snapshots_created == first.records_seen > 0
    assert (second.snapshots_created, second.heads_moved) == (0, 0)
    assert await table_digests(engine) == before


async def test_a_changed_record_moves_its_head_and_replaces_its_timeline_row(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, DatasetFhirTransport(patient_limit=SLICE))
    changed = DatasetFhirTransport(
        patient_limit=SLICE, mutations={LEUKOCYTES: _set(("valueQuantity", "value"), 6.1)}
    )
    second = await _ingest(migrated_database_url, crypto, changed)

    assert (second.snapshots_created, second.heads_moved) == (1, 1)
    current = await _scalar(
        engine,
        "SELECT value_numeric FROM timeline_event WHERE code = '6690-2' AND superseded_at IS NULL"
        " AND source_record_id = (SELECT source_record_id FROM source_resource_head"
        " WHERE resource_id = :id)",
        id=LEUKOCYTES[1],
    )
    superseded = await _scalar(
        engine,
        "SELECT count(*) FROM timeline_event WHERE superseded_at IS NOT NULL"
        " AND code = '6690-2' AND value_numeric = 5.5248",
    )
    assert current == Decimal("6.1")
    assert superseded == 1


async def test_a_changed_patient_record_reseals_the_identity_and_its_lookup(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    await _ingest(migrated_database_url, crypto, DatasetFhirTransport(patient_limit=SLICE))
    renamed = DatasetFhirTransport(
        patient_limit=SLICE, mutations={FIRST_PATIENT: _rename_family(RENAMED)}
    )

    second = await _ingest(migrated_database_url, crypto, renamed)

    assert (second.snapshots_created, second.heads_moved) == (1, 1)
    patient_id = await _scalar(
        engine,
        "SELECT patient_id FROM patient_source_link WHERE external_id = :id",
        id=FIRST_PATIENT[1],
    )
    async with AsyncSession(engine) as session:
        await crypto.keystore.load(session, [patient_id])
        assert (await read_identity(session, patient_id, crypto.sealer)).family_name == RENAMED
        assert await find_patients(session, crypto.indexer, name=RENAMED) == [patient_id]


def _rename_family(family: str) -> Any:
    def mutate(resource: dict[str, Any]) -> dict[str, Any]:
        for name in resource["name"]:
            name["family"] = family
        return resource

    return mutate


async def test_a_patient_whose_record_cannot_be_normalized_rolls_back_alone(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    broken = DatasetFhirTransport(
        patient_limit=SLICE,
        mutations={
            SECOND_PATIENTS_MEDICATION: _set(("medicationReference", "reference"), "Medication/x")
        },
    )

    summary = await _ingest(migrated_database_url, crypto, broken)

    assert summary.status is ImportStatus.FAILED
    assert summary.failures == ["NormalizationError"]
    assert summary.patients == SLICE - 1
    assert await _scalar(engine, "SELECT count(*) FROM patient") == SLICE - 1
    assert await _scalar(engine, "SELECT count(*) FROM patient_source_link") == SLICE - 1
    assert await _scalar(engine, "SELECT status::text || ':' || error_code FROM import_run") == (
        "failed:NormalizationError"
    )


async def test_a_second_run_at_the_same_time_is_turned_away(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    async with engine.connect() as holder:
        holder = await holder.execution_options(isolation_level="AUTOCOMMIT")
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        try:
            with pytest.raises(IngestBusyError):
                await _ingest(
                    migrated_database_url,
                    real_crypto(new_key_material()),
                    DatasetFhirTransport(patient_limit=1),
                )
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )

    assert await _scalar(engine, "SELECT count(*) FROM import_run") == 0


async def test_ingest_never_runs_more_patients_than_the_adapter_can_page(
    migrated_database_url: str,
) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await _ingest(
            migrated_database_url,
            real_crypto(new_key_material()),
            DatasetFhirTransport(patient_limit=1),
            concurrency=5,
        )


def test_the_command_refuses_to_run_without_its_keys(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_database_url)
    for variable in ("FIELD_KEK", "FIELD_KEK_FILE", "BLIND_INDEX_KEY", "BLIND_INDEX_KEY_FILE"):
        monkeypatch.delenv(variable, raising=False)

    assert main() == 1


async def test_a_resource_of_an_unexpected_shape_fails_only_its_patient(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    damaged = DatasetFhirTransport(
        patient_limit=SLICE,
        mutations={SECOND_PATIENTS_MEDICATION: _wrong_shape},
    )

    summary = await _ingest(migrated_database_url, crypto, damaged)

    assert summary.status is ImportStatus.FAILED
    assert summary.failures == ["NormalizationError"]
    assert summary.patients == SLICE - 1
    assert await _scalar(engine, "SELECT count(*) FROM patient") == SLICE - 1


async def test_a_failed_patient_leaves_no_rows_behind(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    crypto = real_crypto(new_key_material())
    broken = DatasetFhirTransport(
        patient_limit=SLICE,
        mutations={SECOND_PATIENTS_MEDICATION: _wrong_shape},
    )
    await _ingest(migrated_database_url, crypto, broken)

    # The Patient record is ingested first, so its snapshot must have rolled back too.
    for resource_id in (SECOND_PATIENT, SECOND_PATIENTS_MEDICATION[1]):
        assert (
            await _scalar(
                engine, "SELECT count(*) FROM source_record WHERE resource_id = :id", id=resource_id
            )
            == 0
        )
    assert (
        await _scalar(
            engine,
            "SELECT count(*) FROM patient_source_link WHERE external_id = :id",
            id=SECOND_PATIENT,
        )
        == 0
    )
    assert await _scalar(engine, "SELECT count(*) FROM data_key") == SLICE - 1
    assert await _scalar(engine, "SELECT count(*) FROM patient") == SLICE - 1


async def test_an_unexpected_error_stops_the_run_and_releases_the_lock(
    migrated_database_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*_: object) -> list[object]:
        raise RuntimeError("not a failure anyone planned for")

    monkeypatch.setattr(ingest_run, "_fetch_with_retry", explode)

    with pytest.raises(ExceptionGroup):
        await _ingest(
            migrated_database_url,
            real_crypto(new_key_material()),
            DatasetFhirTransport(patient_limit=SLICE),
        )

    assert await _scalar(engine, "SELECT status::text || ':' || error_code FROM import_run") == (
        "failed:RuntimeError"
    )
    assert await _scalar(engine, "SELECT pg_try_advisory_lock(:key)", key=ADVISORY_LOCK_KEY)


async def test_a_database_error_in_one_patient_is_counted_and_the_others_continue() -> None:
    class Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, patient: object) -> ingest_run.PatientResult:
            self.calls += 1
            if self.calls == 1:
                raise OperationalError("SELECT 1", {}, Exception("row values live here"))
            return ingest_run.PatientResult()

    summary = ingest_run.IngestSummary(uuid.uuid4(), ImportStatus.RUNNING)
    worker = Worker()
    patient = SimpleNamespace(external_id="p")

    for _ in range(2):
        await ingest_run._record_outcome(summary, patient, worker)  # type: ignore[arg-type]

    assert summary.failures == ["OperationalError"]
    assert summary.patients == 1


def _belongs_to_someone_else(resource: dict[str, Any]) -> dict[str, Any]:
    resource["subject"] = {"reference": f"Patient/{SECOND_PATIENT}"}
    return resource


def _names_no_patient(resource: dict[str, Any]) -> dict[str, Any]:
    resource.pop("subject")
    return resource


@pytest.mark.parametrize("damage", [_belongs_to_someone_else, _names_no_patient])
async def test_a_record_that_does_not_name_its_patient_is_never_ingested(
    migrated_database_url: str, engine: AsyncEngine, damage: Any
) -> None:
    crypto = real_crypto(new_key_material())
    transport = DatasetFhirTransport(patient_limit=SLICE, mutations={LEUKOCYTES: damage})

    summary = await _ingest(migrated_database_url, crypto, transport)

    assert summary.failures == ["IngestError"]
    assert summary.patients == SLICE - 1
    # The first patient's records, including the one claiming a stranger, rolled back together.
    assert await _scalar(engine, "SELECT count(*) FROM patient") == SLICE - 1
    assert (
        await _scalar(
            engine, "SELECT count(*) FROM source_record WHERE resource_id = :id", id=LEUKOCYTES[1]
        )
        == 0
    )
