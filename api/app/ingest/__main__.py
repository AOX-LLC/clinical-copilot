"""Command line for ingest.

    python -m app.ingest

Reads ``DATABASE_URL``, ``FHIR_BASE_URL``, ``CLINIC_TIMEZONE``, ``FIELD_KEK`` and
``BLIND_INDEX_KEY`` (or their ``_FILE`` forms) from the environment, ingests every patient
the FHIR server lists, and exits non-zero if any patient failed or the run could not start.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Sequence

import httpx

from app.config import Settings
from app.crypto.errors import CryptoError
from app.crypto.keys import load_key_material
from app.crypto.runtime import build_field_crypto
from app.db import create_engine
from app.ehr.fhir_r4 import FhirR4Adapter
from app.ehr.ports import EhrAdapterError, SourceSystemRef
from app.ingest.run import (
    MAX_CONCURRENCY,
    IngestBusyError,
    IngestSummary,
    UnknownSourceError,
    run_ingest,
)
from app.timeline.vocabulary import SourceKind

DEFAULT_FHIR_BASE_URL = "http://fhir:5826/fhir/r4"
SOURCE_CODE = "fhir-local"
REQUEST_TIMEOUT_SECONDS = 120.0

logger = logging.getLogger("app.ingest")


class ConfigError(Exception):
    """A setting is missing or out of range."""


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO
    try:
        summary = asyncio.run(_run())
    except (
        CryptoError,
        EhrAdapterError,
        ConfigError,
        IngestBusyError,
        UnknownSourceError,
    ) as error:
        logger.error("%s", error)
        return 1
    _report(summary)
    return 0 if summary.succeeded else 1


async def _run() -> IngestSummary:
    settings = Settings()
    crypto = build_field_crypto(load_key_material(os.environ))
    concurrency = _concurrency(os.environ.get("INGEST_CONCURRENCY"))
    base_url = os.environ.get("FHIR_BASE_URL", DEFAULT_FHIR_BASE_URL)
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            adapter = FhirR4Adapter(SourceSystemRef(SOURCE_CODE, SourceKind.FHIR_R4), client)
            return await run_ingest(engine, adapter, crypto, settings.clinic_zone, concurrency)
    finally:
        await engine.dispose()


def _concurrency(raw: str | None) -> int:
    if raw is None or raw == "":
        return MAX_CONCURRENCY
    if not raw.isdecimal() or not 1 <= int(raw) <= MAX_CONCURRENCY:
        raise ConfigError(f"INGEST_CONCURRENCY must be a whole number from 1 to {MAX_CONCURRENCY}")
    return int(raw)


def _report(summary: IngestSummary) -> None:
    by_type = ", ".join(
        f"{name} {count}" for name, count in sorted(summary.records_by_type.items())
    )
    logger.info(
        "ingest %s: %d patients (%d new), %d records, %d snapshots created, %d heads moved, "
        "%d tombstoned, %d skipped (key destroyed), %.1f s, peak RSS %.0f MiB",
        summary.status.value,
        summary.patients,
        summary.patients_created,
        summary.records_seen,
        summary.snapshots_created,
        summary.heads_moved,
        summary.tombstoned,
        summary.patients_skipped,
        summary.seconds,
        summary.peak_rss_mib,
    )
    logger.info(
        "records by type: %s; reading the source %.1f s and writing %.1f s, summed over patients",
        by_type,
        summary.fetch_seconds,
        summary.write_seconds,
    )
    if summary.failures:
        logger.error("%d patients failed", len(summary.failures))


if __name__ == "__main__":
    sys.exit(main())
