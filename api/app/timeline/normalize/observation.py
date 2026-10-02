"""Observation: laboratory results and vital signs; each component gets its own row.

Categories projected: ``laboratory`` becomes a lab and ``vital-signs`` a vital. Every other
category (survey, social-history, exam, procedure, imaging, activity, therapy) produces no
row: the product reads results and vitals, and those categories are questionnaires and
history that the summary does not trend. The source snapshot keeps them all.

A panel with components and no value of its own (a blood pressure) yields one row per
component, at ``component[i]``. A numeric value needs a UCUM unit; a quantity in another
unit system keeps its text and no number. Value kinds other than quantity, integer, string,
boolean and coded concept (ranges, ratios, sampled data) carry no value; the snapshot keeps them.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Concept,
    Context,
    Json,
    code_values,
    draft,
    first_concept,
    instant_of,
    quantity_text,
    time_choice,
    ucum_quantity,
)
from app.timeline.vocabulary import TimelineKind

KIND_BY_CATEGORY = {"laboratory": TimelineKind.LAB, "vital-signs": TimelineKind.VITAL}


def project_observation(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    kind = _kind_of(resource)
    if kind is None:
        return []
    occurred, period_end = time_choice(ctx, resource, "effective")
    shared: dict[str, Any] = {
        "kind": kind,
        "occurred": occurred,
        "period_end": period_end,
        "recorded_at": instant_of(ctx, "issued", resource.get("issued")),
        "status": resource.get("status"),
    }
    components = [c for c in resource.get("component") or [] if isinstance(c, dict)]
    drafts: list[TimelineEventDraft] = []
    own_value = _value_fields(resource)
    if own_value or not components:
        drafts.append(
            _row(ctx, "", resource, first_concept(resource.get("code")), own_value, shared)
        )
    for index, component in enumerate(components):
        drafts.append(
            _row(
                ctx,
                f"component[{index}]",
                component,
                first_concept(component.get("code")),
                _value_fields(component),
                shared,
            )
        )
    return drafts


def _kind_of(resource: Json) -> TimelineKind | None:
    codes: set[str] = set()
    for category in resource.get("category") or []:
        codes |= code_values(category)
    for category_code, kind in KIND_BY_CATEGORY.items():
        if category_code in codes:
            return kind
    return None


def _row(
    ctx: Context,
    path: str,
    holder: Json,
    concept: Concept,
    value: dict[str, Any],
    shared: dict[str, Any],
) -> TimelineEventDraft:
    return draft(
        ctx,
        path=path,
        concept=concept,
        **shared,
        **value,
        **_range_fields(holder, value.get("value_unit")),
        source_interpretation=_interpretation(holder),
    )


def _value_fields(holder: Json) -> dict[str, Any]:
    quantity = holder.get("valueQuantity")
    if isinstance(quantity, dict):
        ucum = ucum_quantity(quantity)
        if ucum is not None:
            return {"value_numeric": ucum[0], "value_unit": ucum[1]}
        text = quantity_text(quantity)
        return {"value_text": text} if text else {}
    if "valueCodeableConcept" in holder:
        display = first_concept(holder["valueCodeableConcept"]).label
        return {"value_text": display} if display else {}
    if isinstance(holder.get("valueString"), str):
        return {"value_text": holder["valueString"]}
    if isinstance(holder.get("valueBoolean"), bool):
        return {"value_text": "true" if holder["valueBoolean"] else "false"}
    if isinstance(holder.get("valueInteger"), int):
        return {"value_numeric": Decimal(holder["valueInteger"])}
    return {}


def _range_fields(holder: Json, value_unit: str | None) -> dict[str, Any]:
    """The first reference range. Bounds count only in the value's own UCUM unit."""
    ranges = [r for r in holder.get("referenceRange") or [] if isinstance(r, dict)]
    if not ranges:
        return {}
    first = ranges[0]
    fields: dict[str, Any] = {"ref_text": first.get("text")}
    low, high = ucum_quantity(first.get("low")), ucum_quantity(first.get("high"))
    bounds = [b for b in (low, high) if b is not None]
    if value_unit is not None and all(unit == value_unit for _, unit in bounds):
        low_value = low[0] if low else None
        high_value = high[0] if high else None
        if low_value is None or high_value is None or low_value <= high_value:
            fields.update(ref_low=low_value, ref_high=high_value)
    return fields


def _interpretation(holder: Json) -> str | None:
    interpretations = holder.get("interpretation") or []
    return first_concept(interpretations[0]).code if interpretations else None
