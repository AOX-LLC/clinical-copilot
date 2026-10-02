"""Condition: onset is when it happened, abatement is the end of the period."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Context,
    Json,
    clinical_time,
    draft,
    first_concept,
    instant_of,
    status_code,
    time_choice,
)
from app.timeline.vocabulary import TimelineKind


def project_condition(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    onset_start, onset_end = time_choice(ctx, resource, "onset")
    abatement_start, abatement_end = time_choice(ctx, resource, "abatement")
    recorded = resource.get("recordedDate")
    occurred = onset_start or clinical_time(ctx, "recordedDate", recorded)
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.CONDITION,
            occurred=occurred,
            concept=first_concept(resource.get("code")),
            period_end=abatement_end or abatement_start or onset_end,
            recorded_at=instant_of(ctx, "recordedDate", recorded),
            status=status_code(resource.get("clinicalStatus")),
        )
    ]
