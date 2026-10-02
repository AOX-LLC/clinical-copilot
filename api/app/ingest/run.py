"""Ingest every patient an adapter lists: snapshots, heads, timeline rows, one transaction each.

A run reads the whole patient list first, then handles patients a few at a time. For each
patient it fetches every record outside any database transaction, then in one transaction
resolves the patient, loads their data key, and ingests the Patient record and each other
record through ``ingest_snapshot`` with the real sealer. A patient whose records fail rolls
back alone; the run carries on, ends ``failed`` and says which kinds of failure it saw.

Running it again changes nothing but ``last_seen_at`` on the heads and one new ``import_run``
row: identical content hashes to the snapshot already stored, so nothing is inserted, no head
moves, nothing is projected, and a patient's identity is not sealed again.

One run at a time: a Postgres advisory lock held on a dedicated connection turns a second
concurrent run away instead of letting two of them race over the same heads.
"""

import asyncio
import logging
import resource
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crypto.errors import CryptoError
from app.crypto.runtime import FieldCrypto
from app.ehr.ports import (
    EhrAdapter,
    EhrAdapterError,
    RateLimitedError,
    RecordKind,
    RetryableSourceError,
    SourcePatient,
    SourceRecord,
)
from app.timeline.ingest import IngestContext, IngestError, ingest_snapshot
from app.timeline.models import ImportRun, SourceSystem
from app.timeline.normalize import NormalizationError, build_projector, parse_resource
from app.timeline.normalize.patient import identity_from_fhir_patient
from app.timeline.patient_identity import LinkRaceError, replace_identity, resolve_source_patient
from app.timeline.vocabulary import ImportStatus, ImportTrigger

logger = logging.getLogger("app.ingest")

# Two ingest runs must not race over the same heads. The key is arbitrary but fixed.
ADVISORY_LOCK_KEY = 0x434F_5049_4C4F_5401
PATIENT_PAGE_SIZE = 50
# The FHIR adapter keeps at most four listings open at once (ADR 0013); more concurrent
# patients than that would evict each other's paging snapshots.
MAX_CONCURRENCY = 4
FETCH_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 30.0

PATIENT_FAILURES = (
    IngestError,
    NormalizationError,
    EhrAdapterError,
    CryptoError,
    LinkRaceError,
)


class IngestBusyError(Exception):
    """Another ingest run holds the lock."""


class UnknownSourceError(Exception):
    """The adapter's source system is not in the ``source_system`` table."""


@dataclass(slots=True)
class PatientResult:
    created: bool = False
    records: Counter[str] = field(default_factory=Counter)
    snapshots_created: int = 0
    heads_moved: int = 0


@dataclass(slots=True)
class IngestSummary:
    import_run_id: uuid.UUID
    status: ImportStatus
    patients: int = 0
    patients_created: int = 0
    records_seen: int = 0
    snapshots_created: int = 0
    heads_moved: int = 0
    records_by_type: Counter[str] = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    peak_rss_mib: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status is ImportStatus.SUCCEEDED


async def run_ingest(
    engine: AsyncEngine,
    adapter: EhrAdapter,
    crypto: FieldCrypto,
    clinic_zone: ZoneInfo,
    concurrency: int = MAX_CONCURRENCY,
) -> IngestSummary:
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    started = time.monotonic()
    async with engine.connect() as lock_connection:
        lock_connection = await lock_connection.execution_options(isolation_level="AUTOCOMMIT")
        if not await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        ):
            raise IngestBusyError("another ingest run is in progress")
        try:
            summary = await _run_locked(engine, adapter, crypto, clinic_zone, concurrency)
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
    summary.seconds = time.monotonic() - started
    summary.peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return summary


async def _run_locked(
    engine: AsyncEngine,
    adapter: EhrAdapter,
    crypto: FieldCrypto,
    clinic_zone: ZoneInfo,
    concurrency: int,
) -> IngestSummary:
    source_system_id = await _source_system_id(engine, adapter.source.code)
    run_id = await _start_run(engine, source_system_id)
    summary = IngestSummary(import_run_id=run_id, status=ImportStatus.RUNNING)
    try:
        # The whole list first, so the adapter's few paging snapshots are free for the
        # per-patient listings that follow.
        patients = await _list_patients(adapter)
        kinds = (await adapter.capabilities()).record_kinds - {RecordKind.PATIENT}
        worker = _PatientIngest(
            engine, adapter, crypto, clinic_zone, source_system_id, run_id, kinds
        )
        gate = asyncio.Semaphore(concurrency)

        async def one(patient: SourcePatient) -> None:
            async with gate:
                await _record_outcome(summary, patient, worker)

        # A TaskGroup, not gather: an error nobody expected cancels the other patients before
        # the run is marked failed and the lock is released, so nothing keeps writing after.
        async with asyncio.TaskGroup() as group:
            for patient in patients:
                group.create_task(one(patient))
        summary.status = ImportStatus.FAILED if summary.failures else ImportStatus.SUCCEEDED
        await _finish_run(engine, summary)
    except BaseException as error:
        summary.status = ImportStatus.FAILED
        cause = error.exceptions[0] if isinstance(error, BaseExceptionGroup) else error
        summary.failures.append(type(cause).__name__)
        await asyncio.shield(_finish_run(engine, summary))
        raise
    return summary


