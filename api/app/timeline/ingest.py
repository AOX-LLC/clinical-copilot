"""Store a source record as a snapshot, move its head, and re-project its timeline rows.

A source record is stored once per distinct content hash. Every import then points the
resource's head at the snapshot matching what the source returned this time, so content
that reverts (A, then B, then A again) makes A current again even though A's snapshot
already existed. When the head moves, the previous snapshot's timeline rows are
superseded and the new head's rows are projected (or revived) in the same transaction.

Callers own the transaction: run ``ingest_snapshot`` inside ``session.begin()``.
"""

import hmac
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ehr.ports import SourceRecord
from app.timeline.canonical import content_sha256
from app.timeline.clinical_time import ClinicalTime
from app.timeline.models import SourceResourceHead, StoredSourceRecord, TimelineEvent
from app.timeline.vocabulary import TimelineKind, TimePrecision

# Timeline event ids derive from (snapshot, path), so a re-projected row keeps its id and
# the ciphertext bound to that id stays valid.
TIMELINE_EVENT_ID_NAMESPACE = uuid.UUID("0b8f7c52-91d4-4f0e-8a3c-5e2d7b6a1c90")


class IngestError(Exception):
    """A record could not be ingested; the message names the resource, never its content."""


@dataclass(frozen=True, slots=True)
class SealContext:
    """Where a ciphertext will live. Sealers bind it as associated data."""

    table: str
    column: str
    row_id: uuid.UUID


class PayloadSealer(Protocol):
    def seal(self, plaintext: bytes, context: SealContext) -> bytes: ...


@dataclass(frozen=True, slots=True, repr=False)
class TimelineEventDraft:
    """One timeline row a projector wants stored. Its repr names the row, never its content."""

    source_path: str
    kind: TimelineKind
    occurred: ClinicalTime | None
    sort_at: datetime
    status: str | None = None
    code_system: str | None = None
    code: str | None = None
    code_display: str | None = None
    period_end: ClinicalTime | None = None
    recorded_at: datetime | None = None
    value_numeric: Decimal | None = None
    value_unit: str | None = None
    value_text: str | None = None
    ref_low: Decimal | None = None
    ref_high: Decimal | None = None
    ref_text: str | None = None
    source_interpretation: str | None = None
    detail_json: bytes | None = None

    def __repr__(self) -> str:
        return f"TimelineEventDraft(source_path={self.source_path!r}, kind={self.kind.value!r})"


class TimelineProjector(Protocol):
    def __call__(self, record: SourceRecord) -> Sequence[TimelineEventDraft]: ...


@dataclass(frozen=True, slots=True)
class IngestContext:
    source_system_id: int
    projector: TimelineProjector
    sealer: PayloadSealer
    patient_id: uuid.UUID | None = None
    import_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    source_record_id: uuid.UUID
    snapshot_created: bool
    head_moved: bool


async def ingest_snapshot(
    session: AsyncSession, record: SourceRecord, context: IngestContext, now: datetime
) -> IngestOutcome:
    _verify_content_hash(record)
    try:
        snapshot_id, snapshot_created = await _store_snapshot(session, record, context, now)
        previous_head = await _move_head(session, record, context, snapshot_id, now)

        head_moved = previous_head != snapshot_id
        if head_moved:
            await _supersede_rows_of(session, previous_head, now)
            await _project(session, record, snapshot_id, context, now)
    except DBAPIError as error:
        reason = type(error.orig).__name__ if error.orig is not None else type(error).__name__
        raise IngestError(
            f"database rejected {record.resource_type}/{record.resource_id}: {reason}"
        ) from error
    return IngestOutcome(snapshot_id, snapshot_created, head_moved)


def _verify_content_hash(record: SourceRecord) -> None:
    recomputed = content_sha256(record.payload, record.system.kind)
    if not hmac.compare_digest(recomputed, record.content_sha256):
        raise IngestError(
            f"content hash does not match payload for {record.resource_type}/{record.resource_id}"
        )


async def _store_snapshot(
    session: AsyncSession, record: SourceRecord, context: IngestContext, now: datetime
) -> tuple[uuid.UUID, bool]:
    new_id = uuid.uuid4()
    sealed_payload = context.sealer.seal(
        record.payload,
        SealContext("source_record", "payload_enc", new_id),
    )
    inserted_id = await session.scalar(
        insert(StoredSourceRecord)
        .values(
            id=new_id,
            source_system_id=context.source_system_id,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            version_id=record.version_id,
            source_updated_at=record.source_updated_at,
            content_sha256=record.content_sha256,
            payload_enc=sealed_payload,
            patient_id=context.patient_id,
            import_run_id=context.import_run_id,
            fetched_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["source_system_id", "resource_type", "resource_id", "content_sha256"]
        )
        .returning(StoredSourceRecord.id)
    )
    if inserted_id is not None:
        return inserted_id, True

    existing_id = await session.scalar(
        select(StoredSourceRecord.id).where(
            StoredSourceRecord.source_system_id == context.source_system_id,
            StoredSourceRecord.resource_type == record.resource_type,
            StoredSourceRecord.resource_id == record.resource_id,
            StoredSourceRecord.content_sha256 == record.content_sha256,
        )
    )
    if existing_id is None:
        raise IngestError(
            f"snapshot of {record.resource_type}/{record.resource_id} vanished during ingest"
        )
    return existing_id, False


