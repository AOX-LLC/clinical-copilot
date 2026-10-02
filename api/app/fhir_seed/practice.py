"""Synthetic supplement regimens and practice protocols, added to the committed dataset.

A small deterministic generator, run by ``python -m app.fhir_seed prepare`` after the Synthea
bundles are trimmed (ADR 0016). The same seed and reference date give the same resources, so
``generate.sh --check`` covers them with the rest of the dataset.

Each adult patient who is not deceased gets one current protocol, and some get an earlier,
finished one. A protocol is a FHIR ``CarePlan`` and each supplement it contains is a
``MedicationStatement`` the plan references; the normalizers tell them from ordinary care
plans and medications by the practice category they carry. The catalog names generic
substances only. Doses and regimens are made up and describe no real patient.
"""

import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.timeline.normalize.practice import (
    PRACTICE_CATEGORY_SYSTEM,
    PROTOCOL_CATEGORY,
    PROTOCOL_SYSTEM,
    SUPPLEMENT_CATEGORY,
    SUPPLEMENT_SYSTEM,
)

Json = dict[str, Any]

# Fixed, so a resource's id depends only on the patient, the protocol and the item.
ID_NAMESPACE = uuid.UUID("5a3f0c1e-8d2b-4c47-9a60-3b1e7d2f4c90")
UCUM = "http://unitsofmeasure.org"
ADULT_YEARS = 18
CURRENT_PROTOCOL_START_DAYS = (120, 540)  # how long before the reference date it began
EARLIER_PROTOCOL_CHANCE = 0.4
SUPPLEMENT_STOPPED_CHANCE = 0.3


@dataclass(frozen=True, slots=True)
class Dose:
    text: str
    value: int | None = None  # None: no UCUM unit fits (live cultures), so no structured dose
    unit: str | None = None
    ucum: str | None = None


@dataclass(frozen=True, slots=True)
class Supplement:
    code: str
    display: str
    route: str
    doses: tuple[Dose, ...]
    frequency: tuple[int, int, str]  # times per period, period, unit (FHIR UnitsOfTime)
    when: str
    reason: str


@dataclass(frozen=True, slots=True)
class Protocol:
    code: str
    name: str
    description: str
    supplements: tuple[str, ...]
    follow_up: str


CATALOG: dict[str, Supplement] = {
    s.code: s
    for s in (
        Supplement(
            "vitamin-d3",
            "Vitamin D3 (cholecalciferol)",
            "Oral",
            (Dose("2000 IU", 2000, "IU", "[IU]"), Dose("5000 IU", 5000, "IU", "[IU]")),
            (1, 1, "d"),
            "once daily with a meal",
            "Maintain vitamin D status",
        ),
        Supplement(
            "magnesium-glycinate",
            "Magnesium glycinate",
            "Oral",
            (Dose("200 mg", 200, "mg", "mg"), Dose("400 mg", 400, "mg", "mg")),
            (1, 1, "d"),
            "at bedtime",
            "Sleep and muscle recovery support",
        ),
        Supplement(
            "omega-3",
            "Omega-3 fatty acids (EPA and DHA)",
            "Oral",
            (Dose("1000 mg", 1000, "mg", "mg"), Dose("2000 mg", 2000, "mg", "mg")),
            (1, 1, "d"),
            "once daily with a meal",
            "Lipid and inflammatory marker support",
        ),
        Supplement(
            "methylcobalamin",
            "Methylcobalamin (vitamin B12)",
            "Oral",
            (Dose("1000 mcg", 1000, "ug", "ug"),),
            (1, 1, "d"),
            "once daily",
            "Maintain B12 status",
        ),
        Supplement(
            "l-methylfolate",
            "L-methylfolate",
            "Oral",
            (Dose("800 mcg", 800, "ug", "ug"),),
            (1, 1, "d"),
            "once daily",
            "Folate support",
        ),
        Supplement(
            "zinc-picolinate",
            "Zinc picolinate",
            "Oral",
            (Dose("15 mg", 15, "mg", "mg"), Dose("30 mg", 30, "mg", "mg")),
            (1, 1, "d"),
            "once daily with food",
            "Immune support",
        ),
        Supplement(
            "vitamin-c",
            "Vitamin C (ascorbic acid)",
            "Oral",
            (Dose("500 mg", 500, "mg", "mg"),),
            (2, 1, "d"),
            "twice daily",
            "Immune support",
        ),
        Supplement(
            "coenzyme-q10",
            "Coenzyme Q10 (ubiquinone)",
            "Oral",
            (Dose("100 mg", 100, "mg", "mg"), Dose("200 mg", 200, "mg", "mg")),
            (1, 1, "d"),
            "once daily with a meal",
            "Energy and cardiovascular support",
        ),
        Supplement(
            "creatine",
            "Creatine monohydrate",
            "Oral",
            (Dose("5 g", 5, "g", "g"),),
            (1, 1, "d"),
            "once daily",
            "Strength and recovery support",
        ),
        Supplement(
            "probiotic",
            "Multi-strain probiotic",
            "Oral",
            (Dose("1 capsule"),),
            (1, 1, "d"),
            "once daily",
            "Digestive health support",
        ),
        Supplement(
            "curcumin",
            "Curcumin (turmeric extract)",
            "Oral",
            (Dose("500 mg", 500, "mg", "mg"),),
            (2, 1, "d"),
            "twice daily with meals",
            "Inflammatory marker support",
        ),
        Supplement(
            "ashwagandha",
            "Ashwagandha (Withania somnifera) extract",
            "Oral",
            (Dose("300 mg", 300, "mg", "mg"),),
            (1, 1, "d"),
            "once daily in the evening",
            "Stress and sleep support",
        ),
    )
}

