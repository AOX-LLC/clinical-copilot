"""Snapshot ingestion against a real database, running as the application role."""

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.ehr.fake import FakeAdapter
from app.ehr.ports import RecordKind, SourceRecord
from app.timeline.clinical_time import parse_fhir_datetime, sort_instant
from app.timeline.ingest import (
    IngestContext,
    IngestError,
    IngestOutcome,
    SealContext,
    TimelineEventDraft,
    ingest_snapshot,
)
from app.timeline.models import Patient, SourceResourceHead, StoredSourceRecord, TimelineEvent
from app.timeline.vocabulary import TimelineKind
from tests.conftest import APP_ROLE
from tests.fixtures import (
    FAKE_SOURCE,
    NOTIFICATION_SECRET,
    SOURCE_CLOCK_START,
    populated_fake_adapter,
    synthetic_hba1c,
    synthetic_patient,
)

pytestmark = pytest.mark.db

INGEST_CLOCK_START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CLINIC_ZONE = ZoneInfo("America/New_York")


class LabelingTestSealer:
    """Test-only stand-in for encryption. It is NOT encryption.

    It reverses the bytes behind a label derived from the seal context, which is enough
    to prove that plaintext never lands in an ``_enc`` column and that the context is used.
    """

    def seal(self, plaintext: bytes, context: SealContext) -> bytes:
        label = f"{context.table}.{context.column}.{context.row_id}".encode()
        return b"test-sealed:" + hashlib.sha256(label).digest()[:8] + plaintext[::-1]


def project_hba1c(record: SourceRecord) -> Sequence[TimelineEventDraft]:
    """A minimal test projector: one lab row per Observation, nothing for other types."""
    resource = json.loads(record.payload, parse_float=Decimal)
    if resource["resourceType"] != "Observation":
        return []
    coding = resource["code"]["coding"][0]
    occurred = parse_fhir_datetime(resource["effectiveDateTime"])
    return [
        TimelineEventDraft(
            source_path="",
            kind=TimelineKind.LAB,
            occurred=occurred,
            sort_at=sort_instant(occurred, CLINIC_ZONE),
            status=resource["status"],
            code_system=coding["system"],
            code=coding["code"],
            value_numeric=resource["valueQuantity"]["value"],
            value_unit=resource["valueQuantity"]["unit"],
        )
    ]


