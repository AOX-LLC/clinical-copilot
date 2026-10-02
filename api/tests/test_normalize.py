"""Every normalizer, on resources from the committed dataset. No database."""

import dataclasses
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.timeline.ingest import TimelineEventDraft
from app.timeline.normalize import NormalizationError, build_projector
from app.timeline.normalize.patient import identity_from_fhir_patient
from app.timeline.vocabulary import TimelineKind, TimePrecision
from tests.dataset import (
    RESOURCE_COUNTS,
    count_by_type,
    dataset_record,
    dataset_resource,
    record_of,
)
from tests.fixtures import SENTINEL_FAMILY_NAME

ZONE = ZoneInfo("America/New_York")
PROJECT = build_projector(ZONE)

ENCOUNTER = "0b56998e-f475-39ed-85e3-5222ecf51d74"
RESOLVED_CONDITION = "0b56998e-f475-39ed-5fd0-476bc6d76513"
ACTIVE_CONDITION = "0b879479-3d66-a073-1cfb-6d68bf56a575"
HEIGHT = "0b56998e-f475-39ed-dea5-bd537d54f71c"
BLOOD_PRESSURE = "0b56998e-f475-39ed-53d5-944ab61863f0"
SINGLE_COMPONENT_PANEL = "93fa2321-9e6d-6f54-3801-2ecb9ce8db00"
LEUKOCYTES = "0b56998e-f475-39ed-9bdc-fdefe1ba5e05"
URINE_APPEARANCE = "105c7075-b0bd-5f17-7a4b-ca2deb1f9f9d"
LAB_STRING = "cce575b1-e574-5c2f-dcca-11072ce85a82"
SURVEY = "0b879479-3d66-a073-8a5f-7d0db10a4477"
SOCIAL_HISTORY = "0b56998e-f475-39ed-03ff-4bd89b292788"
INLINE_MEDICATION = "0b56998e-f475-39ed-d8c7-04dec8a13af5"
CONTAINED_MEDICATION = "0b879479-3d66-a073-11a1-e749dce77bfb"
DOSED_MEDICATION = "0b879479-3d66-a073-06fe-6dbe21ebae62"
PROCEDURE = "0b56998e-f475-39ed-f7f4-0cba703caa60"
IMMUNIZATION = "0b56998e-f475-39ed-d80c-0d4ec98ffeab"
ALLERGY = "3144c596-4231-2d47-d6cd-cd93be9329f3"
ALLERGY_WITH_REACTION = "3144c596-4231-2d47-14d3-a6bb3d2b5b82"
OPEN_CARE_PLAN = "0b879479-3d66-a073-6bfd-719ba69d6e75"
ENDED_CARE_PLAN = "0b56998e-f475-39ed-f319-0b0712a1f56b"
PATIENT = "0b56998e-f475-39ed-f31e-e0e5f79c9aee"


def project(resource_type: str, resource_id: str) -> list[TimelineEventDraft]:
    return list(PROJECT(dataset_record(resource_type, resource_id)))


def project_resource(resource: dict[str, Any]) -> list[TimelineEventDraft]:
    return list(PROJECT(record_of(resource)))


def detail(row: TimelineEventDraft) -> dict[str, Any]:
    assert row.detail_json is not None
    decoded: dict[str, Any] = json.loads(row.detail_json)
    return decoded


def test_the_dataset_counts_match_adr_0012() -> None:
    assert dict(count_by_type()) == RESOURCE_COUNTS
    assert sum(RESOURCE_COUNTS.values()) == 13_708


def test_encounter_spans_its_period() -> None:
    (row,) = project("Encounter", ENCOUNTER)

    assert row.kind is TimelineKind.ENCOUNTER
    assert row.occurred is not None
    assert row.occurred.precision is TimePrecision.INSTANT
    assert row.occurred.instant == datetime(2022, 8, 9, 1, 16, 44, tzinfo=UTC)
    assert row.period_end is not None
    assert row.period_end.instant == datetime(2022, 8, 9, 1, 31, 44, tzinfo=UTC)
    assert row.sort_at == row.occurred.instant
    assert (row.status, row.code_system) == ("finished", "http://snomed.info/sct")


def test_a_resolved_condition_ends_at_its_abatement() -> None:
    (row,) = project("Condition", RESOLVED_CONDITION)

    assert row.kind is TimelineKind.CONDITION
    assert row.status == "resolved"
    assert row.occurred is not None
    assert row.occurred.raw == "2022-08-09T01:16:44+00:00"
    assert row.period_end is not None
    assert row.period_end.raw == "2023-08-15T01:16:44+00:00"
    assert row.recorded_at == row.occurred.instant


def test_an_active_condition_has_no_end() -> None:
    (row,) = project("Condition", ACTIVE_CONDITION)

    assert row.status == "active"
    assert row.period_end is None


