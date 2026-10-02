"""Synthetic test data. Nothing here describes a real person."""

from datetime import UTC, datetime
from typing import Any

from app.ehr.fake import FakeAdapter
from app.ehr.ports import RecordKind, SourceSystemRef
from app.timeline.vocabulary import SourceKind

FAKE_SOURCE = SourceSystemRef(code="fhir-local", kind=SourceKind.FHIR_R4)
NOTIFICATION_SECRET = b"test-only-notification-secret"
SOURCE_CLOCK_START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# A distinctive synthetic value placed in every payload; it must never surface in logs,
# exception messages or reprs.
SENTINEL_FAMILY_NAME = "Quillfeather-Sentinel"


def synthetic_patient(patient_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"family": SENTINEL_FAMILY_NAME, "given": ["Test"]}],
        "birthDate": "1970-01-01",
    }


def synthetic_hba1c(observation_id: str, patient_id: str, value: str, day: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": observation_id,
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
        "subject": {"reference": f"Patient/{patient_id}", "display": SENTINEL_FAMILY_NAME},
        "effectiveDateTime": day,
        "valueQuantity": {"value": float(value), "unit": "%"},
    }


def populated_fake_adapter() -> FakeAdapter:
    """Three patients, each with three HbA1c results: more than one page everywhere."""
    adapter = FakeAdapter(FAKE_SOURCE, NOTIFICATION_SECRET, SOURCE_CLOCK_START)
    for patient_number in range(1, 4):
        patient_id = f"patient-{patient_number}"
        adapter.put(RecordKind.PATIENT, synthetic_patient(patient_id), patient_id)
        for result_number, (value, day) in enumerate(
            [("5.4", "2026-01-15"), ("5.9", "2026-04-15"), ("6.1", "2026-07-15")], start=1
        ):
            observation = synthetic_hba1c(
                f"obs-{patient_number}-{result_number}", patient_id, value, day
            )
            adapter.put(RecordKind.OBSERVATION, observation, patient_id)
    return adapter
