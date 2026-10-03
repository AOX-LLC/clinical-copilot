"""The committed synthetic dataset as test fixtures: real resources, read by id."""

import gzip
import json
from collections import Counter
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

from app.ehr.ports import SourceRecord
from tests.fixtures import FAKE_SOURCE

DATASET_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "synthea"
PATIENT_FILES = sorted((DATASET_DIRECTORY / "patients").glob("*.json.gz"))

# What the dataset holds and what ingest projects from it. The stack-smoke CI job reads the same
# file, so the tests here and the running stack are held to one set of numbers.
_EXPECTED = json.loads((DATASET_DIRECTORY / "expected-counts.json").read_text(encoding="utf-8"))
PATIENT_COUNT: int = _EXPECTED["patients"]
RESOURCE_COUNTS: dict[str, int] = _EXPECTED["resources_by_type"]
TIMELINE_COUNTS: dict[str, int] = _EXPECTED["timeline_rows_by_kind"]
RESOURCE_TOTAL = sum(RESOURCE_COUNTS.values())
TIMELINE_TOTAL = sum(TIMELINE_COUNTS.values())


def patient_resources() -> Iterator[list[dict[str, Any]]]:
    """Each patient's resources, one list per bundle, in file order."""
    for path in PATIENT_FILES:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield [entry["resource"] for entry in json.load(handle)["entry"]]


@cache
def _by_key() -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["resourceType"], r["id"]): r for resources in patient_resources() for r in resources}


def dataset_resource(resource_type: str, resource_id: str) -> dict[str, Any]:
    resource: dict[str, Any] = json.loads(json.dumps(_by_key()[(resource_type, resource_id)]))
    return resource


def record_of(resource: dict[str, Any], version_id: str = "1") -> SourceRecord:
    payload = json.dumps(resource, separators=(",", ":")).encode()
    return SourceRecord.from_payload(
        FAKE_SOURCE, resource["resourceType"], resource["id"], version_id, None, payload
    )


def dataset_record(resource_type: str, resource_id: str) -> SourceRecord:
    return record_of(dataset_resource(resource_type, resource_id))


def count_by_type() -> Counter[str]:
    return Counter(r["resourceType"] for resources in patient_resources() for r in resources)
