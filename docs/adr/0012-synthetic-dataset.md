# 0012. A pinned, trimmed Synthea dataset, committed

Status: Accepted

## Context
The stack needs a population of synthetic patients with years of history, the same on every machine, and a clean clone has to reach a seeded, healthy stack within about five minutes. [Synthea](https://github.com/synthetichealth/synthea) generates such patients and exports FHIR R4. It runs on Java, and nothing in this project's tooling should require Java on the host.

Two ways to supply the data:
- **Commit the generated data.** A clean clone needs no download and no generation step.
- **Generate once into a cached volume.** The repository stays small, but the first `up` needs a 201 MB jar, a JRE image and a run of the generator.

## Measurements (2026-10-02)
Synthea v4.0.0, seed 20260901, reference date 2026-09-01, five years of history, FHIR R4 export only, in a container with no network.

| Measure | Result |
| --- | --- |
| Patients with `-p 25` | 33 (25 living, 8 died during the simulation) |
| Patients with `-p 20` | 28 (20 living, 8 died), the chosen population |
| Generation time | about 35 s with the jar cached, about 37 s end to end including the trim |
| Raw FHIR output, `-p 20` / `-p 25` | 60 MB / 65 MB |
| Share of raw output that is claims (`ExplanationOfBenefit`, `Claim`) | 19.5 MB of 46.6 MB of resources, for `-p 25` |
| After trimming, `-p 20` | 13,994 resources, 16.3 MB of JSON, 1.4 MB committed as gzip |
| Reproducibility | two runs gave byte-identical patient files; only the two shared-bundle file names carry a timestamp, and their contents match |

## Decision
- **Commit the trimmed dataset** under `data/synthea/`. The rule was: commit if the committed size is at most 10 MB or generation is not reproducible. Both pointed the same way, with 1.4 MB committed.
- **What is committed:**
  - `shared.json.gz`: practitioners, organizations and locations, which every patient bundle references.
  - `patients/<patient-id>.json.gz`: one transaction bundle per patient, as Synthea wrote it.
  - `MANIFEST.sha256`: a checksum of each file's decompressed JSON. Gzip bytes can differ between zlib versions; the content cannot.
- **Trimmed types:** claims, explanations of benefit, diagnostic reports, document references, provenance, imaging studies, devices, supply deliveries, medication administrations, care teams and practitioner roles. The product never reads them. A care plan's reference to a dropped care team is removed with it.
- **Kept types:** Patient, Encounter, Condition, Observation, MedicationRequest, Procedure, Immunization, AllergyIntolerance, CarePlan, Practitioner, Organization, Location.
- **Medications are inlined.** 272 of the 855 medication requests name their medication only through a reference to a separate `Medication` resource, with no display text. A normalizer sees one record at a time, and a citation points at one snapshot, so the medication has to be inside the request. Trimming copies each referenced `Medication` into the request's `contained` list (without server metadata) and rewrites the reference to `#<id>`. The standalone `Medication` resources are then dropped. fhir-candle stores and returns contained resources.
- **Otherwise the files stay in Synthea's own shape** (`POST` entries, `urn:uuid` references). The loader rewrites them on every start ([0013](0013-fhir-seeding-and-adapter-limits.md)).
- **Every input is pinned in `data/synthea/generate.sh`:**
  - the jar version and its SHA-256
  - the JRE image digest
  - the seed, the clinician seed, the reference date and the population
  - the exporters, in `synthea.properties`
  - no module filter, so the jar's built-in modules run and its checksum pins them
- **`generate.sh --check`** regenerates into a scratch directory and compares the manifest with the committed one.
- **Licence:** Synthea is Apache-2.0, and its output describes no real person.

## Consequences
- A clean clone needs neither Java, nor the jar, nor network access to seed the stack. Every run loads identical data.
- The dataset changes only when an input in `generate.sh` changes, and that shows up as a manifest diff in review.
- Deaths during the simulation are part of the population: 8 of the 28 patients are deceased. That is useful for the timeline, and it means "20 patients" means 20 living.
- Synthea generates plausible identifiers (record numbers, SSN-shaped values, licence numbers). They are synthetic, and they are exactly the values field encryption protects ([0008](0008-field-level-encryption.md)).
- Regenerating needs Docker, uv and a 201 MB download once. `--check` is not part of CI yet.
- Trimming happens at generation time, so the committed bundles can be loaded without Synthea's claim resources or the Java toolchain.
