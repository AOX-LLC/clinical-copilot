"""CarePlan: its own kind, because "protocol" belongs to the practice's supplement protocols.

A care plan carries a generic category (US Core's ``assess-plan``) and a specific one
(a SNOMED regime). The specific one names the plan; the activities go to the sealed detail.
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
from app.timeline.vocabulary import TimelineKind

GENERIC_CATEGORY_SYSTEM = "http://hl7.org/fhir/us/core/CodeSystem/careplan-category"


def project_care_plan(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    start, end = period_bounds(ctx, "period", resource.get("period"))
    activities = [
        {
            "code": _concept_json(first_concept(a["detail"]["code"])),
            "status": a["detail"].get("status"),
        }
        for a in resource.get("activity") or []
        if isinstance(a.get("detail"), dict) and "code" in a["detail"]
    ]
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.CARE_PLAN,
            occurred=start,
            concept=_plan_concept(resource),
            period_end=end,
            status=resource.get("status"),
            detail_json=detail_json({"intent": resource.get("intent"), "activities": activities}),
        )
    ]


def _plan_concept(resource: Json) -> Concept:
    concepts = [first_concept(category) for category in resource.get("category") or []]
    specific = [c for c in concepts if c.system != GENERIC_CATEGORY_SYSTEM and c.code]
    return (specific or concepts or [Concept(None, None, None)])[0]


def _concept_json(concept: Concept) -> dict[str, str | None]:
    return {"system": concept.system, "code": concept.code, "display": concept.display}