def test_a_laboratory_quantity_keeps_its_digits_and_ucum_unit() -> None:
    (row,) = project("Observation", LEUKOCYTES)

    assert row.kind is TimelineKind.LAB
    assert row.source_path == ""
    assert row.value_numeric == Decimal("5.5248")
    assert row.value_numeric.as_tuple().exponent == -4
    assert row.value_unit == "10*3/uL"
    assert row.code == "6690-2"
    assert row.code_system == "http://loinc.org"
    assert row.recorded_at == datetime(2022, 8, 9, 1, 16, 44, 781000, tzinfo=UTC)


def test_a_coded_laboratory_result_is_a_sealed_text_value() -> None:
    (row,) = project("Observation", URINE_APPEARANCE)

    assert row.kind is TimelineKind.LAB
    assert row.value_text == "Cloudy urine (finding)"
    assert row.value_numeric is None
    assert row.value_unit is None


def test_a_string_laboratory_result_is_a_text_value() -> None:
    (row,) = project("Observation", LAB_STRING)

    assert row.value_text
    assert row.value_numeric is None


def test_a_vital_sign_is_a_vital() -> None:
    (row,) = project("Observation", HEIGHT)

    assert row.kind is TimelineKind.VITAL
    assert (row.value_numeric, row.value_unit) == (Decimal("125.4"), "cm")


def test_each_blood_pressure_component_is_its_own_row() -> None:
    rows = project("Observation", BLOOD_PRESSURE)

    assert [row.source_path for row in rows] == ["component[0]", "component[1]"]
    assert {row.kind for row in rows} == {TimelineKind.VITAL}
    assert [(r.code, r.value_numeric, r.value_unit) for r in rows] == [
        ("8462-4", Decimal("88"), "mm[Hg]"),
        ("8480-6", Decimal("125"), "mm[Hg]"),
    ]
    assert rows[0].occurred == rows[1].occurred
    assert rows[0].occurred is not None


def test_a_one_component_panel_yields_one_row() -> None:
    rows = project("Observation", SINGLE_COMPONENT_PANEL)

    assert [row.source_path for row in rows] == ["component[0]"]


@pytest.mark.parametrize("resource_id", [SURVEY, SOCIAL_HISTORY])
def test_observation_categories_the_product_does_not_read_yield_no_rows(resource_id: str) -> None:
    assert project("Observation", resource_id) == []


def test_a_quantity_without_a_ucum_unit_keeps_text_and_no_number() -> None:
    resource = dataset_resource("Observation", LEUKOCYTES)
    resource["valueQuantity"] = {"value": 7.5, "unit": "widgets", "system": "http://example.test"}

    (row,) = project_resource(resource)

    assert row.value_numeric is None
    assert row.value_unit is None
    assert row.value_text == "7.5 widgets"


def test_a_reference_range_and_interpretation_are_carried_in_the_values_unit() -> None:
    resource = dataset_resource("Observation", LEUKOCYTES)
    ucum = "http://unitsofmeasure.org"
    resource["referenceRange"] = [
        {
            "low": {"value": 4.0, "code": "10*3/uL", "system": ucum},
            "high": {"value": 11.0, "code": "10*3/uL", "system": ucum},
            "text": "4.0 to 11.0",
        }
    ]
    resource["interpretation"] = [{"coding": [{"code": "N"}]}]

    (row,) = project_resource(resource)

    assert (row.ref_low, row.ref_high, row.ref_text) == (
        Decimal("4.0"),
        Decimal("11.0"),
        "4.0 to 11.0",
    )
    assert row.source_interpretation == "N"


def test_a_reference_range_in_another_unit_keeps_only_its_text() -> None:
    resource = dataset_resource("Observation", LEUKOCYTES)
    ucum = "http://unitsofmeasure.org"
    resource["referenceRange"] = [
        {"low": {"value": 4, "code": "mg/dL", "system": ucum}, "text": "x"}
    ]

    (row,) = project_resource(resource)

    assert (row.ref_low, row.ref_high, row.ref_text) == (None, None, "x")


def test_an_inline_medication_is_named_with_its_dosage_text() -> None:
    (row,) = project("MedicationRequest", INLINE_MEDICATION)

    assert row.kind is TimelineKind.MEDICATION
    assert row.code_display == "Ibuprofen 100 MG Oral Tablet"
    assert row.code == "198405"
    assert row.value_text == "Take as needed."
    assert row.occurred is not None
    assert row.occurred.raw == "2023-01-21T01:43:28+00:00"
    assert row.status == "completed"


def test_a_contained_medication_is_resolved_from_the_request() -> None:
    (row,) = project("MedicationRequest", CONTAINED_MEDICATION)

    assert row.code == "807283"
    assert row.code_display == "levonorgestrel 0.000833 MG/HR Intrauterine System [Mirena]"


