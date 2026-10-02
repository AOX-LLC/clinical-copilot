# 0013. Seed fhir-candle with PUT transactions, and adapt to what it cannot do

Status: Accepted. Supersedes the loader sentence in [0002](0002-fhir-server-and-memory-budget.md) ("Phase 2's loader uses individual PUTs").

## Context
[0002](0002-fhir-server-and-memory-budget.md) chose fhir-candle and warned that its search and transaction support is narrower than HAPI's. The FHIR adapter and the loader can only be designed around what the server actually does, so each behavior they depend on was tested against the pinned image (version 0.2026.708.1349) loaded with the dataset from [0012](0012-synthetic-dataset.md).

## Measurements (2026-10-02)
| Behavior | Result |
| --- | --- |
| `PUT Type/<id>` with a client-assigned id | Works. 201 on create, 200 on update. `versionId` goes up on every PUT and restarts at 1 after a server restart. |
| Transaction bundle of PUT entries | Works. All 13,994 resources of the dataset (286 shared, 13,708 in patient bundles) load in a few seconds. A transaction with one invalid entry returned 422 and stored none of its entries, so transactions are atomic. |
| Search by `patient` for the nine clinical types | Works and is complete. No result was truncated; the largest single result was 2,860 observations. |
| `_count` | Truncates the result but returns no `next` link, so there is no server-side paging. Without `_count`, every match comes back. |
| `_lastUpdated` | Not usable. Comparisons behave as if timestamps were cut to whole seconds, and a `gt` with a `+00:00` offset matched everything. It cannot express "strictly after" at the precision a source record carries. |
| `_history` and version reads | 404, not supported. |
| Restart, then reload | The store comes back empty; after reloading, the canonical hashes of all 13,708 resources read back were unchanged. |

## Decision
- **Load with one PUT transaction per patient.** The shared bundle (practitioners, organizations, locations) goes first, then the patients. A failed transaction leaves nothing half loaded, so the loader retries transport and server errors a few times, refuses anything the server rejects, and can always be run again.
- **Rewrite Synthea's bundles on every load:** each `POST` with `urn:uuid` becomes `PUT Type/<synthea-uuid>`, and each reference becomes `Type/<id>`. Conditional references to practitioners, organizations and locations resolve through an identifier index built from the shared bundle. The server's ids match Synthea's and stay stable across reloads.
- **Seed on every `up`.** A one-shot `seed` service runs after the FHIR server is healthy, in the API image, with a read-only root filesystem and the dataset mounted read-only. The API waits for it to finish, so a healthy API means a seeded server. If only the FHIR container restarts, its data is gone; run `docker compose run --rm seed` to load it again.
- **The FHIR adapter declares what the server lacks:**
  - `supports_versions` is false. Reading an exact version that is not the current one raises `OperationNotSupportedError`; it never returns a different version.
  - `supports_since` is false, a new capability. `fetch_changes` then ignores `since` and returns every record of the requested kinds for the patient. It never filters on `meta.lastUpdated`, which changes on every reload. Ingest already skips content it has seen, by hash ([0003](0003-provenance-snapshots-and-heads.md)).
  - Paging is the adapter's: it reads the whole result, orders it by resource type and id, and hands out opaque offset cursors. That is acceptable for a panel of tens or hundreds of patients and one patient's records; a server with real paging would replace it.
  - Write-back and notifications are not supported and raise `OperationNotSupportedError`.
- **The contract suite respects the capability.** The "strictly later" test runs only for adapters that declare `supports_since`.

## Consequences
- A clean start loads the data in well under a minute, and the load is repeatable. The time to a seeded stack and the loaded server's memory are in [the architecture page](../architecture.md).
- Every sync of a FHIR source re-reads each patient's records in full. At this scale that is cheap, and it removes a class of silent data loss: a record changed within the same second as the last sync cannot be skipped.
- The adapter reads a patient's observations in one response (2,860 observations for the largest synthetic patient, a few megabytes of JSON). The API's memory limit has to allow that.
- Replacing fhir-candle with HAPI changes Compose and the capability flags, not the adapter's contract.
