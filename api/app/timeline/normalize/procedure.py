"""Procedure: one row over the period it was performed."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import Context, Json, draft, first_concept, time_choice
from app.timeline.vocabulary import TimelineKind


def project_procedure(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    start, end = time_choice(ctx, resource, "performed")
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.PROCEDURE,
            occurred=start,
            concept=first_concept(resource.get("code")),
            period_end=end,
            status=resource.get("status"),
        )
    ]
