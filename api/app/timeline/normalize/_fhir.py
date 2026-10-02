"""Small readers shared by the normalizers. Pure functions over parsed FHIR JSON.

Numbers are parsed as ``Decimal`` from their source text, so ``1.0`` keeps the precision
the source stated. A UCUM code is the only unit a timeline row carries: a quantity with
any other unit system keeps its value as text, because a number without a comparable unit
must never be trended. Nothing here puts a value into an error message.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.timeline.clinical_time import (
    ClinicalTime,
    ClinicalTimeError,
    parse_fhir_datetime,
    sort_instant,
)
from app.timeline.ingest import TimelineEventDraft
from app.timeline.vocabulary import TimelineKind

UCUM = "http://unitsofmeasure.org"

Json = dict[str, Any]


class NormalizationError(Exception):
    """A resource could not be normalized. Names the resource and path, never a value."""


@dataclass(frozen=True, slots=True)
class Concept:
    system: str | None
    code: str | None
    display: str | None


@dataclass(frozen=True, slots=True)
class Context:
    """What a normalizer needs beside the resource: its label, the clinic zone, a fallback time."""

    label: str
    zone: ZoneInfo
    source_updated_at: datetime | None


def parse_resource(payload: bytes, label: str) -> Json:
    try:
        resource = json.loads(payload, parse_float=Decimal)
    except ValueError:
        raise NormalizationError(f"{label} is not valid JSON") from None
    if not isinstance(resource, dict):
        raise NormalizationError(f"{label} is not a JSON object")
    return resource


def first_concept(codeable: Any) -> Concept:
    """The first coding of a CodeableConcept; its text stands in for a missing display."""
    if not isinstance(codeable, dict):
        return Concept(None, None, None)
    codings = codeable.get("coding") or [{}]
    coding = codings[0] if isinstance(codings[0], dict) else {}
    return Concept(
        coding.get("system"), coding.get("code"), coding.get("display") or codeable.get("text")
    )


def code_values(codeable: Any) -> set[str]:
    if not isinstance(codeable, dict):
        return set()
    return {c["code"] for c in codeable.get("coding") or [] if isinstance(c, dict) and "code" in c}


def status_code(codeable: Any) -> str | None:
    return first_concept(codeable).code


# A condition or allergy the source says was never true for the patient has no place on a
# chart: it was recorded by mistake, or ruled out. The snapshot keeps it.
NOT_TRUE_VERIFICATIONS = frozenset({"entered-in-error", "refuted"})


def verification_code(resource: Json) -> str | None:
    return status_code(resource.get("verificationStatus"))


def clinical_time(ctx: Context, path: str, raw: Any) -> ClinicalTime | None:
    if raw is None:
        return None
    try:
        return parse_fhir_datetime(raw)
    except (ClinicalTimeError, TypeError):
        where = f"{ctx.label} {path}" if path else ctx.label
        raise NormalizationError(
            f"{where} has a time that is not a FHIR date or dateTime"
        ) from None


def instant_of(ctx: Context, path: str, raw: Any) -> datetime | None:
    """A time as a UTC instant, or None when the source gave only a date (never midnight)."""
    parsed = clinical_time(ctx, path, raw)
    return parsed.instant if parsed else None


def period_bounds(
    ctx: Context, path: str, period: Any
) -> tuple[ClinicalTime | None, ClinicalTime | None]:
    if not isinstance(period, dict):
        return None, None
    return (
        clinical_time(ctx, f"{path}.start", period.get("start")),
        clinical_time(ctx, f"{path}.end", period.get("end")),
    )


def time_choice(
    ctx: Context, resource: Json, stem: str
) -> tuple[ClinicalTime | None, ClinicalTime | None]:
    """Read a FHIR ``[x]`` time: ``stemDateTime`` / ``stemInstant`` or ``stemPeriod``."""
    for suffix in ("DateTime", "Instant"):
        raw = resource.get(f"{stem}{suffix}")
        if raw is not None:
            return clinical_time(ctx, f"{stem}{suffix}", raw), None
    return period_bounds(ctx, f"{stem}Period", resource.get(f"{stem}Period"))


def ucum_quantity(quantity: Any) -> tuple[Decimal, str] | None:
    """(value, UCUM code) of a quantity, or None when it is not an exact number in a UCUM unit.

    A quantity with a comparator ("<5", ">=200") states a bound, not a value; it stays text.
    """
    if not isinstance(quantity, dict) or quantity.get("comparator"):
        return None
    value, code = quantity.get("value"), quantity.get("code")
    if quantity.get("system") != UCUM or not code or not isinstance(value, int | Decimal):
        return None
    return Decimal(value), code


def quantity_text(quantity: dict[str, Any]) -> str | None:
    value = quantity.get("value")
    if value is None:
        return None
    unit = quantity.get("unit") or quantity.get("code")
    bound = f"{quantity['comparator']}{value}" if quantity.get("comparator") else str(value)
    return f"{bound} {unit}" if unit else bound


def detail_json(detail: Any) -> bytes | None:
    if not detail:
        return None
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def draft(
    ctx: Context,
    *,
    path: str,
    kind: TimelineKind,
    occurred: ClinicalTime | None,
    concept: Concept,
    recorded_at: datetime | None = None,
    **fields: Any,
) -> TimelineEventDraft:
    """Build a draft. Rows order by their clinical time, else when recorded, else when fetched."""
    if occurred is not None:
        sort_at = sort_instant(occurred, ctx.zone)
    elif recorded_at is not None:
        sort_at = recorded_at
    elif ctx.source_updated_at is not None:
        sort_at = ctx.source_updated_at
    else:
        raise NormalizationError(f"{ctx.label} has no time to place it on the timeline")
    return TimelineEventDraft(
        source_path=path,
        kind=kind,
        occurred=occurred,
        sort_at=sort_at,
        code_system=concept.system,
        code=concept.code,
        code_display=concept.display,
        recorded_at=recorded_at,
        **fields,
    )