PROTOCOLS: tuple[Protocol, ...] = (
    Protocol(
        "foundational-micronutrient",
        "Foundational micronutrient protocol",
        "Replete the common deficiencies, then maintain.",
        ("vitamin-d3", "magnesium-glycinate", "omega-3", "methylcobalamin", "l-methylfolate"),
        "Recheck vitamin D and B12 in 12 weeks",
    ),
    Protocol(
        "sleep-recovery",
        "Sleep and recovery protocol",
        "Support sleep quality and overnight recovery.",
        ("magnesium-glycinate", "ashwagandha", "vitamin-d3"),
        "Review sleep log in 8 weeks",
    ),
    Protocol(
        "metabolic-support",
        "Metabolic support protocol",
        "Support lipid and inflammatory markers alongside diet changes.",
        ("omega-3", "curcumin", "coenzyme-q10", "vitamin-d3"),
        "Repeat lipid panel in 12 weeks",
    ),
    Protocol(
        "seasonal-immune",
        "Seasonal immune protocol",
        "Short seasonal course to support immune function.",
        ("vitamin-c", "zinc-picolinate", "vitamin-d3", "probiotic"),
        "Stop at the end of the season",
    ),
    Protocol(
        "performance-recovery",
        "Performance and recovery protocol",
        "Support training load and recovery.",
        ("creatine", "omega-3", "coenzyme-q10", "magnesium-glycinate"),
        "Review training log in 8 weeks",
    ),
)


def add_practice_data(bundle: Mapping[str, Any], seed: int, reference_date: date) -> Json:
    """Return ``bundle`` with the patient's supplement regimens and protocols appended.

    A patient who is deceased, under 18 at the reference date, or without a birth date gets
    none. The result depends only on the arguments, so it is reproducible.
    """
    patient = _the_patient(bundle)
    entries = list(bundle["entry"])
    if _is_eligible(patient, reference_date):
        patient_id = str(patient["id"])
        rng = random.Random(f"{seed}:{patient_id}")  # noqa: S311  # not for security
        for resource in _protocols_for(patient_id, rng, reference_date):
            entries.append(
                {
                    "fullUrl": f"urn:uuid:{resource['id']}",
                    "resource": resource,
                    "request": {"method": "POST", "url": resource["resourceType"]},
                }
            )
    return {**bundle, "entry": entries}


