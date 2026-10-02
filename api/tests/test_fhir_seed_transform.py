"""Bundle transforms from Synthea output to loadable PUT transactions. Synthetic data only."""

import copy
from typing import Any

import pytest

from app.fhir_seed.transform import (
    IdentifierIndex,
    SeedError,
    to_put_bundle,
    trim_bundle,
)

PATIENT_ID = "11111111-0000-4000-8000-000000000001"
ENCOUNTER_ID = "11111111-0000-4000-8000-000000000002"
CLAIM_ID = "11111111-0000-4000-8000-000000000003"
CARE_TEAM_ID = "11111111-0000-4000-8000-000000000004"
CARE_PLAN_ID = "11111111-0000-4000-8000-000000000005"
PRACTITIONER_ID = "22222222-0000-4000-8000-000000000001"
ORGANIZATION_ID = "22222222-0000-4000-8000-000000000002"
NPI_QUERY = "Practitioner?identifier=http://example.test/npi|0000000001"
UNKNOWN_QUERY = "Practitioner?identifier=http://example.test/npi|0000000042"


def _entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "fullUrl": f"urn:uuid:{resource['id']}",
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"]},
    }


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [_entry(resource) for resource in resources],
    }


def _shared() -> IdentifierIndex:
    practitioner = {
        "resourceType": "Practitioner",
        "id": PRACTITIONER_ID,
        "identifier": [{"system": "http://example.test/npi", "value": "0000000001"}],
    }
    organization = {
        "resourceType": "Organization",
        "id": ORGANIZATION_ID,
        "identifier": [{"system": "http://example.test/org", "value": "org-1"}],
    }
    return IdentifierIndex.from_bundles([_bundle(practitioner, organization)])


def _patient_bundle() -> dict[str, Any]:
    patient = {"resourceType": "Patient", "id": PATIENT_ID}
    encounter = {
        "resourceType": "Encounter",
        "id": ENCOUNTER_ID,
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "participant": [{"individual": {"reference": NPI_QUERY}}],
        "serviceProvider": {"reference": "Organization?identifier=http://example.test/org|org-1"},
    }
    claim = {
        "resourceType": "Claim",
        "id": CLAIM_ID,
        "patient": {"reference": f"urn:uuid:{PATIENT_ID}"},
    }
    care_team = {"resourceType": "CareTeam", "id": CARE_TEAM_ID}
    care_plan = {
        "resourceType": "CarePlan",
        "id": CARE_PLAN_ID,
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "careTeam": [{"reference": f"urn:uuid:{CARE_TEAM_ID}"}],
        "activity": [],
    }
    return _bundle(patient, encounter, claim, care_team, care_plan)


def test_trim_drops_types_the_product_does_not_read() -> None:
    trimmed = trim_bundle(_patient_bundle())

    types = [entry["resource"]["resourceType"] for entry in trimmed["entry"]]
    assert types == ["Patient", "Encounter", "CarePlan"]


def test_trim_removes_references_to_dropped_resources_and_the_list_they_emptied() -> None:
    trimmed = trim_bundle(_patient_bundle())

    care_plan = trimmed["entry"][2]["resource"]
    assert "careTeam" not in care_plan
    assert care_plan["subject"] == {"reference": f"urn:uuid:{PATIENT_ID}"}


def test_trim_leaves_the_input_untouched() -> None:
    original = _patient_bundle()
    snapshot = copy.deepcopy(original)

    trim_bundle(original)

    assert original == snapshot


def test_every_entry_becomes_a_put_on_its_own_id() -> None:
    loadable = to_put_bundle(trim_bundle(_patient_bundle()), _shared())

    assert loadable["type"] == "transaction"
    assert [(e["request"]["method"], e["request"]["url"]) for e in loadable["entry"]] == [
        ("PUT", f"Patient/{PATIENT_ID}"),
        ("PUT", f"Encounter/{ENCOUNTER_ID}"),
        ("PUT", f"CarePlan/{CARE_PLAN_ID}"),
    ]
    assert all(e["fullUrl"] == e["request"]["url"] for e in loadable["entry"])


def test_urn_references_become_type_slash_id() -> None:
    loadable = to_put_bundle(trim_bundle(_patient_bundle()), _shared())

    encounter = loadable["entry"][1]["resource"]
    assert encounter["subject"] == {"reference": f"Patient/{PATIENT_ID}"}


def test_conditional_references_resolve_through_the_shared_bundles() -> None:
    loadable = to_put_bundle(trim_bundle(_patient_bundle()), _shared())

    encounter = loadable["entry"][1]["resource"]
    assert encounter["participant"][0]["individual"] == {
        "reference": f"Practitioner/{PRACTITIONER_ID}"
    }
    assert encounter["serviceProvider"] == {"reference": f"Organization/{ORGANIZATION_ID}"}


def test_the_rewrite_leaves_no_urn_or_conditional_reference_behind() -> None:
    loadable = to_put_bundle(trim_bundle(_patient_bundle()), _shared())

    serialized = str(loadable)
    assert "urn:uuid" not in serialized
    assert "?identifier" not in serialized


def test_an_unmatched_conditional_reference_is_refused_without_echoing_its_identifier() -> None:
    bundle = _patient_bundle()
    bundle["entry"][1]["resource"]["participant"][0]["individual"]["reference"] = UNKNOWN_QUERY

    with pytest.raises(SeedError) as raised:
        to_put_bundle(bundle, _shared())

    assert "Practitioner" in str(raised.value)
    assert "0000000042" not in str(raised.value)


def test_a_urn_reference_outside_the_bundle_is_refused() -> None:
    bundle = _patient_bundle()
    bundle["entry"][1]["resource"]["subject"]["reference"] = f"urn:uuid:{'f' * 8}-0000-4000-8000-0"

    with pytest.raises(SeedError, match="outside the bundle"):
        to_put_bundle(bundle, _shared())


def test_an_entry_that_does_not_carry_its_own_id_in_full_url_is_refused() -> None:
    bundle = _patient_bundle()
    bundle["entry"][0]["fullUrl"] = f"urn:uuid:{CLAIM_ID}"

    with pytest.raises(SeedError, match="fullUrl"):
        to_put_bundle(bundle, _shared())


def test_the_rewrite_is_deterministic_and_does_not_touch_its_input() -> None:
    original = trim_bundle(_patient_bundle())
    snapshot = copy.deepcopy(original)

    first = to_put_bundle(original, _shared())
    second = to_put_bundle(original, _shared())

    assert first == second
    assert original == snapshot
