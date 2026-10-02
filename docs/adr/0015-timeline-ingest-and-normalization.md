# 0015. How ingest and normalization work: what is projected, what is skipped, and how a run behaves

Status: Accepted. Builds the ingest command that [0014](0014-field-encryption-implementation.md) listed under "Not built yet".

## Context
[0003](0003-provenance-snapshots-and-heads.md) defined snapshots, heads and the projected timeline, [0004](0004-clinical-time-and-timezones.md) the clinical time, and [0014](0014-field-encryption-implementation.md) the sealer. Joining them needed answers to what each resource type becomes, what is left out, how a patient is found again, and how a run behaves when something fails or is repeated. Figures below were measured on 2026-10-02 against the committed dataset ([0012](0012-synthetic-dataset.md)).

## Decision
**What becomes a timeline row**

| Resource | Rows | Kind |
| --- | --- | --- |
| Encounter | 789 | `encounter` |
| Condition | 695 | `condition` |
| Observation, `laboratory` | 6,385 | `lab` |
| Observation, `vital-signs` | 1,784 | `vital` |
| MedicationRequest | 855 | `medication` |
| Procedure | 2,202 | `procedure` |
| Immunization | 147 | `immunization` |
| AllergyIntolerance | 12 | `allergy` |
| CarePlan | 70 | `care_plan` |
| Patient | 0 | none: it feeds the patient's sealed identity |
| **Total** | **12,939** from 13,708 resources | |

- The resources per type are 28 Patient, 789 Encounter, 695 Condition, 8,910 Observation, 855 MedicationRequest, 2,202 Procedure, 147 Immunization, 12 AllergyIntolerance and 70 CarePlan. They sum to the 13,708 of [0012](0012-synthetic-dataset.md) and [0013](0013-fhir-seeding-and-adapter-limits.md), which gave totals only.
- **Observation categories.** `laboratory` and `vital-signs` are projected. Survey (707 resources), social-history (205), exam (23) and procedure (7) are not: they are questionnaires and history the summary does not trend. Their snapshots are stored like any other.
- **Components.** Each component of a panel is its own row at `component[i]`. A panel with no value of its own (a blood pressure) produces no row for itself, so 201 blood pressures give 402 rows and one single-component panel gives one.
- **Units: a UCUM code or no number.** A quantity becomes `value_numeric` and `value_unit` only when its system is `http://unitsofmeasure.org` and it has a code. Any other quantity keeps its text and no number, because a number in a unit that cannot be compared must never be trended. Every quantity in the dataset is UCUM. Numbers are parsed from their source text, so `1.0` keeps its precision.
- **Reference ranges and interpretation** are carried when present (the dataset has none). Range bounds count only in the value's own UCUM unit and only when ordered.
- **Value kinds.** Quantity, integer, string, boolean and coded values are carried. Ranges, ratios and sampled data carry no value; the snapshot keeps them.
- **Medications.** A request names its drug from its `medicationCodeableConcept` or from the contained `Medication` its reference points at ([0012](0012-synthetic-dataset.md) inlines them). A reference that resolves to nothing inside the record is an error, not a row without a drug. Dosage text is the sealed `value_text`; the structured dosage is the sealed detail. MedicationStatement is handled the same way; the dataset has none, so its test uses a statement derived from a dataset request.
- **Care plans get their own kind**, added by migration 0003, because `protocol` is reserved for the practice's supplement protocols in 2b. A plan is named by its specific category, not US Core's generic `assess-plan`; its activities go in the sealed detail.
- **Times.** Every time goes through `parse_fhir_datetime` and keeps its precision. A row sorts by its clinical time, else by when it was recorded, else by the source's update time, and with none of the three it is refused. The fallbacks do not occur in the dataset. A malformed time, or a clock time with no offset, is an error whose message names the resource and path and never the value.
- An unknown resource type has no normalizer and raises, so a new type cannot be dropped silently.

