"""Command line for the synthetic dataset.

    python -m app.fhir_seed prepare RAW_DIR OUT_DIR   trim Synthea output into the committed dataset
    python -m app.fhir_seed load DATA_DIR             load the dataset into the FHIR server

``load`` reads the server address from ``FHIR_BASE_URL`` and exits non-zero on any failure.
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from app.fhir_seed.load import (
    PATIENT_DIRECTORY,
    REQUEST_TIMEOUT_SECONDS,
    SHARED_FILE,
    load_dataset,
)
from app.fhir_seed.transform import SeedError, trim_bundle

DEFAULT_FHIR_BASE_URL = "http://fhir:5826/fhir/r4"
MANIFEST_FILE = "MANIFEST.sha256"
SHARED_MARKER = "Information"

logger = logging.getLogger("app.fhir_seed")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepare_dataset(arguments.raw_dir, arguments.out_dir)
        else:
            asyncio.run(_load(arguments.data_dir))
    except SeedError as error:
        logger.error("%s", error)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.fhir_seed")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="trim raw Synthea output into the dataset")
    prepare.add_argument("raw_dir", type=Path)
    prepare.add_argument("out_dir", type=Path)
    load = commands.add_parser("load", help="load the dataset into the FHIR server")
    load.add_argument("data_dir", type=Path)
    return parser


async def _load(data_dir: Path) -> None:
    base_url = os.environ.get("FHIR_BASE_URL", DEFAULT_FHIR_BASE_URL)
    async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        summary = await load_dataset(client, data_dir)
    logger.info("seeded %d patients, %d resources", summary.patients, summary.resources)


def prepare_dataset(raw_dir: Path, out_dir: Path) -> None:
    """Write the trimmed dataset as reproducible gzip files plus a checksum manifest."""
    raw_files = sorted(raw_dir.glob("*.json"))
    shared_files = [path for path in raw_files if SHARED_MARKER in path.name]
    patient_raw_files = [path for path in raw_files if SHARED_MARKER not in path.name]
    if not shared_files or not patient_raw_files:
        raise SeedError("the raw directory needs shared and patient bundles")

    (out_dir / PATIENT_DIRECTORY).mkdir(parents=True, exist_ok=True)
    shared_entries = [
        entry for path in shared_files for entry in trim_bundle(_read_json(path))["entry"]
    ]
    shared_entries.sort(key=lambda entry: entry["fullUrl"])
    _write_reproducible(
        out_dir / SHARED_FILE,
        {"resourceType": "Bundle", "type": "transaction", "entry": shared_entries},
    )
    for path in patient_raw_files:
        bundle = trim_bundle(_read_json(path))
        patient_id = _patient_id(bundle)
        _write_reproducible(out_dir / PATIENT_DIRECTORY / f"{patient_id}.json.gz", bundle)
    _write_manifest(out_dir)
    logger.info("prepared %d patient bundles in %s", len(patient_raw_files), out_dir)


def _read_json(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _patient_id(bundle: dict[str, Any]) -> str:
    ids = [
        e["resource"]["id"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    if len(ids) != 1:
        raise SeedError(f"a patient bundle must hold exactly one Patient, found {len(ids)}")
    return str(ids[0])


def _write_reproducible(path: Path, document: dict[str, Any]) -> None:
    """Compact, sorted-key JSON in a gzip stream with no name or timestamp: same bytes every run."""
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as stream,
    ):
        stream.write(payload)


def _write_manifest(out_dir: Path) -> None:
    """Hash the decompressed JSON: gzip bytes can differ between zlib versions, content cannot."""
    lines = [
        f"{_content_sha256(path)}  {path.relative_to(out_dir).as_posix()}"
        for path in sorted(out_dir.rglob("*.json.gz"))
    ]
    (out_dir / MANIFEST_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _content_sha256(path: Path) -> str:
    with gzip.open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
