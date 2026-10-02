"""Load the committed synthetic dataset into the local FHIR server.

fhir-candle keeps its data in memory, so this runs on every ``docker compose up``. The
shared bundle (practitioners, organizations, locations) goes first, then one transaction
per patient. A transaction is atomic on the server, so a failed patient leaves nothing
half loaded and the whole load can simply be run again.

Logs and errors carry counts, file labels and HTTP statuses, never resource content.
"""

import asyncio
import gzip
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.fhir_seed.transform import IdentifierIndex, SeedError, to_put_bundle

logger = logging.getLogger(__name__)

SHARED_FILE = "shared.json.gz"
PATIENT_DIRECTORY = "patients"
MAX_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 1.0
READY_TIMEOUT_SECONDS = 120.0
READY_POLL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class LoadSummary:
    patients: int
    resources: int


def read_bundle(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rb") as stream:
        bundle: dict[str, Any] = json.load(stream)
    return bundle


def patient_files(data_dir: Path) -> list[Path]:
    return sorted((data_dir / PATIENT_DIRECTORY).glob("*.json.gz"))


async def load_dataset(client: httpx.AsyncClient, data_dir: Path) -> LoadSummary:
    shared_bundle = read_bundle(data_dir / SHARED_FILE)
    shared_index = IdentifierIndex.from_bundles([shared_bundle])
    files = patient_files(data_dir)
    if not files:
        raise SeedError(f"no patient bundles found under {PATIENT_DIRECTORY}/")

    await wait_until_ready(client)
    resources = await _post_transaction(
        client, to_put_bundle(shared_bundle, shared_index), "shared"
    )
    for path in files:
        label = f"{PATIENT_DIRECTORY}/{path.name}"
        bundle = to_put_bundle(read_bundle(path), shared_index)
        resources += await _post_transaction(client, bundle, label)
        logger.info("loaded %s", label)

    await _verify_patient_count(client, minimum=len(files))
    return LoadSummary(patients=len(files), resources=resources)


async def wait_until_ready(client: httpx.AsyncClient) -> None:
    """Poll the capability statement until the server answers; Compose already waits for health."""
    waited = 0.0
    while True:
        try:
            response = await client.get("/metadata", timeout=10.0)
            if response.status_code == httpx.codes.OK:
                return
        except httpx.TransportError:
            pass
        if waited >= READY_TIMEOUT_SECONDS:
            raise SeedError("the FHIR server did not become ready in time")
        await asyncio.sleep(READY_POLL_SECONDS)
        waited += READY_POLL_SECONDS


async def _post_transaction(
    client: httpx.AsyncClient, bundle: Mapping[str, Any], label: str
) -> int:
    last_problem = "no attempt made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.post(
                "/", json=bundle, headers={"Content-Type": "application/fhir+json"}
            )
        except httpx.TransportError as error:
            last_problem = f"transport error ({type(error).__name__})"
        else:
            if response.is_success:
                return _count_created(response, bundle, label)
            if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                raise SeedError(
                    f"{label}: the server refused the transaction ({response.status_code})"
                )
            last_problem = f"server error ({response.status_code})"
        logger.warning(
            "%s: attempt %d of %d failed: %s", label, attempt, MAX_ATTEMPTS, last_problem
        )
        await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
    raise SeedError(f"{label}: gave up after {MAX_ATTEMPTS} attempts: {last_problem}")


def _count_created(response: httpx.Response, bundle: Mapping[str, Any], label: str) -> int:
    expected = len(bundle["entry"])
    entries = _json_of(response).get("entry")
    statuses = [_status_of(entry) for entry in entries] if isinstance(entries, list) else None
    if statuses is None or None in statuses:
        raise SeedError(f"{label}: the server's answer is not a transaction response")
    failed = [status for status in statuses if not str(status).startswith(("200", "201"))]
    if len(statuses) != expected or failed:
        raise SeedError(f"{label}: {len(failed)} of {expected} entries were not stored")
    return expected


def _status_of(entry: object) -> object:
    response = entry.get("response") if isinstance(entry, dict) else None
    return response.get("status") if isinstance(response, dict) else None


def _json_of(response: httpx.Response) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


async def _verify_patient_count(client: httpx.AsyncClient, minimum: int) -> None:
    response = await client.get("/Patient", params={"_summary": "count"})
    total = _json_of(response).get("total")
    if not response.is_success or not isinstance(total, int):
        raise SeedError("the server did not answer the patient count check")
    if total < minimum:
        raise SeedError(f"expected at least {minimum} patients on the server, found {total}")
