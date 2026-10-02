"""Encounter: one row, placed at the start of its period."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import Concept, Context, Json, draft, first_concept, period_bounds
from app.timeline.vocabulary import TimelineKind


def project_encounter(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    types = resource.get("type") or [None]
    concept = first_concept(types[0])
    if concept.code is None and isinstance(resource.get("class"), dict):
        coding = resource["class"]
        concept = Concept(coding.get("system"), coding.get("code"), coding.get("display"))
    start, end = period_bounds(ctx, "period", resource.get("period"))
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.ENCOUNTER,
            occurred=start,
            concept=concept,
            period_end=end,
            status=resource.get("status"),
        )
    ]
