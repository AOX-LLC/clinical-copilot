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

# Resources per type in the committed dataset (ADR 0015); together they are ADR 0012's 13,708.
RESOURCE_COUNTS = {
    "Patient": 28,
    "Encounter": 789,
    "Condition": 695,
    "Observation": 8910,
    "MedicationRequest": 855,
    "Procedure": 2202,
    "Immunization": 147,
    "AllergyIntolerance": 12,
    "CarePlan": 70,
}


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