def test_structured_dosage_goes_to_the_sealed_detail() -> None:
    (row,) = project("MedicationRequest", DOSED_MEDICATION)

    assert row.status == "active"
    assert detail(row)["dosage"][0]["sequence"] == 1


def test_a_medication_reference_that_is_not_contained_is_an_error() -> None:
    resource = dataset_resource("MedicationRequest", CONTAINED_MEDICATION)
    resource["medicationReference"] = {"reference": "Medication/elsewhere"}

    with pytest.raises(NormalizationError, match="not inside it"):
        project_resource(resource)


def test_a_medication_statement_uses_its_effective_period() -> None:
    # The dataset holds no MedicationStatement: this one is derived from a dataset request.
    request = dataset_resource("MedicationRequest", INLINE_MEDICATION)
    statement = {
        "resourceType": "MedicationStatement",
        "id": "derived-statement",
        "status": "active",
        "medicationCodeableConcept": request["medicationCodeableConcept"],
        "effectivePeriod": {"start": "2023-02-01", "end": "2023-03-01"},
        "dosage": request["dosageInstruction"],
    }

    (row,) = project_resource(statement)

    assert row.kind is TimelineKind.MEDICATION
    assert row.code == "198405"
    assert row.occurred is not None
    assert row.occurred.precision is TimePrecision.DAY
    assert row.period_end is not None
    assert row.period_end.calendar_date == date(2023, 3, 1)
    assert row.value_text == "Take as needed."


def test_a_procedure_spans_its_period() -> None:
    (row,) = project("Procedure", PROCEDURE)

    assert row.kind is TimelineKind.PROCEDURE
    assert row.status == "completed"
    assert row.period_end is not None
    assert row.occurred is not None
    assert row.period_end.instant == datetime(2022, 8, 9, 1, 21, 44, tzinfo=UTC)


def test_an_immunization_is_placed_at_its_occurrence() -> None:
    (row,) = project("Immunization", IMMUNIZATION)

    assert row.kind is TimelineKind.IMMUNIZATION
    assert (row.code_system, row.code) == ("http://hl7.org/fhir/sid/cvx", "140")
    assert row.occurred is not None
    assert row.occurred.instant == datetime(2022, 8, 9, 1, 16, 44, tzinfo=UTC)


def test_an_allergy_keeps_criticality_and_reactions_in_the_sealed_detail() -> None:
    plain, with_reaction = (
        project("AllergyIntolerance", ALLERGY)[0],
        project("AllergyIntolerance", ALLERGY_WITH_REACTION)[0],
    )

    assert plain.kind is TimelineKind.ALLERGY
    assert plain.status == "active"
    assert detail(plain)["criticality"] == "low"
    assert "reaction" in detail(with_reaction)
    assert plain.occurred is not None
    assert plain.occurred.raw == "1935-03-11T22:35:09+00:00"


def test_a_care_plan_is_named_by_its_specific_category_and_is_not_a_protocol() -> None:
    (open_row,) = project("CarePlan", OPEN_CARE_PLAN)
    (ended_row,) = project("CarePlan", ENDED_CARE_PLAN)

    assert open_row.kind is TimelineKind.CARE_PLAN
    assert open_row.kind.value != "protocol"
    assert open_row.code_system == "http://snomed.info/sct"
    assert open_row.period_end is None
    assert ended_row.period_end is not None
    assert detail(open_row)["activities"], "activities belong in the sealed detail"


def test_patient_yields_no_timeline_rows() -> None:
    assert project("Patient", PATIENT) == []


def test_patient_identity_is_read_from_the_record() -> None:
    resource = dataset_resource("Patient", PATIENT)

    identity = identity_from_fhir_patient(resource, "Patient/x")

    official = next(n for n in resource["name"] if n.get("use") == "official")
    assert identity.given_names == tuple(official["given"])
    assert identity.family_name == official["family"]
    assert identity.birth_date == date.fromisoformat(resource["birthDate"])
    assert {(i.system, i.value) for i in identity.identifiers} == {
        (i.get("system", ""), i["value"]) for i in resource["identifier"]
    }
    assert identity.sex_at_birth in {"male", "female"}


def test_a_date_only_time_stays_a_date_and_sorts_in_the_clinic_zone() -> None:
    resource = dataset_resource("Procedure", PROCEDURE)
    resource["performedPeriod"] = {"start": "2022-08-09"}

    (row,) = project_resource(resource)

    assert row.occurred is not None
    assert row.occurred.precision is TimePrecision.DAY
    assert row.occurred.instant is None
    assert row.occurred.calendar_date == date(2022, 8, 9)
    assert row.sort_at == datetime(2022, 8, 9, 4, 0, tzinfo=UTC)  # midnight in New York


