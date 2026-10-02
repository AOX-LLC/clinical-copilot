"""The supplement and protocol generator, and how the normalizers read what it writes."""

import json
import uuid
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.ehr.ports import SourceRecord, SourceSystemRef
from app.fhir_seed.practice import CATALOG, PROTOCOLS, add_practice_data
from app.fhir_seed.transform import IdentifierIndex, to_put_bundle
from app.timeline.normalize import build_projector
from app.timeline.normalize.practice import (
    PRACTICE_CATEGORY_SYSTEM,
    PROTOCOL_CATEGORY,
    SUPPLEMENT_CATEGORY,
)
from app.timeline.vocabulary import SourceKind, TimelineKind

REFERENCE = date(2026, 9, 1)
PATIENT_ID = "11111111-0000-4000-8000-000000000001"
SOURCE = SourceSystemRef("fhir-test", SourceKind.FHIR_R4)


def bundle(**patient_fields: Any) -> dict[str, Any]:
    patient = {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "birthDate": "1980-05-17",
        **patient_fields,
    }
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": f"urn:uuid:{PATIENT_ID}",
                "resource": patient,
                "request": {"method": "POST", "url": "Patient"},
            }
        ],
    }


def added(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [e["resource"] for e in result["entry"][1:]]


def test_the_same_inputs_give_the_same_resources() -> None:
    first = add_practice_data(bundle(), 5, REFERENCE)
    second = add_practice_data(bundle(), 5, REFERENCE)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_another_seed_gives_different_regimens() -> None:
    outcomes = {json.dumps(add_practice_data(bundle(), seed, REFERENCE)) for seed in range(8)}

    assert len(outcomes) > 1


def test_the_input_bundle_is_not_changed() -> None:
    original = bundle()
    before = json.dumps(original, sort_keys=True)

    add_practice_data(original, 5, REFERENCE)

    assert json.dumps(original, sort_keys=True) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"deceasedDateTime": "2020-01-01T00:00:00+00:00"},
        {"deceasedBoolean": True},
        {"birthDate": "2010-01-01"},
        {"birthDate": "2008-09-02"},  # turns 18 the day after the reference date
        {"birthDate": "not a date"},
    ],
)
def test_an_ineligible_patient_gets_nothing(fields: dict[str, Any]) -> None:
    assert added(add_practice_data(bundle(**fields), 5, REFERENCE)) == []


def test_a_patient_who_turns_18_on_the_reference_date_is_eligible() -> None:
    assert added(add_practice_data(bundle(birthDate="2008-09-01"), 5, REFERENCE))


def test_a_patient_without_a_birth_date_gets_nothing() -> None:
    patient = bundle()
    del patient["entry"][0]["resource"]["birthDate"]

    assert added(add_practice_data(patient, 5, REFERENCE)) == []


def test_each_patient_has_a_current_protocol_whose_supplements_it_references() -> None:
    for seed in range(20):
        resources = added(add_practice_data(bundle(), seed, REFERENCE))
        statements = {r["id"]: r for r in resources if r["resourceType"] == "MedicationStatement"}
        plans = [r for r in resources if r["resourceType"] == "CarePlan"]

        assert 1 <= len(plans) <= 2
        assert sum(p["status"] == "active" for p in plans) == 1
        referenced = [
            a["reference"]["reference"].removeprefix("urn:uuid:")
            for plan in plans
            for a in plan["activity"]
            if "reference" in a
        ]
        assert sorted(referenced) == sorted(statements)
        assert len(set(referenced)) == len(referenced)


def test_ids_are_uuids_unique_and_stable_per_patient() -> None:
    resources = added(add_practice_data(bundle(), 5, REFERENCE))
    ids = [r["id"] for r in resources]

    assert len(set(ids)) == len(ids)
    assert all(str(uuid.UUID(i)) == i for i in ids)
    other = added(
        add_practice_data(bundle(id="11111111-0000-4000-8000-000000000002"), 5, REFERENCE)
    )
    assert not set(ids) & {r["id"] for r in other}


def test_a_regimen_is_dated_before_the_reference_date_and_ends_after_it_starts() -> None:
    for seed in range(30):
        for resource in added(add_practice_data(bundle(), seed, REFERENCE)):
            period: dict[str, str] = resource.get("period") or resource["effectivePeriod"]
            start = date.fromisoformat(period["start"])
            assert start < REFERENCE
            if "end" in period:
                assert start <= date.fromisoformat(period["end"]) <= REFERENCE


