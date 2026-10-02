# 0016. Supplement regimens and practice protocols as FHIR resources, generated deterministically

Status: Accepted. Supersedes one sentence of [0015](0015-timeline-ingest-and-normalization.md): "`protocol` is reserved for the practice's supplement protocols" now has an implementation.

## Context
The timeline has `supplement` and `protocol` kinds, and Synthea produces neither: it has no notion of a practice's supplement regimens. The summary and the protocol history need them, and they have to arrive through the same adapter and normalizers as everything else, so a real source can later supply them the same way.

## Decision
- **A supplement regimen is a `MedicationStatement`** with a `category` coded `supplement` in a code system of this project's own (`.../CodeSystem/practice-category`). The substance is coded in a second local system, with the display name carried as a plain generic substance name (no brands). Dose text, a structured dose where a UCUM unit exists, route, timing, reason and an effective period are filled in.
- **A protocol is a `CarePlan`** with a category coded `practice-protocol` in the same system, plus a second category naming the specific protocol. Its `activity` entries reference the supplement statements it contains, and one detail entry carries the follow-up step.
- **Why these two types.** The FHIR adapter already reads both, fhir-candle stores them, the loader already rewrites their references, and the normalizers already handle them. A new type would have needed an adapter change, a contract-suite change and a normalizer for no gain. The cost is that the meaning rides on a category code, so a statement or plan without it is an ordinary medication or care plan, and a test pins that.
- **Normalizers.** `medication.py` emits `supplement` for a statement carrying the supplement category and `medication` otherwise. `care_plan.py` emits `protocol` for a plan carrying the protocol category, names it by its specific category, and keeps the referenced supplements in the sealed detail. Everything else keeps its existing kind, and Synthea's care plans are unchanged.
- **The generator** (`api/app/fhir_seed/practice.py`) runs inside `python -m app.fhir_seed prepare`, after Synthea's output is trimmed. It is a pure function of a seed, a reference date and the patient bundle: `random.Random` seeded from the seed and the patient id, and `uuid5` ids. It gives each living adult one current protocol of two to five supplements (some stopped partway) and some an earlier finished one. A deceased patient, a minor and a patient without a birth date get none.
- **Pinned like Synthea.** `data/synthea/generate.sh` carries `PRACTICE_SEED` and passes it and the existing reference date to `prepare`. The catalog and rules are in the generator, and `generate.sh --check` regenerates everything and compares the manifest, so a change to either shows up as a manifest diff.
- **Catalog.** Twelve generic supplements and five protocols. Doses and regimens are invented and describe no real patient.

## Measurements (2026-10-02)
| Measure | Result |
| --- | --- |
| Patients given regimens | 17 of 28 (the 8 deceased and 3 under 18 get none) |
| Added resources | 95: 22 protocols (`CarePlan`) and 73 supplements (`MedicationStatement`) |
| Dataset size | 13,803 resources in patient bundles, up from 13,708 |
| Timeline rows | 13,034, up from 12,939: 73 `supplement`, 22 `protocol`; `care_plan` stays 70 and `medication` 855 |
| Effect on the rest of the dataset | Only the 17 bundles of patients who received regimens changed; the Synthea content of every bundle is byte-identical to before |

## Consequences
- The per-type counts in the tests and in [0015](0015-timeline-ingest-and-normalization.md) change by these amounts. Accepted ADRs are not edited, so this record carries the new figures.
- A protocol's supplements are linked by references in its sealed detail, not by a column. A query that joins a protocol to its supplements reads them through the detail, which is Phase 3's concern.
- Nothing in the generator reads a real record. If a real practice's regimens arrive through Healthie, they come as medications and care plans ([0018](0018-healthie-adapter.md)) and need their own normalizers, which this record does not build.