**Patients**
- A source patient is found by its `patient_source_link` and created, sealed and linked on first sight. **There is no matching across sources**: with one source it could only add a risk of merging two people. Matching belongs with the second source, and is a policy decision for 2b.
- An existing patient's identity is sealed again **only when the Patient record's head moved**. Sealing uses a fresh nonce, so sealing on every run would change every identity ciphertext for no change in content.
- A birth date that is a year or a month alone is refused: it is a valid FHIR date but not one a lookup can use.

**How a run behaves**
- **One transaction per patient.** Records are fetched first, outside any transaction. Then one transaction resolves the patient, loads their key, and ingests every record. A patient whose record fails rolls back alone; the run finishes the others, ends `failed` and exits non-zero. Nothing is half-written.
- **Re-running changes nothing** except `last_seen_at` on the heads and one new `import_run` row, which is the log of the run. Identical content hashes to the stored snapshot, so nothing is inserted, no head moves, nothing is projected and no identity is sealed again. A live test restarts fhir-candle, reseeds and re-ingests, and expects zero new snapshots and unmoved heads.
- **One run at a time.** A session-level Postgres advisory lock turns a second concurrent run away before it records anything.
- **Bounded concurrency, at most four patients at once.** That is the FHIR adapter's cap of four open listings ([0013](0013-fhir-seeding-and-adapter-limits.md)), and a fifth would evict a listing mid-read. The patient list is read in full first so its snapshot is not competing. An expired or throttled listing is read again, up to three times.
- **Real crypto only.** The command builds its keys, key ring and sealer from the environment itself and takes no sealer as input. No production code can construct the test sealer, and a test fails if it is mentioned.
- **Keys reach only the `ingest` service.** The API decrypts nothing yet, so it gets no keys until it does. The API waits for `ingest` to complete, so a healthy API means an ingested timeline.

## Measurements (2026-10-02)
On a host with load average 5 to 12, with the API image already built:

| Measure | Result |
| --- | --- |
| `docker compose up -d --wait` from empty volumes | 3 min 57 s (seed 10 s, ingest 110 s) |
| `docker compose up -d --wait` from a fresh clone, images rebuilt from warm layer cache | 4 min 53 s, healthy, 12,939 timeline rows |
| Ingest from empty, concurrency 4 | 106 s, 13,708 snapshots, 12,939 timeline rows |
| Re-run, concurrency 4 | 76 s and 60 s on two runs, 0 snapshots, 0 heads moved |
| Re-run, concurrency 1 | 133 s |
| Memory during a run | ingest 83 MiB (`docker stats`) and 106 MiB peak RSS, of 256 MiB; fhir-candle peaked at 511 MiB of 768 MiB; postgres 96 MiB |

## Consequences
- **A record deleted at the source is not noticed.** The FHIR adapter cannot say what changed ([0013](0013-fhir-seeding-and-adapter-limits.md)), and ingest only ever upserts, so a resource that disappears keeps its head and its timeline rows. The `deleted` record state exists in the schema and nothing sets it yet.
- A patient's records are held in memory while that patient is ingested. The largest synthetic patient has 2,292 records; four at once fit easily. A source with far larger histories needs streaming per kind.
- A re-run is bound by reading the source (about 60 to 76 s of the 106 s), not by writing.
- The test that ingests the whole dataset twice takes about 2.5 minutes, which is the bulk of the API CI job's added time.
- Practitioners and organizations are not ingested. The adapter reads per patient, and nothing in the timeline refers to them yet.
- Only `migrate` builds the API image; `api`, `seed` and `ingest` run it with `pull_policy: never`. With four services building one tag, a fresh clone's first `up` failed on naming the image, so this is what makes the first start work. The cost is that those three services depend on `migrate` having been part of the `up`.
- Concurrency 4 is the adapter's ceiling, not a tuned optimum. A different source needs its own number.