def test_only_catalog_supplements_and_ucum_units_are_used() -> None:
    for seed in range(30):
        for resource in added(add_practice_data(bundle(), seed, REFERENCE)):
            if resource["resourceType"] != "MedicationStatement":
                continue
            code = resource["medicationCodeableConcept"]["coding"][0]["code"]
            assert code in CATALOG
            for rate in resource["dosage"][0].get("doseAndRate", []):
                assert rate["doseQuantity"]["system"] == "http://unitsofmeasure.org"


def test_every_protocol_draws_only_on_catalog_supplements() -> None:
    assert {s for p in PROTOCOLS for s in p.supplements} <= set(CATALOG)


def test_the_loader_rewrites_the_new_references_like_any_other() -> None:
    result = add_practice_data(bundle(), 5, REFERENCE)

    put = to_put_bundle(result, IdentifierIndex())

    plan = next(e["resource"] for e in put["entry"] if e["resource"]["resourceType"] == "CarePlan")
    statement_ids = {
        e["resource"]["id"]
        for e in put["entry"]
        if e["resource"]["resourceType"] == "MedicationStatement"
    }
    assert plan["subject"]["reference"] == f"Patient/{PATIENT_ID}"
    refs = [a["reference"]["reference"] for a in plan["activity"] if "reference" in a]
    assert {r.removeprefix("MedicationStatement/") for r in refs} <= statement_ids


# -- normalizers ------------------------------------------------------------------------------


def project(resource: dict[str, Any]) -> list[Any]:
    # The loader rewrites references before the server sees them; do the same here.
    put = to_put_bundle(add_practice_data(bundle(), 5, REFERENCE), IdentifierIndex())
    rewritten = {e["resource"]["id"]: e["resource"] for e in put["entry"]}[resource["id"]]
    rewritten = {**rewritten, **{k: v for k, v in resource.items() if k == "category"}}
    record = SourceRecord.from_payload(
        SOURCE,
        rewritten["resourceType"],
        rewritten["id"],
        "1",
        None,
        json.dumps(rewritten).encode(),
    )
    return list(build_projector(ZoneInfo("America/New_York"))(record))


def generated(seed: int = 5) -> list[dict[str, Any]]:
    return added(add_practice_data(bundle(), seed, REFERENCE))


def test_a_generated_supplement_becomes_a_supplement_row() -> None:
    statement = next(r for r in generated() if r["resourceType"] == "MedicationStatement")

    [row] = project(statement)

    assert row.kind is TimelineKind.SUPPLEMENT
    assert row.code_system == statement["medicationCodeableConcept"]["coding"][0]["system"]
    assert row.code == statement["medicationCodeableConcept"]["coding"][0]["code"]
    assert row.status == statement["status"]
    assert row.occurred is not None


def test_a_generated_protocol_becomes_a_protocol_row_named_by_its_specific_category() -> None:
    plan = next(r for r in generated() if r["resourceType"] == "CarePlan")

    [row] = project(plan)

    assert row.kind is TimelineKind.PROTOCOL
    assert row.code == plan["category"][1]["coding"][0]["code"]
    assert row.code_system != PRACTICE_CATEGORY_SYSTEM


def test_a_protocols_supplements_are_in_its_sealed_detail_not_in_a_plaintext_column() -> None:
    plan = next(r for r in generated() if r["resourceType"] == "CarePlan")

    [row] = project(plan)

    assert row.detail_json is not None
    detail = json.loads(row.detail_json)
    assert len(detail["supplements"]) == sum("reference" in a for a in plan["activity"])
    assert all(r.startswith("MedicationStatement/") for r in detail["supplements"])
    assert row.value_text is None


def test_an_ordinary_medication_statement_stays_a_medication() -> None:
    statement = next(r for r in generated() if r["resourceType"] == "MedicationStatement")
    ordinary = {**statement, "category": {"coding": [{"system": "x", "code": SUPPLEMENT_CATEGORY}]}}

    [row] = project(ordinary)

    assert row.kind is TimelineKind.MEDICATION


def test_an_ordinary_care_plan_stays_a_care_plan() -> None:
    plan = next(r for r in generated() if r["resourceType"] == "CarePlan")
    ordinary = {**plan, "category": [{"coding": [{"system": "x", "code": PROTOCOL_CATEGORY}]}]}

    [row] = project(ordinary)

    assert row.kind is TimelineKind.CARE_PLAN
