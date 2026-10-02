"""Shared fixtures. Database tests run against throwaway databases on a real Postgres.

Set TEST_DATABASE_ADMIN_URL to an owner (superuser) URL on any Postgres with pgvector,
for example the Compose one on 127.0.0.1:4602. Each session creates its own databases
and drops them afterwards; the dev database is never touched. CI sets
REQUIRE_DB_TESTS=1 so a missing URL fails the run instead of skipping.
"""

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import MIGRATIONS_DIR

ADMIN_URL_VARIABLE = "TEST_DATABASE_ADMIN_URL"
APP_ROLE = "copilot_app"
MUTABLE_TABLES = (
    "timeline_event",
    "source_resource_head",
    "source_record",
    "patient_source_link",
    "patient",
    "import_run",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(ADMIN_URL_VARIABLE):
        return
    if os.environ.get("REQUIRE_DB_TESTS") == "1":
        raise pytest.UsageError(f"REQUIRE_DB_TESTS=1 but {ADMIN_URL_VARIABLE} is not set")
    skip_db = pytest.mark.skip(reason=f"set {ADMIN_URL_VARIABLE} to run database tests")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


def _admin_url() -> URL:
    return make_url(os.environ[ADMIN_URL_VARIABLE])


async def _run_as_admin(statements: list[str]) -> None:
    engine = create_async_engine(_admin_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


@contextmanager
def temporary_database() -> Iterator[str]:
    """Create an empty database (plus the app role if missing) and drop it afterwards."""
    name = f"clinical_copilot_test_{secrets.token_hex(4)}"
    ensure_app_role = (
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}')"
        f" THEN CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$"
    )
    asyncio.run(_run_as_admin([ensure_app_role, f'CREATE DATABASE "{name}"']))
    try:
        yield _admin_url().set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_run_as_admin([f'DROP DATABASE "{name}" WITH (FORCE)']))


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser treats % as interpolation; escape it in passwords.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    with temporary_database() as database_url:
        command.upgrade(alembic_config(database_url), "head")
        yield database_url


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    with temporary_database() as database_url:
        yield database_url


@pytest.fixture
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    """An owner engine on the migrated database; tables are emptied after each test."""
    test_engine = create_async_engine(
        migrated_database_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {', '.join(MUTABLE_TABLES)} CASCADE"))
        await test_engine.dispose()
