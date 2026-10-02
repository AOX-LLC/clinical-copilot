"""AllergyIntolerance: onset when stated, else when it was recorded."""

from collections.abc import Sequence

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Context,
    Json,
    clinical_time,
    detail_json,
    draft,
    first_concept,
    instant_of,
    status_code,
    time_choice,
)
from app.timeline.vocabulary import TimelineKind

DETAIL_FIELDS = ("type", "category", "criticality", "reaction")


def project_allergy(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    onset_start, onset_end = time_choice(ctx, resource, "onset")
    recorded = resource.get("recordedDate")
    return [
        draft(
            ctx,
            path="",
            kind=TimelineKind.ALLERGY,
            occurred=onset_start or clinical_time(ctx, "recordedDate", recorded),
            concept=first_concept(resource.get("code")),
            period_end=onset_end,
            recorded_at=instant_of(ctx, "recordedDate", recorded),
            status=status_code(resource.get("clinicalStatus")),
            detail_json=detail_json({k: resource[k] for k in DETAIL_FIELDS if k in resource}),
        )
    ]
