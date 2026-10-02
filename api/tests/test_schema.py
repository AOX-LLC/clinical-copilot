"""Schema guarantees that application code cannot bypass."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import CursorResult, TextClause, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import APP_ROLE

pytestmark = pytest.mark.db

INSERT_SNAPSHOT = text(
    "INSERT INTO source_record"
    " (id, source_system_id, resource_type, resource_id, content_sha256, payload_enc)"
    " SELECT :id, id, 'Observation', 'obs-1', :digest, 'test-sealed:x'"
    " FROM source_system WHERE code = 'fhir-local'"
)
INSERT_HEAD = text(
    "INSERT INTO source_resource_head"
    " (source_system_id, resource_type, resource_id, source_record_id, last_seen_at, changed_at)"
    " SELECT source_system_id, resource_type, resource_id, id, now(), now()"
    " FROM source_record WHERE id = :id"
)


async def _snapshot_with_head_as_app(engine: AsyncEngine) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        await connection.execute(INSERT_SNAPSHOT, {"id": snapshot_id, "digest": bytes(32)})
        await connection.execute(INSERT_HEAD, {"id": snapshot_id})
    return snapshot_id


INSERT_HEAD_FOR_OTHER_RESOURCE = text(
    "INSERT INTO source_resource_head"
    " (source_system_id, resource_type, resource_id, source_record_id, last_seen_at, changed_at)"
    " SELECT source_system_id, resource_type, 'obs-2', id, now(), now()"
    " FROM source_record WHERE id = :id"
)
INSERT_TIMELINE_EVENT = text(
    "INSERT INTO timeline_event (id, patient_id, source_record_id, kind, time_precision,"
    " occurred_at, occurred_on, sort_at)"
    " SELECT gen_random_uuid(), patient.id, :snapshot_id, 'lab',"
    " CAST(:precision AS time_precision), :occurred_at, :occurred_on, now()"
    " FROM patient LIMIT 1"
)


async def _execute(
    engine: AsyncEngine, statement: TextClause, parameters: dict[str, object], *, as_app: bool
) -> CursorResult[Any]:
    async with engine.begin() as connection:
        if as_app:
            await connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        return await connection.execute(statement, parameters)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE source_record SET version_id = 'tampered'", id="update"),
        pytest.param("DELETE FROM source_record", id="delete"),
        pytest.param("TRUNCATE source_record CASCADE", id="truncate"),
    ],
)
async def test_the_app_role_cannot_change_or_remove_a_snapshot(
    engine: AsyncEngine, statement: str
) -> None:
    await _snapshot_with_head_as_app(engine)

    with pytest.raises(DBAPIError, match="permission denied"):
        await _execute(engine, text(statement), {}, as_app=True)


async def test_the_app_role_can_move_a_head(engine: AsyncEngine) -> None:
    await _snapshot_with_head_as_app(engine)

    moved = await _execute(
        engine, text("UPDATE source_resource_head SET last_seen_at = now()"), {}, as_app=True
    )

    assert moved.rowcount == 1


async def test_a_head_cannot_point_at_another_resources_snapshot(engine: AsyncEngine) -> None:
    snapshot_id = await _snapshot_with_head_as_app(engine)

    with pytest.raises(IntegrityError, match="fk_source_resource_head_snapshot"):
        await _execute(engine, INSERT_HEAD_FOR_OTHER_RESOURCE, {"id": snapshot_id}, as_app=True)


async def test_the_same_content_cannot_be_stored_twice(engine: AsyncEngine) -> None:
    await _snapshot_with_head_as_app(engine)

    with pytest.raises(IntegrityError, match="uq_source_record_content"):
        await _execute(
            engine, INSERT_SNAPSHOT, {"id": uuid.uuid4(), "digest": bytes(32)}, as_app=True
        )


NOW = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("precision", "occurred_at", "occurred_on", "violated"),
    [
        pytest.param("instant", None, None, "instant_has_occurred_at", id="instant without time"),
        pytest.param(
            "day", None, None, "calendar_precision_has_occurred_on", id="day without date"
        ),
        pytest.param("day", NOW, NOW.date(), "instant_has_occurred_at", id="day with a time too"),
        pytest.param(
            "unknown",
            None,
            NOW.date(),
            "calendar_precision_has_occurred_on",
            id="unknown with a date",
        ),
    ],
)
async def test_clinical_time_columns_match_their_precision(
    engine: AsyncEngine,
    precision: str,
    occurred_at: datetime | None,
    occurred_on: date | None,
    violated: str,
) -> None:
    snapshot_id = await _snapshot_with_head_as_app(engine)
    await _execute(engine, text("INSERT INTO patient DEFAULT VALUES"), {}, as_app=True)
    row = {
        "snapshot_id": snapshot_id,
        "precision": precision,
        "occurred_at": occurred_at,
        "occurred_on": occurred_on,
    }

    with pytest.raises(IntegrityError, match=violated):
        await _execute(engine, INSERT_TIMELINE_EVENT, row, as_app=True)
