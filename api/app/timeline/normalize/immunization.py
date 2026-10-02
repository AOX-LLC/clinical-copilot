"""Immunization: one row at the time the vaccine was given."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Context,
    Json,
    draft,
    first_concept,
    instant_of,
    time_choice,
)
from app.timeline.vocabulary import TimelineKind


def project_immunization(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    occurred, _ = time_choice(ctx, resource, "occurrence")
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.IMMUNIZATION,
            occurred=occurred,
            concept=first_concept(resource.get("vaccineCode")),
            recorded_at=instant_of(ctx, "recorded", resource.get("recorded")),
            status=resource.get("status"),
        )
    ]
