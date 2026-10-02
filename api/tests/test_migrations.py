import asyncio
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from app.timeline.models import Base
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

TIMELINE_TABLES = {
    "data_key",
    "import_run",
    "patient",
    "patient_blind_index",
    "patient_source_link",
    "source_record",
    "source_resource_head",
    "source_system",
    "timeline_event",
}


def _model_constraint_and_index_names() -> set[str]:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    ddl = [
        str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables
    ]
    ddl += [
        str(CreateIndex(index).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
        for index in table.indexes
    ]
    return {
        name for statement in ddl for name in re.findall(r"(?:CONSTRAINT|INDEX) (\w+)", statement)
    }


async def _database_constraint_and_index_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT conname AS name FROM pg_constraint"
                " WHERE connamespace = 'public'::regnamespace AND contype <> 'n'"
                " UNION SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        )
        names = {row.name for row in rows}
    await engine.dispose()
    return names


def _model_drift(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))


async def _public_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {row.tablename for row in rows}
    await engine.dispose()
    return tables


async def _drift(database_url: str) -> list[object]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        drift = await connection.run_sync(_model_drift)
    await engine.dispose()
    return drift


def test_upgrade_downgrade_upgrade_round_trips(empty_database_url: str) -> None:
    config = alembic_config(empty_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def test_migrations_create_exactly_what_the_models_declare(
    migrated_database_url: str,
) -> None:
    tables = await _public_tables(migrated_database_url)

    assert tables >= TIMELINE_TABLES
    assert await _drift(migrated_database_url) == []


async def test_constraint_and_index_names_match_the_models(migrated_database_url: str) -> None:
    # compare_metadata does not compare check constraints, so names are checked directly.
    database_names = await _database_constraint_and_index_names(migrated_database_url)

    assert _model_constraint_and_index_names() <= database_names
    assert {name for name in database_names if name != "alembic_version_pkc"} <= (
        _model_constraint_and_index_names()
    )


def test_downgrade_removes_the_timeline_schema(empty_database_url: str) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    # Alembic runs its own event loop, so this test stays synchronous.
    assert not TIMELINE_TABLES & asyncio.run(_public_tables(empty_database_url))


async def _timeline_kinds(database_url: str) -> list[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT unnest(enum_range(NULL::timeline_kind))::text AS kind")
        )
        kinds = [row.kind for row in rows]
    await engine.dispose()
    return kinds


async def _insert_care_plan_row(database_url: str) -> None:
    """One synthetic care-plan timeline row, with the rows it needs, inserted as the owner."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        patient_id = await connection.scalar(
            text("INSERT INTO patient DEFAULT VALUES RETURNING id")
        )
        snapshot_id = await connection.scalar(
            text(
                "INSERT INTO source_record (id, source_system_id, resource_type, resource_id,"
                " content_sha256, payload_enc, patient_id)"
                " SELECT gen_random_uuid(), id, 'CarePlan', 'care-plan-1', decode(repeat('ab', 32),"
                " 'hex'), '\\x00'::bytea, :patient FROM source_system LIMIT 1 RETURNING id"
            ),
            {"patient": patient_id},
        )
        await connection.execute(
            text(
                "INSERT INTO timeline_event (id, patient_id, source_record_id, kind,"
                " time_precision, sort_at) VALUES (gen_random_uuid(), :patient, :snapshot,"
                " 'care_plan', 'unknown', now())"
            ),
            {"patient": patient_id, "snapshot": snapshot_id},
        )
    await engine.dispose()


def test_care_plan_kind_is_added_and_removed_by_migration_0003(empty_database_url: str) -> None:
    config = alembic_config(empty_database_url)

    command.upgrade(config, "0003")
    assert "care_plan" in asyncio.run(_timeline_kinds(empty_database_url))
    command.downgrade(config, "0002")
    assert "care_plan" not in asyncio.run(_timeline_kinds(empty_database_url))
    command.upgrade(config, "head")


def test_downgrade_refuses_to_drop_the_kind_while_care_plan_rows_exist(
    empty_database_url: str,
) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")
    asyncio.run(_insert_care_plan_row(empty_database_url))

    with pytest.raises(RuntimeError, match="care_plan timeline rows exist"):
        command.downgrade(config, "0002")

    assert "care_plan" in asyncio.run(_timeline_kinds(empty_database_url))


async def _head_columns(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'source_resource_head'"
            )
        )
        columns = {row.column_name for row in rows}
    await engine.dispose()
    return columns


async def _tombstone_a_head(database_url: str) -> None:
    await _insert_care_plan_row(database_url)
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO source_resource_head (source_system_id, resource_type, resource_id,"
                " source_record_id, last_seen_at, changed_at, deleted_at)"
                " SELECT source_system_id, resource_type, resource_id, id, now(), now(), now()"
                " FROM source_record LIMIT 1"
            )
        )
    await engine.dispose()


def test_the_head_tombstone_is_added_and_removed_by_migration_0004(
    empty_database_url: str,
) -> None:
    config = alembic_config(empty_database_url)

    command.upgrade(config, "0004")
    assert "deleted_at" in asyncio.run(_head_columns(empty_database_url))
    command.downgrade(config, "0003")
    assert "deleted_at" not in asyncio.run(_head_columns(empty_database_url))
    command.upgrade(config, "head")


def test_downgrade_refuses_to_drop_the_tombstone_while_tombstoned_heads_exist(
    empty_database_url: str,
) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")
    asyncio.run(_tombstone_a_head(empty_database_url))

    with pytest.raises(RuntimeError, match="tombstoned source resources exist"):
        command.downgrade(config, "0003")

    assert "deleted_at" in asyncio.run(_head_columns(empty_database_url))
