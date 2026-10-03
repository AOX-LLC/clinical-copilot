#!/usr/bin/env python3
"""Check a freshly seeded and ingested stack against data/synthea/expected-counts.json.

Run from the repository root once `docker compose up -d --build --wait` has finished. It reads the
FHIR server and the database (through `docker compose exec`) and prints every count that differs
from the committed expectation, so a failure names the kind that drifted instead of a bare exit 1.
Standard library only, so it runs on a CI runner or a laptop without the API's environment.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

EXPECTED_FILE = Path(__file__).resolve().parents[1] / "data" / "synthea" / "expected-counts.json"
FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "http://127.0.0.1:4603/fhir/r4")
DATABASE_USER = os.environ.get("CHECK_DATABASE_USER", "copilot_owner")
DATABASE_NAME = os.environ.get("CHECK_DATABASE_NAME", "clinical_copilot")

LATEST_IMPORT_STATUS = "SELECT status FROM import_run ORDER BY started_at DESC LIMIT 1"
PATIENTS = "SELECT count(*) FROM patient"
LIVE_HEADS_BY_TYPE = (
    "SELECT resource_type, count(*) FROM source_resource_head WHERE deleted_at IS NULL GROUP BY 1"
)
CURRENT_ROWS_BY_KIND = (
    "SELECT kind::text, count(*) FROM timeline_event WHERE superseded_at IS NULL GROUP BY 1"
)


def query(statement: str) -> list[list[str]]:
    """Rows of a query run as the owner role inside the postgres container."""
    # A fixed argument list with no shell; the statements are the constants above.
    output = subprocess.run(  # noqa: S603
        [  # noqa: S607  # docker is found on PATH, as on any runner or laptop
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            DATABASE_USER,
            "-d",
            DATABASE_NAME,
            "-tAF",
            "\t",
            "-c",
            statement,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.split("\t") for line in output.splitlines() if line]


def counts_by_label(statement: str) -> dict[str, int]:
    return {label: int(count) for label, count in query(statement)}


def fhir_patient_total() -> int:
    if not FHIR_BASE_URL.startswith(("http://", "https://")):
        raise SystemExit(f"FHIR_BASE_URL must be an http(s) URL, not {FHIR_BASE_URL!r}")
    url = f"{FHIR_BASE_URL}/Patient?_summary=count"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310  # scheme checked above
        return int(json.load(response)["total"])


def differences(name: str, expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    return [
        f"{name} {label}: expected {expected.get(label, 0)}, found {actual.get(label, 0)}"
        for label in sorted(expected.keys() | actual.keys())
        if expected.get(label, 0) != actual.get(label, 0)
    ]


def main() -> int:
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    patients = expected["patients"]
    problems: list[str] = []

    status = query(LATEST_IMPORT_STATUS)
    if status != [["succeeded"]]:
        problems.append(f"latest import run: expected succeeded, found {status or 'no run'}")
    if (fhir_patients := fhir_patient_total()) != patients:
        problems.append(f"FHIR patients: expected {patients}, found {fhir_patients}")
    if (stored_patients := int(query(PATIENTS)[0][0])) != patients:
        problems.append(f"stored patients: expected {patients}, found {stored_patients}")
    problems += differences(
        "resources", expected["resources_by_type"], counts_by_label(LIVE_HEADS_BY_TYPE)
    )
    problems += differences(
        "timeline rows", expected["timeline_rows_by_kind"], counts_by_label(CURRENT_ROWS_BY_KIND)
    )

    if problems:
        for problem in problems:
            print(f"::error::{problem}" if os.environ.get("GITHUB_ACTIONS") else problem)
        print(
            f"Update {EXPECTED_FILE.name} only if the dataset or a normalizer changed on purpose."
        )
        return 1

    resources = sum(expected["resources_by_type"].values())
    rows = sum(expected["timeline_rows_by_kind"].values())
    print(
        f"Stack matches {EXPECTED_FILE.name}: {patients} patients, {resources} resources, "
        f"{rows} current timeline rows."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
