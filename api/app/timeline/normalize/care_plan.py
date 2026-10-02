"""CarePlan: its own kind, apart from the practice's protocols.

A care plan carries a generic category (US Core's ``assess-plan``) and a specific one
(a SNOMED regime). The specific one names the plan; the activities go to the sealed detail.
A plan the practice categorised as a protocol becomes a ``protocol`` row instead (ADR 0016):
the specific category names it, and its activities (the supplement regimens it references and
its follow-up steps) go to the sealed detail.
"""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Concept,
    Context,
    Json,
    detail_json,
    draft,
    first_concept,
    period_bounds,
)
from app.timeline.normalize.practice import (
    PRACTICE_CATEGORY_SYSTEM,
    PROTOCOL_CATEGORY,
    has_practice_category,
)
from app.timeline.vocabulary import TimelineKind

GENERIC_CATEGORY_SYSTEM = "http://hl7.org/fhir/us/core/CodeSystem/careplan-category"


def project_care_plan(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    is_protocol = any(
        has_practice_category(category, PROTOCOL_CATEGORY)
        for category in resource.get("category") or []
    )
    start, end = period_bounds(ctx, "period", resource.get("period"))
    activities = [
        {
            "code": _concept_json(first_concept(a["detail"]["code"])),
            "status": a["detail"].get("status"),
        }
        for a in resource.get("activity") or []
        if isinstance(a.get("detail"), dict) and "code" in a["detail"]
    ]
    detail: Json = {"intent": resource.get("intent"), "activities": activities}
    if is_protocol:
        detail["supplements"] = [
            a["reference"]["reference"]
            for a in resource.get("activity") or []
            if isinstance(a.get("reference"), dict)
            and isinstance(a["reference"].get("reference"), str)
        ]
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.PROTOCOL if is_protocol else TimelineKind.CARE_PLAN,
            occurred=start,
            concept=_plan_concept(resource),
            period_end=end,
            status=resource.get("status"),
            detail_json=detail_json(detail),
        )
    ]


def _plan_concept(resource: Json) -> Concept:
    concepts = [first_concept(category) for category in resource.get("category") or []]
    ordinary = (GENERIC_CATEGORY_SYSTEM, PRACTICE_CATEGORY_SYSTEM)
    specific = [c for c in concepts if c.system not in ordinary and c.code]
    return (specific or concepts or [Concept(None, None, None)])[0]


def _concept_json(concept: Concept) -> dict[str, str | None]:
    return {"system": concept.system, "code": concept.code, "display": concept.label}
