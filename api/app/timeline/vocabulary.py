"""Closed vocabularies shared by the timeline schema, ingestion and the EHR adapters."""

from enum import StrEnum


class SourceKind(StrEnum):
    FHIR_R4 = "fhir_r4"
    HEALTHIE = "healthie"
    LAB_FEED = "lab_feed"


class RecordState(StrEnum):
    PRESENT = "present"
    DELETED = "deleted"


class ImportTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"


class ImportStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TimelineKind(StrEnum):
    ENCOUNTER = "encounter"
    CONDITION = "condition"
    LAB = "lab"
    VITAL = "vital"
    MEDICATION = "medication"
    SUPPLEMENT = "supplement"
    PROTOCOL = "protocol"
    CARE_PLAN = "care_plan"
    PROCEDURE = "procedure"
    IMMUNIZATION = "immunization"
    ALLERGY = "allergy"
    NOTE = "note"
    APPOINTMENT = "appointment"


class TimePrecision(StrEnum):
    """How much of a clinical time the source actually stated.

    A date-only lab stays a date: storing it as midnight UTC would show it on the
    previous day in every US timezone.
    """

    INSTANT = "instant"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


CALENDAR_PRECISIONS = frozenset({TimePrecision.DAY, TimePrecision.MONTH, TimePrecision.YEAR})
