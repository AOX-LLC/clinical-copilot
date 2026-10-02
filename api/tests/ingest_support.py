"""Shared pieces for the ingest tests: the app-role engine, real crypto, a dataset adapter."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.crypto.keys import KeyMaterial
from app.crypto.runtime import FieldCrypto, build_field_crypto
from app.db import create_engine
from app.ehr.fhir_r4 import FhirR4Adapter
from app.ehr.ports import SourceSystemRef
from app.timeline.vocabulary import SourceKind
from tests.conftest import APP_ROLE
from tests.dataset_server import DatasetFhirTransport

SOURCE = SourceSystemRef(code="fhir-local", kind=SourceKind.FHIR_R4)
# Every table ingest writes except import_run, which gains a row per run by design.
STATE_TABLES = (
    "patient",
    "data_key",
    "patient_blind_index",
    "patient_source_link",
    "source_record",
    "source_resource_head",
    "timeline_event",
)
# Heads are touched on every sight; this is the one column a re-run may change.
VOLATILE_COLUMNS = {"source_resource_head": {"last_seen_at"}}


def new_key_material() -> KeyMaterial:
    return KeyMaterial(kek=os.urandom(32), kek_version=1, blind_index_key=os.urandom(32))


def real_crypto(material: KeyMaterial) -> FieldCrypto:
    return build_field_crypto(material)


def adapter_over(transport: DatasetFhirTransport) -> tuple[FhirR4Adapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport, base_url="http://fhir.test/fhir/r4")
    return FhirR4Adapter(SOURCE, client), client


@asynccontextmanager
async def app_role_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """The production engine, with every connection running as the application role."""
    engine = create_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _become_app_role(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {APP_ROLE}")
        cursor.close()

    try:
        yield engine
    finally:
        await engine.dispose()


async def table_digests(engine: AsyncEngine) -> dict[str, str]:
    """An order-independent digest of each state table, leaving out the volatile columns."""
    digests: dict[str, str] = {}
    async with engine.connect() as connection:
        for table in STATE_TABLES:
            columns = (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND table_name = :table"
                        " ORDER BY ordinal_position"
                    ),
                    {"table": table},
                )
            ).scalars()
            kept = [c for c in columns if c not in VOLATILE_COLUMNS.get(table, set())]
            selected = ", ".join(f'"{c}"' for c in kept)
            digests[table] = str(
                await connection.scalar(
                    text(
                        "SELECT md5(coalesce(string_agg(row::text, '|' ORDER BY row::text), ''))"  # noqa: S608
                        f" FROM (SELECT {selected} FROM {table}) AS row"
                    )
                )
            )
    return digests