async def _move_head(
    session: AsyncSession,
    record: SourceRecord,
    context: IngestContext,
    snapshot_id: uuid.UUID,
    now: datetime,
) -> uuid.UUID | None:
    """Point the head at ``snapshot_id`` and return the snapshot it pointed at before."""
    head_key = (
        SourceResourceHead.source_system_id == context.source_system_id,
        SourceResourceHead.resource_type == record.resource_type,
        SourceResourceHead.resource_id == record.resource_id,
    )
    first_sighting = await session.scalar(
        insert(SourceResourceHead)
        .values(
            source_system_id=context.source_system_id,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            source_record_id=snapshot_id,
            last_seen_at=now,
            changed_at=now,
        )
        .on_conflict_do_nothing()
        .returning(SourceResourceHead.source_record_id)
    )
    if first_sighting is not None:
        return None

    previous = await session.scalar(
        select(SourceResourceHead.source_record_id).where(*head_key).with_for_update()
    )
    head_changes: dict[str, Any] = {"source_record_id": snapshot_id, "last_seen_at": now}
    if previous != snapshot_id:
        head_changes["changed_at"] = now
    await session.execute(update(SourceResourceHead).where(*head_key).values(**head_changes))
    return previous


async def _supersede_rows_of(
    session: AsyncSession, snapshot_id: uuid.UUID | None, now: datetime
) -> None:
    if snapshot_id is None:
        return
    await session.execute(
        update(TimelineEvent)
        .where(TimelineEvent.source_record_id == snapshot_id, TimelineEvent.superseded_at.is_(None))
        .values(superseded_at=now)
    )


async def _project(
    session: AsyncSession,
    record: SourceRecord,
    snapshot_id: uuid.UUID,
    context: IngestContext,
    now: datetime,
) -> None:
    drafts = context.projector(record)
    resource_label = f"{record.resource_type}/{record.resource_id}"
    paths = [draft.source_path for draft in drafts]
    if len(set(paths)) != len(paths):
        raise IngestError(f"projector emitted a duplicate source path for {resource_label}")
    if drafts and context.patient_id is None:
        raise IngestError(f"timeline rows for {resource_label} need a patient")

    if drafts:
        rows = [_timeline_row(draft, snapshot_id, context, now) for draft in drafts]
        statement = insert(TimelineEvent).values(rows)
        projected_columns = {
            name: statement.excluded[name]
            for name in rows[0]
            if name not in {"id", "source_record_id", "source_path"}
        }
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["source_record_id", "source_path"],
                set_={**projected_columns, "superseded_at": None},
            )
        )

    # A projector that no longer emits a path must not leave that path's row current.
    await session.execute(
        update(TimelineEvent)
        .where(
            TimelineEvent.source_record_id == snapshot_id,
            TimelineEvent.source_path.not_in(paths),
            TimelineEvent.superseded_at.is_(None),
        )
        .values(superseded_at=now)
    )


def _timeline_row(
    draft: TimelineEventDraft, snapshot_id: uuid.UUID, context: IngestContext, now: datetime
) -> dict[str, Any]:
    event_id = uuid.uuid5(TIMELINE_EVENT_ID_NAMESPACE, f"{snapshot_id}:{draft.source_path}")

    def seal(column: str, plaintext: bytes | None) -> bytes | None:
        if plaintext is None:
            return None
        return context.sealer.seal(plaintext, SealContext("timeline_event", column, event_id))

    occurred = draft.occurred
    period_end = draft.period_end
    return {
        "id": event_id,
        "patient_id": context.patient_id,
        "source_record_id": snapshot_id,
        "source_path": draft.source_path,
        "kind": draft.kind,
        "status": draft.status,
        "code_system": draft.code_system,
        "code": draft.code,
        "code_display": draft.code_display,
        "time_precision": occurred.precision if occurred else TimePrecision.UNKNOWN,
        "occurred_at": occurred.instant if occurred else None,
        "occurred_on": occurred.calendar_date if occurred else None,
        "occurred_raw": occurred.raw if occurred else None,
        "period_end_precision": period_end.precision if period_end else None,
        "period_end_at": period_end.instant if period_end else None,
        "period_end_on": period_end.calendar_date if period_end else None,
        "sort_at": draft.sort_at,
        "recorded_at": draft.recorded_at,
        "value_numeric": draft.value_numeric,
        "value_unit": draft.value_unit,
        "value_text_enc": seal(
            "value_text_enc", draft.value_text.encode("utf-8") if draft.value_text else None
        ),
        "ref_low": draft.ref_low,
        "ref_high": draft.ref_high,
        "ref_text": draft.ref_text,
        "source_interpretation": draft.source_interpretation,
        "detail_enc": seal("detail_enc", draft.detail_json),
        "projected_at": now,
    }
