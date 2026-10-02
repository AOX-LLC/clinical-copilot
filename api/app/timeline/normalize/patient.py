"""Patient: no timeline rows. The record feeds the patient's sealed identity."""

from collections.abc import Sequence
from datetime import date

from app.timeline.clinical_time import ClinicalTimeError, parse_fhir_datetime
from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import Context, Json, NormalizationError
from app.timeline.patient_identity import PatientIdentifier, PatientIdentity
from app.timeline.vocabulary import TimePrecision

BIRTH_SEX_URL = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-birthsex"
SEX_BY_BIRTHSEX = {"M": "male", "F": "female"}


def project_patient(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    return []


def identity_from_fhir_patient(resource: Json, label: str) -> PatientIdentity:
    try:
        return _identity(resource, label)
    except NormalizationError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError):
        raise NormalizationError(f"{label} is not shaped as FHIR R4 expects") from None


def _identity(resource: Json, label: str) -> PatientIdentity:
    names = [n for n in resource.get("name") or [] if isinstance(n, dict)]
    official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    return PatientIdentity(
        given_names=tuple(g for g in official.get("given") or [] if isinstance(g, str)),
        family_name=official.get("family") if isinstance(official.get("family"), str) else None,
        birth_date=_birth_date(resource, label),
        identifiers=tuple(
            PatientIdentifier(item.get("system") or "", item["value"])
            for item in resource.get("identifier") or []
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        ),
        sex_at_birth=_sex_at_birth(resource),
    )


def _birth_date(resource: Json, label: str) -> date | None:
    raw = resource.get("birthDate")
    if raw is None:
        return None
    try:
        parsed = parse_fhir_datetime(raw)
    except (ClinicalTimeError, TypeError):
        raise NormalizationError(f"{label} has a birth date that is not a FHIR date") from None
    if parsed.precision is not TimePrecision.DAY or parsed.calendar_date is None:
        # A year or a month alone is a valid FHIR date but no calendar date to look up by.
        raise NormalizationError(f"{label} has a birth date that is not a full date")
    return parsed.calendar_date


def _sex_at_birth(resource: Json) -> str:
    for extension in resource.get("extension") or []:
        if extension.get("url") == BIRTH_SEX_URL:
            return SEX_BY_BIRTHSEX.get(extension.get("valueCode"), "unknown")
    return "unknown"
