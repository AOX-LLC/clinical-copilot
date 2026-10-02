"""Record a small set of synthetic patients from a running FHIR server into the offline fixture.

    cd api
    uv run python -m tests.recorded.record http://127.0.0.1:4603/fhir/r4

Each resource is stored exactly as a direct read returned it, one per line, so number tokens
and key order are the server's own. The recording keeps whole patients (every kind) but caps
the bulky kinds, so the file stays small. The patients picked are the first ones, by id, that
have every kind of record; pass ids to choose others.
"""

import sys
from pathlib import Path

import httpx

from app.ehr.fhir_r4 import RESOURCE_TYPES_BY_KIND

FIXTURE = Path(__file__).parent / "fhir_r4" / "resources.ndjson"
PATIENT_COUNT = 3
CAPS = {"Observation": 12, "Procedure": 4, "Encounter": 4, "Condition": 4, "MedicationRequest": 6}
CLINICAL_TYPES = [t for types in RESOURCE_TYPES_BY_KIND.values() for t in types if t != "Patient"]


def main(base_url: str, patient_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        chosen = patient_ids or _pick_patients(client)
        lines: list[bytes] = []
        for patient_id in chosen:
            lines.append(_read(client, f"/Patient/{patient_id}"))
            for resource_type in CLINICAL_TYPES:
                found = _search(client, resource_type, patient_id)
                for resource_id in sorted(found)[: CAPS.get(resource_type, len(found))]:
                    lines.append(_read(client, f"/{resource_type}/{resource_id}"))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(b"\n".join(lines) + b"\n")
    print(f"recorded {len(lines)} resources for {len(chosen)} patients into {FIXTURE}")


def _pick_patients(client: httpx.Client) -> list[str]:
    patients = sorted(e["resource"]["id"] for e in client.get("/Patient").json()["entry"])
    complete = [pid for pid in patients if _has_every_kind(client, pid)]
    return complete[:PATIENT_COUNT]


def _has_every_kind(client: httpx.Client, patient_id: str) -> bool:
    needed = ["Encounter", "Condition", "Observation", "MedicationRequest", "Procedure"]
    needed += ["Immunization", "AllergyIntolerance", "CarePlan"]
    return all(_search(client, resource_type, patient_id) for resource_type in needed)


def _search(client: httpx.Client, resource_type: str, patient_id: str) -> list[str]:
    bundle = client.get(f"/{resource_type}", params={"patient": patient_id}).json()
    return [entry["resource"]["id"] for entry in bundle.get("entry", [])]


def _read(client: httpx.Client, path: str) -> bytes:
    response = client.get(path)
    response.raise_for_status()
    body = response.content.strip()
    if b"\n" in body:
        # The fixture is one resource per line; never re-serialize to make a body fit.
        raise SystemExit(
            f"{path}: the server returned multi-line JSON; the recorder needs a new format"
        )
    return body


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
