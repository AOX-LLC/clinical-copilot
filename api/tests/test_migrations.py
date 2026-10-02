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
