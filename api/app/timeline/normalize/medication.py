"""MedicationRequest and MedicationStatement, with the contained Medication they name.

The dataset inlines each medication into its request (ADR 0012), so a request resolves its
drug from ``contained``. A reference that cannot be resolved there is an error: a
normalizer sees one record, and a drug it cannot name is not a timeline row. The dosage
text is the row's value; the structured dosage is kept in the sealed detail.
"""

from collections.abc import Sequence
from typing import Any

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize._fhir import (
    Concept,
    Context,
    Json,
    NormalizationError,
    clinical_time,
    detail_json,
    draft,
    first_concept,
    instant_of,
    time_choice,
)
from app.timeline.vocabulary import TimelineKind


def project_medication_request(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    authored = resource.get("authoredOn")
    return [
        _row(
            ctx,
            resource,
            occurred=clinical_time(ctx, "authoredOn", authored),
            period_end=None,
            recorded_at=instant_of(ctx, "authoredOn", authored),
            dosage=resource.get("dosageInstruction"),
        )
    ]


def project_medication_statement(resource: Json, ctx: Context) -> Sequence[TimelineEventDraft]:
    start, end = time_choice(ctx, resource, "effective")
    asserted = resource.get("dateAsserted")
    return [
        _row(
            ctx,
            resource,
            occurred=start or clinical_time(ctx, "dateAsserted", asserted),
            period_end=end,
            recorded_at=instant_of(ctx, "dateAsserted", asserted),
            dosage=resource.get("dosage"),
        )
    ]


def _row(
    ctx: Context,
    resource: Json,
    *,
    occurred: Any,
    period_end: Any,
    recorded_at: Any,
    dosage: Any,
) -> TimelineEventDraft:
    instructions = [d for d in dosage or [] if isinstance(d, dict)]
    text = next((d["text"] for d in instructions if isinstance(d.get("text"), str)), None)
    return draft(
        ctx,
        path="",
        kind=TimelineKind.MEDICATION,
        occurred=occurred,
        concept=_drug(resource, ctx),
        period_end=period_end,
        recorded_at=recorded_at,
        status=resource.get("status"),
        value_text=text,
        detail_json=detail_json({"dosage": instructions} if instructions else None),
    )


def _drug(resource: Json, ctx: Context) -> Concept:
    if "medicationCodeableConcept" in resource:
        return first_concept(resource["medicationCodeableConcept"])
    reference = (resource.get("medicationReference") or {}).get("reference")
    if isinstance(reference, str) and reference.startswith("#"):
        for contained in resource.get("contained") or []:
            if (
                contained.get("resourceType") == "Medication"
                and contained.get("id") == reference[1:]
            ):
                return first_concept(contained.get("code"))
    raise NormalizationError(f"{ctx.label} names a medication that is not inside it")