async def _record_outcome(
    summary: IngestSummary, patient: SourcePatient, worker: "_PatientIngest"
) -> None:
    try:
        result = await worker(patient)
    except PATIENT_FAILURES as error:
        # The messages of these errors name a resource and a reason, never content.
        logger.error("patient %s failed: %s: %s", patient.external_id, type(error).__name__, error)
        summary.failures.append(type(error).__name__)
        return
    except SQLAlchemyError as error:
        # A database error's message can carry row values; name only its type.
        logger.error("patient %s failed: %s", patient.external_id, type(error).__name__)
        summary.failures.append(type(error).__name__)
        return
    summary.patients += 1
    summary.patients_created += result.created
    summary.records_seen += sum(result.records.values())
    summary.snapshots_created += result.snapshots_created
    summary.heads_moved += result.heads_moved
    summary.records_by_type.update(result.records)


@dataclass(frozen=True, slots=True)
class _PatientIngest:
    engine: AsyncEngine
    adapter: EhrAdapter
    crypto: FieldCrypto
    clinic_zone: ZoneInfo
    source_system_id: int
    import_run_id: uuid.UUID
    kinds: frozenset[RecordKind]

    async def __call__(self, patient: SourcePatient) -> PatientResult:
        records = await _fetch_with_retry(self.adapter, patient.external_id, self.kinds)
        projector = build_projector(self.clinic_zone)
        result = PatientResult()
        now = datetime.now(UTC)
        label = f"{patient.record.resource_type}/{patient.record.resource_id}"
        identity = identity_from_fhir_patient(parse_resource(patient.record.payload, label), label)

        async with AsyncSession(self.engine) as session, session.begin():
            patient_id, result.created = await resolve_source_patient(
                session,
                self.source_system_id,
                patient.external_id,
                identity,
                self.crypto.keystore,
                self.crypto.sealer,
                self.crypto.indexer,
            )
            context = IngestContext(
                source_system_id=self.source_system_id,
                projector=projector,
                sealer=self.crypto.sealer,
                patient_id=patient_id,
                import_run_id=self.import_run_id,
            )
            outcome = await ingest_snapshot(session, patient.record, context, now)
            if outcome.head_moved and not result.created:
                await replace_identity(
                    session, patient_id, identity, self.crypto.sealer, self.crypto.indexer
                )
            tally = [outcome]
            result.records[patient.record.resource_type] += 1
            for record in records:
                tally.append(await ingest_snapshot(session, record, context, now))
                result.records[record.resource_type] += 1
        result.snapshots_created = sum(item.snapshot_created for item in tally)
        result.heads_moved = sum(item.head_moved for item in tally)
        return result


async def _list_patients(adapter: EhrAdapter) -> list[SourcePatient]:
    patients: list[SourcePatient] = []
    cursor: str | None = None
    while True:
        page = await adapter.list_patients(cursor, PATIENT_PAGE_SIZE)
        patients.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return patients


async def _fetch_with_retry(
    adapter: EhrAdapter, external_id: str, kinds: frozenset[RecordKind]
) -> list[SourceRecord]:
    """Every record of a patient. A listing that expired or was throttled starts again."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return await _fetch_all(adapter, external_id, kinds)
        except (RetryableSourceError, RateLimitedError) as error:
            if attempt == FETCH_ATTEMPTS:
                raise
            delay = min(
                getattr(error, "retry_after_seconds", None) or 2.0**attempt, MAX_BACKOFF_SECONDS
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _fetch_all(
    adapter: EhrAdapter, external_id: str, kinds: frozenset[RecordKind]
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    cursor: str | None = None
    while True:
        page = await adapter.fetch_changes(external_id, kinds, None, cursor)
        records.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return records


async def _source_system_id(engine: AsyncEngine, code: str) -> int:
    async with AsyncSession(engine) as session:
        found = await session.scalar(select(SourceSystem.id).where(SourceSystem.code == code))
    if found is None:
        raise UnknownSourceError(f"source system {code!r} is not registered")
    return found


async def _start_run(engine: AsyncEngine, source_system_id: int) -> uuid.UUID:
    async with AsyncSession(engine) as session, session.begin():
        run_id: uuid.UUID | None = await session.scalar(
            insert(ImportRun)
            .values(source_system_id=source_system_id, trigger=ImportTrigger.MANUAL)
            .returning(ImportRun.id)
        )
    assert run_id is not None  # noqa: S101  # RETURNING always yields the inserted row
    return run_id


async def _finish_run(engine: AsyncEngine, summary: IngestSummary) -> None:
    error_code = ",".join(sorted(set(summary.failures)))[:200] or None
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            update(ImportRun)
            .where(ImportRun.id == summary.import_run_id)
            .values(
                status=summary.status,
                finished_at=datetime.now(UTC),
                records_seen=summary.records_seen,
                snapshots_created=summary.snapshots_created,
                error_code=error_code,
            )
        )
