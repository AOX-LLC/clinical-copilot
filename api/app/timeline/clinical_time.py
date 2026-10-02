"""Precision-aware clinical times.

FHIR ``date`` and ``dateTime`` values may state a year, a month, a day, or an instant
with a UTC offset. This module keeps exactly what the source stated: instants become
UTC datetimes, everything coarser stays a calendar date with its precision. Nothing
here invents a time of day or a timezone.

Ordering and display use one practice timezone, so every user sees the same calendar
date for an event regardless of their browser's timezone.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.timeline.vocabulary import TimePrecision

_FHIR_DATETIME = re.compile(
    r"(?P<year>\d{4})"
    r"(?:-(?P<month>\d{2})"
    r"(?:-(?P<day>\d{2})"
    r"(?:T(?P<clock>\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)(?P<offset>Z|[+-]\d{2}:\d{2})?)?"
    r")?)?"
)


class ClinicalTimeError(ValueError):
    """A clinical time string is malformed or lacks a required UTC offset."""


@dataclass(frozen=True, slots=True)
class ClinicalTime:
    precision: TimePrecision
    instant: datetime | None
    calendar_date: date | None
    raw: str

    def __post_init__(self) -> None:
        if self.precision is TimePrecision.INSTANT:
            if self.instant is None or self.calendar_date is not None:
                raise ClinicalTimeError("an instant needs a datetime and no calendar date")
            if self.instant.utcoffset() != UTC.utcoffset(None):
                raise ClinicalTimeError("instants are stored in UTC")
        elif self.calendar_date is None or self.instant is not None:
            raise ClinicalTimeError("a calendar-precision time needs a date and no datetime")


def parse_fhir_datetime(raw: str) -> ClinicalTime:
    """Parse a FHIR R4 ``date``, ``dateTime`` or ``instant`` string."""
    match = _FHIR_DATETIME.fullmatch(raw)
    if match is None:
        raise ClinicalTimeError(f"not a FHIR date or dateTime: {raw!r}")

    year, month, day = match["year"], match["month"], match["day"]
    if match["clock"] is not None:
        return _parse_instant(raw, match["offset"])
    try:
        if day is not None:
            return ClinicalTime(TimePrecision.DAY, None, date(int(year), int(month), int(day)), raw)
        if month is not None:
            return ClinicalTime(TimePrecision.MONTH, None, date(int(year), int(month), 1), raw)
        return ClinicalTime(TimePrecision.YEAR, None, date(int(year), 1, 1), raw)
    except ValueError as error:
        raise ClinicalTimeError(f"not a real calendar date: {raw!r}") from error


def _parse_instant(raw: str, offset: str | None) -> ClinicalTime:
    if offset is None:
        # FHIR requires an offset whenever a time is given; guessing one shifts the event.
        raise ClinicalTimeError(f"time without a UTC offset: {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ClinicalTimeError(f"not a real instant: {raw!r}") from error
    return ClinicalTime(TimePrecision.INSTANT, parsed.astimezone(UTC), None, raw)


def sort_instant(value: ClinicalTime, clinic_zone: ZoneInfo) -> datetime:
    """Return the UTC instant used to order this time on a timeline.

    Calendar-precision times sort at the start of their day in the practice timezone.
    """
    if value.instant is not None:
        return value.instant
    start_of_day = datetime.combine(_calendar_date_of(value), time.min, tzinfo=clinic_zone)
    return start_of_day.astimezone(UTC)


def clinic_calendar_date(value: ClinicalTime, clinic_zone: ZoneInfo) -> date:
    """Return the calendar date this time falls on in the practice timezone."""
    if value.instant is not None:
        return value.instant.astimezone(clinic_zone).date()
    return _calendar_date_of(value)


def _calendar_date_of(value: ClinicalTime) -> date:
    if value.calendar_date is None:
        raise ClinicalTimeError("a calendar-precision time needs a date")
    return value.calendar_date
