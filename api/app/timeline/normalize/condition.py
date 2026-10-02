"""Condition: onset is when it happened, abatement is the end of the period.

The end of an onset window says when onset finished, not when the condition resolved, so
it never becomes the end of the period."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    NOT_TRUE_VERIFICATIONS,
    Context,
    Json,
    clinical_time,
    detail_json,
    draft,
    first_concept,
    instant_of,
    status_code,
    time_choice,
    verification_code,
)
from app.timeline.vocabulary import TimelineKind


def project_condition(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    verification = verification_code(resource)
    if verification in NOT_TRUE_VERIFICATIONS:
        return []
    onset_start, _ = time_choice(ctx, resource, "onset")
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
            period_end=abatement_end or abatement_start,
            recorded_at=instant_of(ctx, "recordedDate", recorded),
            status=status_code(resource.get("clinicalStatus")),
            detail_json=detail_json({"verification": verification} if verification else None),
        )
    ]