def test_a_time_without_an_offset_is_refused_without_echoing_it() -> None:
    resource = dataset_resource("Encounter", ENCOUNTER)
    resource["period"] = {"start": "2022-08-09T01:16:44"}

    with pytest.raises(NormalizationError) as caught:
        project_resource(resource)

    assert "2022-08-09" not in str(caught.value)
    assert f"Encounter/{ENCOUNTER}" in str(caught.value)


def test_errors_never_carry_payload_content() -> None:
    resource = dataset_resource("Condition", ACTIVE_CONDITION)
    resource["onsetDateTime"] = SENTINEL_FAMILY_NAME

    with pytest.raises(NormalizationError) as caught:
        project_resource(resource)

    assert SENTINEL_FAMILY_NAME not in str(caught.value)


def test_a_resource_type_with_no_normalizer_is_refused() -> None:
    with pytest.raises(NormalizationError, match="no normalizer"):
        project_resource({"resourceType": "Device", "id": "d1"})


def test_a_row_with_no_time_at_all_is_refused() -> None:
    resource = dataset_resource("Encounter", ENCOUNTER)
    del resource["period"]

    with pytest.raises(NormalizationError, match="no time"):
        project_resource(resource)


def test_every_resource_in_the_dataset_projects_without_error() -> None:
    from tests.dataset import patient_resources

    produced = 0
    for resources in patient_resources():
        for resource in resources:
            produced += len(project_resource(resource))

    assert produced == 12_939


def _read(resource: dict[str, Any]) -> object:
    """What ingest reads from a resource: a patient's identity, anything else's timeline rows."""
    label = f"{resource['resourceType']}/{resource['id']}"
    if resource["resourceType"] == "Patient":
        return identity_from_fhir_patient(resource, label)
    return project_resource(resource)


def _year_one(resource: dict[str, Any]) -> None:
    resource["performedPeriod"] = {"start": "0001-01-01T00:00:00+01:00"}


@pytest.mark.parametrize(
    ("resource_type", "resource_id", "damage"),
    [
        ("Condition", RESOLVED_CONDITION, lambda r: r["code"].update(coding={"a": 1})),
        ("Encounter", ENCOUNTER, lambda r: r.update(type={"a": 1})),
        ("Observation", LEUKOCYTES, lambda r: r.update(interpretation={"a": 1})),
        ("Procedure", PROCEDURE, _year_one),
        ("Patient", PATIENT, lambda r: r.update(extension=[1])),
    ],
)
def test_a_resource_of_an_unexpected_shape_is_a_normalization_error(
    resource_type: str, resource_id: str, damage: Any
) -> None:
    resource = dataset_resource(resource_type, resource_id)
    damage(resource)

    with pytest.raises(NormalizationError, match=f"{resource_type}/{resource_id}"):
        _read(resource)


def test_json_nested_past_the_interpreter_limit_is_a_normalization_error() -> None:
    nested = b"[" * 100_000 + b"]" * 100_000
    record = record_of(dataset_resource("Procedure", PROCEDURE))
    deep = dataclasses.replace(record, payload=nested)

    with pytest.raises(NormalizationError):
        PROJECT(deep)


def test_list_items_that_are_not_objects_are_ignored_not_errors() -> None:
    blood_pressure = dataset_resource("Observation", BLOOD_PRESSURE)
    blood_pressure["component"].append("not an object")
    medication = dataset_resource("MedicationRequest", INLINE_MEDICATION)
    medication["dosageInstruction"] = [1]

    assert len(project_resource(blood_pressure)) == 2
    (row,) = project_resource(medication)
    assert row.value_text is None


@pytest.mark.parametrize("comparator", ["<", "<=", ">=", ">"])
def test_a_qualified_quantity_is_text_never_a_number(comparator: str) -> None:
    resource = dataset_resource("Observation", LEUKOCYTES)
    resource["valueQuantity"] = {
        "value": 5,
        "comparator": comparator,
        "unit": "mg/dL",
        "code": "mg/dL",
        "system": "http://unitsofmeasure.org",
    }

    (row,) = project_resource(resource)

    assert row.value_numeric is None
    assert row.value_unit is None
    assert row.value_text == f"{comparator}5 mg/dL"


def test_a_reference_bound_with_a_comparator_is_not_a_number() -> None:
    resource = dataset_resource("Observation", LEUKOCYTES)
    ucum = "http://unitsofmeasure.org"
    resource["referenceRange"] = [
        {"high": {"value": 5, "comparator": "<", "code": "10*3/uL", "system": ucum}, "text": "<5"}
    ]

    (row,) = project_resource(resource)

    assert (row.ref_low, row.ref_high, row.ref_text) == (None, None, "<5")
