"""Projectors: pure functions from a source record to the timeline rows it yields.

``build_projector`` returns the ``TimelineProjector`` ingest calls for each record. It knows
the nine FHIR resource types the product reads; a type it does not know raises, so a new
type can never be silently dropped. Patient yields no rows (it feeds the patient's identity).
"""

from collections.abc import Callable, Sequence
from zoneinfo import ZoneInfo

from app.ehr.ports import SourceRecord
from app.timeline.ingest import TimelineEventDraft, TimelineProjector
from app.timeline.normalize._fhir import Context, Json, NormalizationError, parse_resource
from app.timeline.normalize.allergy import project_allergy
from app.timeline.normalize.care_plan import project_care_plan
from app.timeline.normalize.condition import project_condition
from app.timeline.normalize.encounter import project_encounter
from app.timeline.normalize.immunization import project_immunization
from app.timeline.normalize.medication import (
    project_medication_request,
    project_medication_statement,
)
from app.timeline.normalize.observation import project_observation
from app.timeline.normalize.patient import project_patient
from app.timeline.normalize.procedure import project_procedure

__all__ = ["NormalizationError", "build_projector", "parse_resource"]

Normalizer = Callable[[Json, Context], Sequence[TimelineEventDraft]]

NORMALIZERS: dict[str, Normalizer] = {
    "Patient": project_patient,
    "Encounter": project_encounter,
    "Condition": project_condition,
    "Observation": project_observation,
    "MedicationRequest": project_medication_request,
    "MedicationStatement": project_medication_statement,
    "Procedure": project_procedure,
    "Immunization": project_immunization,
    "AllergyIntolerance": project_allergy,
    "CarePlan": project_care_plan,
}


def build_projector(clinic_zone: ZoneInfo) -> TimelineProjector:
    def project(record: SourceRecord) -> Sequence[TimelineEventDraft]:
        label = f"{record.resource_type}/{record.resource_id}"
        normalize = NORMALIZERS.get(record.resource_type)
        if normalize is None:
            raise NormalizationError(f"no normalizer for resource type {record.resource_type}")
        context = Context(label, clinic_zone, record.source_updated_at)
        return normalize(parse_resource(record.payload, label), context)

    return project