class IngestHarness:
    """Ingests records one transaction at a time as the app role, with a ticking clock."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._now = INGEST_CLOCK_START
        self._patients: dict[str, uuid.UUID] = {}

    async def ingest(self, record: SourceRecord, patient_external_id: str) -> IngestOutcome:
        self._now += timedelta(minutes=1)
        async with AsyncSession(self._engine) as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            context = IngestContext(
                source_system_id=await _fhir_local_id(session),
                projector=project_hba1c,
                sealer=LabelingTestSealer(),
                patient_id=await self._patient_id(session, patient_external_id),
            )
            return await ingest_snapshot(session, record, context, self._now)

    async def ingest_everything(
        self, adapter: FakeAdapter, patient_external_ids: Sequence[str]
    ) -> None:
        kinds = (await adapter.capabilities()).record_kinds
        for patient_external_id in patient_external_ids:
            cursor: str | None = None
            while True:
                page = await adapter.fetch_changes(patient_external_id, kinds, None, cursor)
                for record in page.items:
                    await self.ingest(record, patient_external_id)
                cursor = page.next_cursor
                if cursor is None:
                    break

    async def _patient_id(self, session: AsyncSession, external_id: str) -> uuid.UUID:
        if external_id not in self._patients:
            patient_id = await session.scalar(
                insert(Patient).values(sex_at_birth="unknown").returning(Patient.id)
            )
            assert patient_id is not None
            self._patients[external_id] = patient_id
        return self._patients[external_id]


async def _fhir_local_id(session: AsyncSession) -> int:
    source_system_id = await session.scalar(
        text("SELECT id FROM source_system WHERE code = 'fhir-local'")
    )
    assert isinstance(source_system_id, int)
    return source_system_id


async def _snapshot_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(StoredSourceRecord))
    return int(count or 0)


async def _heads(
    engine: AsyncEngine,
) -> dict[tuple[str, str], tuple[uuid.UUID, datetime, datetime]]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(SourceResourceHead))
    return {
        (row.resource_type, row.resource_id): (
            row.source_record_id,
            row.changed_at,
            row.last_seen_at,
        )
        for row in rows
    }


async def _current_rows(engine: AsyncEngine) -> list[tuple[uuid.UUID, uuid.UUID, Decimal | None]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(TimelineEvent.id, TimelineEvent.source_record_id, TimelineEvent.value_numeric)
            .where(TimelineEvent.superseded_at.is_(None))
            .order_by(TimelineEvent.id)
        )
    return [(row.id, row.source_record_id, row.value_numeric) for row in rows]


async def test_content_that_reverts_makes_the_original_snapshot_current_again(
    engine: AsyncEngine,
) -> None:
    harness = IngestHarness(engine)
    adapter = FakeAdapter(FAKE_SOURCE, NOTIFICATION_SECRET, SOURCE_CLOCK_START)
    adapter.put(RecordKind.PATIENT, synthetic_patient("p1"), "p1")

    async def put_and_ingest(value: str) -> IngestOutcome:
        adapter.put(
            RecordKind.OBSERVATION, synthetic_hba1c("obs-1", "p1", value, "2026-03-08"), "p1"
        )
        return await harness.ingest(await adapter.get_record("Observation", "obs-1"), "p1")

    content_a = await put_and_ingest("5.4")
    content_b = await put_and_ingest("6.3")
    content_a_again = await put_and_ingest("5.4")

    assert (content_a.snapshot_created, content_a.head_moved) == (True, True)
    assert (content_b.snapshot_created, content_b.head_moved) == (True, True)
    assert (content_a_again.snapshot_created, content_a_again.head_moved) == (False, True)
    assert content_a_again.source_record_id == content_a.source_record_id
    assert await _snapshot_count(engine) == 2

    heads = await _heads(engine)
    assert heads[("Observation", "obs-1")][0] == content_a.source_record_id

    current = await _current_rows(engine)
    assert [(record_id, value) for _, record_id, value in current] == [
        (content_a.source_record_id, Decimal("5.4"))
    ]


async def test_reloading_identical_content_into_a_reset_server_creates_no_snapshots(
    engine: AsyncEngine,
) -> None:
    harness = IngestHarness(engine)
    adapter = populated_fake_adapter()
    patients = ["patient-1", "patient-2", "patient-3"]
    await harness.ingest_everything(adapter, patients)
    snapshots_before = await _snapshot_count(engine)
    heads_before = await _heads(engine)
    rows_before = await _current_rows(engine)

    adapter.reset_and_reload()
    reloaded = await adapter.get_record("Observation", "obs-1-1")
    assert reloaded.version_id == "1"
    await harness.ingest_everything(adapter, patients)

    assert await _snapshot_count(engine) == snapshots_before == 12
    heads_after = await _heads(engine)
    assert heads_after.keys() == heads_before.keys()
    for resource, (snapshot_id, changed_at, last_seen_at) in heads_after.items():
        before_snapshot_id, before_changed_at, before_last_seen_at = heads_before[resource]
        assert (snapshot_id, changed_at) == (before_snapshot_id, before_changed_at)
        assert last_seen_at > before_last_seen_at
    assert await _current_rows(engine) == rows_before


async def test_payloads_are_sealed_never_stored_as_plaintext(engine: AsyncEngine) -> None:
    harness = IngestHarness(engine)
    adapter = populated_fake_adapter()
    record = await adapter.get_record("Observation", "obs-1-1")

    outcome = await harness.ingest(record, "patient-1")

    async with engine.connect() as connection:
        stored = await connection.scalar(
            select(StoredSourceRecord.payload_enc).where(
                StoredSourceRecord.id == outcome.source_record_id
            )
        )
    assert stored is not None
    assert stored.startswith(b"test-sealed:")
    assert record.payload not in stored


async def test_a_record_whose_hash_does_not_match_its_payload_is_refused(
    engine: AsyncEngine,
) -> None:
    harness = IngestHarness(engine)
    record = await populated_fake_adapter().get_record("Observation", "obs-1-1")
    tampered = dataclasses.replace(record, payload=record.payload.replace(b"5.4", b"9.9"))

    with pytest.raises(IngestError, match="content hash does not match"):
        await harness.ingest(tampered, "patient-1")
    assert await _snapshot_count(engine) == 0
