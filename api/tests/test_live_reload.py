"""Wipe the running FHIR server, reload the dataset, and expect the same content hashes.

Needs the Compose stack and the Docker CLI: it restarts the ``fhir`` service, which keeps
its data in memory, so the store really is emptied. Set ``LIVE_FHIR_BASE_URL`` (and
``COMPOSE_PROJECT_NAME`` if the stack runs under another project name). The ingest test also
needs ``TEST_DATABASE_ADMIN_URL``: it ingests into a throwaway database, never the stack's.
"""

import asyncio
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ehr.fhir_r4 import FhirR4Adapter
from app.ehr.ports import EhrAdapter, RecordKind, SourceSystemRef
from app.fhir_seed.load import load_dataset
from app.ingest.run import IngestSummary, run_ingest
from app.timeline.vocabulary import ImportStatus, SourceKind
from tests.ingest_support import app_role_engine, new_key_material, real_crypto, table_digests

pytestmark = pytest.mark.live
CLINIC_ZONE = ZoneInfo("America/New_York")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPOSITORY_ROOT / "data" / "synthea"
SOURCE = SourceSystemRef(code="fhir-local", kind=SourceKind.FHIR_R4)
READY_TIMEOUT_SECONDS = 120.0
ALL_KINDS = frozenset(RecordKind) - {RecordKind.DOCUMENT, RecordKind.APPOINTMENT}


async def _content_hashes(adapter: EhrAdapter) -> dict[tuple[str, str], bytes]:
    """Every record of every patient, keyed by resource, valued by its canonical hash."""
    hashes: dict[tuple[str, str], bytes] = {}
    patient_cursor: str | None = None
    while True:
        patients = await adapter.list_patients(patient_cursor, page_size=50)
        for patient in patients.items:
            record_cursor: str | None = None
            while True:
                page = await adapter.fetch_changes(
                    patient.external_id, ALL_KINDS, None, record_cursor
                )
                for record in page.items:
                    hashes[(record.resource_type, record.resource_id)] = record.content_sha256
                record_cursor = page.next_cursor
                if record_cursor is None:
                    break
        patient_cursor = patients.next_cursor
        if patient_cursor is None:
            return hashes


def _wait_until_healthy(base_url: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/metadata", timeout=5.0).is_success:
                return
        except httpx.TransportError:
            pass
        time.sleep(1.0)
    pytest.fail("the FHIR server did not come back after the restart")


def _patient_count(base_url: str) -> int:
    response = httpx.get(f"{base_url}/Patient", params={"_summary": "count"}, timeout=30.0)
    return int(response.json()["total"])


async def test_a_wiped_and_reloaded_server_serves_identical_content() -> None:
    base_url = os.environ["LIVE_FHIR_BASE_URL"]
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
        adapter = FhirR4Adapter(SOURCE, client)
        before = await _content_hashes(adapter)
        assert before

        restart = await asyncio.create_subprocess_exec(
            "docker",
            *["compose", "restart", "fhir"],
            cwd=REPOSITORY_ROOT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await restart.wait() == 0
        _wait_until_healthy(base_url)
        assert _patient_count(base_url) == 0, "the restart did not empty the server"

        await load_dataset(client, DATASET)
        after = await _content_hashes(adapter)

    assert after == before


async def _heads(engine: AsyncEngine) -> list[tuple[object, ...]]:
    """What each head points at and when it last moved; ``last_seen_at`` may change freely."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT resource_type, resource_id, source_record_id, changed_at"
                " FROM source_resource_head ORDER BY resource_type, resource_id"
            )
        )
        return [tuple(row) for row in rows]


async def _restart_and_reseed(client: httpx.AsyncClient, base_url: str) -> None:
    restart = await asyncio.create_subprocess_exec(
        "docker",
        *["compose", "restart", "fhir"],
        cwd=REPOSITORY_ROOT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await restart.wait() == 0
    _wait_until_healthy(base_url)
    assert _patient_count(base_url) == 0, "the restart did not empty the server"
    await load_dataset(client, DATASET)


@pytest.mark.db
async def test_a_wiped_and_reseeded_server_ingests_to_zero_new_snapshots(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    base_url = os.environ["LIVE_FHIR_BASE_URL"]
    crypto = real_crypto(new_key_material())
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=120.0) as client,
        app_role_engine(migrated_database_url) as app_engine,
    ):
        adapter = FhirR4Adapter(SOURCE, client)
        first: IngestSummary = await run_ingest(app_engine, adapter, crypto, CLINIC_ZONE)
        heads_before, state_before = await _heads(engine), await table_digests(engine)
        assert first.succeeded
        assert first.snapshots_created == first.records_seen > 0

        await _restart_and_reseed(client, base_url)
        second = await run_ingest(app_engine, adapter, crypto, CLINIC_ZONE)

    assert second.status is ImportStatus.SUCCEEDED
    assert second.records_seen == first.records_seen
    assert second.snapshots_created == 0
    assert second.heads_moved == 0
    assert await _heads(engine) == heads_before, "a head moved or was re-created"
    assert await table_digests(engine) == state_before