def _the_patient(bundle: Mapping[str, Any]) -> Json:
    patients = [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    if len(patients) != 1:
        raise ValueError("a patient bundle must hold exactly one Patient")
    patient: Json = patients[0]
    return patient


def _is_eligible(patient: Mapping[str, Any], reference_date: date) -> bool:
    if "deceasedDateTime" in patient or patient.get("deceasedBoolean") is True:
        return False
    try:
        born = date.fromisoformat(str(patient["birthDate"]))
    except (KeyError, ValueError):
        return False
    birthday_passed = (reference_date.month, reference_date.day) >= (born.month, born.day)
    return reference_date.year - born.year - (not birthday_passed) >= ADULT_YEARS


def _protocols_for(patient_id: str, rng: random.Random, reference_date: date) -> list[Json]:
    low, high = CURRENT_PROTOCOL_START_DAYS
    current_start = reference_date - timedelta(days=rng.randint(low, high))
    chosen = rng.sample(PROTOCOLS, 2)
    resources: list[Json] = []
    if rng.random() < EARLIER_PROTOCOL_CHANCE:
        earlier_end = current_start - timedelta(days=rng.randint(1, 14))
        earlier_start = earlier_end - timedelta(days=rng.randint(60, 180))
        resources += _protocol(
            patient_id, 0, chosen[1], rng, earlier_start, earlier_end, reference_date
        )
    resources += _protocol(patient_id, 1, chosen[0], rng, current_start, None, reference_date)
    return resources


def _protocol(
    patient_id: str,
    index: int,
    protocol: Protocol,
    rng: random.Random,
    start: date,
    end: date | None,
    reference_date: date,
) -> list[Json]:
    codes = _pick_supplements(protocol, rng)
    statements = [
        _statement(patient_id, index, CATALOG[code], rng, start, end, reference_date)
        for code in codes
    ]
    plan_id = _id(patient_id, f"protocol:{index}")
    plan: Json = {
        "resourceType": "CarePlan",
        "id": plan_id,
        "status": "completed" if end else "active",
        "intent": "plan",
        "category": [
            {
                "coding": [
                    {
                        "system": PRACTICE_CATEGORY_SYSTEM,
                        "code": PROTOCOL_CATEGORY,
                        "display": "Practice protocol",
                    }
                ]
            },
            {
                "coding": [
                    {"system": PROTOCOL_SYSTEM, "code": protocol.code, "display": protocol.name}
                ],
                "text": protocol.name,
            },
        ],
        "title": protocol.name,
        "description": protocol.description,
        "subject": {"reference": f"urn:uuid:{patient_id}"},
        "created": start.isoformat(),
        "period": {"start": start.isoformat(), **({"end": end.isoformat()} if end else {})},
        "activity": [
            *({"reference": {"reference": f"urn:uuid:{s['id']}"}} for s in statements),
            {"detail": {"code": {"text": protocol.follow_up}, "status": "scheduled"}},
        ],
    }
    return [plan, *statements]


def _pick_supplements(protocol: Protocol, rng: random.Random) -> Sequence[str]:
    count = rng.randint(min(3, len(protocol.supplements)), len(protocol.supplements))
    chosen = set(rng.sample(protocol.supplements, count))
    return [code for code in protocol.supplements if code in chosen]  # keep the catalog's order


def _statement(
    patient_id: str,
    index: int,
    supplement: Supplement,
    rng: random.Random,
    protocol_start: date,
    protocol_end: date | None,
    reference_date: date,
) -> Json:
    started = protocol_start + timedelta(days=rng.randint(0, 14))
    dose = rng.choice(supplement.doses)
    ended = protocol_end
    status = "completed" if protocol_end else "active"
    if protocol_end is None and rng.random() < SUPPLEMENT_STOPPED_CHANCE:
        stop_after = rng.randint(30, 120)
        ended = min(started + timedelta(days=stop_after), reference_date)
        status = "stopped"
    times, period, unit = supplement.frequency
    dosage: Json = {
        "text": f"{dose.text} by mouth {supplement.when}",
        "timing": {"repeat": {"frequency": times, "period": period, "periodUnit": unit}},
        "route": {"text": supplement.route},
    }
    if dose.value is not None:
        dosage["doseAndRate"] = [
            {
                "doseQuantity": {
                    "value": dose.value,
                    "unit": dose.unit,
                    "system": UCUM,
                    "code": dose.ucum,
                }
            }
        ]
    statement: Json = {
        "resourceType": "MedicationStatement",
        "id": _id(patient_id, f"supplement:{index}:{supplement.code}"),
        "status": status,
        "category": {
            "coding": [
                {
                    "system": PRACTICE_CATEGORY_SYSTEM,
                    "code": SUPPLEMENT_CATEGORY,
                    "display": "Supplement",
                }
            ]
        },
        "medicationCodeableConcept": {
            "coding": [
                {
                    "system": SUPPLEMENT_SYSTEM,
                    "code": supplement.code,
                    "display": supplement.display,
                }
            ],
            "text": supplement.display,
        },
        "subject": {"reference": f"urn:uuid:{patient_id}"},
        "effectivePeriod": {
            "start": started.isoformat(),
            **({"end": ended.isoformat()} if ended else {}),
        },
        "dateAsserted": f"{started.isoformat()}T12:00:00+00:00",
        "reasonCode": [{"text": supplement.reason}],
        "dosage": [dosage],
    }
    return statement


def _id(patient_id: str, name: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{patient_id}:{name}"))
