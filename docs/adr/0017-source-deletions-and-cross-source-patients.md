# 0017. Tombstone records a complete read no longer sees; never merge patients across sources on our own

Status: Accepted. Settles the two decisions [0015](0015-timeline-ingest-and-normalization.md) left open ("detecting records deleted at the source" and matching "with the second source").

## Context
Ingest only upserts, and the adapters cannot report deletions, so a resource deleted at the source kept its head and its timeline rows ([0015](0015-timeline-ingest-and-normalization.md)). A second source now exists in design (Healthie, [0018](0018-healthie-adapter.md)), so two source patients may describe one person.

## Decision
**Deleted at the source: a complete read tombstones.**
- When a patient's records were read to the end (a failure raises before the tombstone step and rolls the patient back), the records ingest holds for that patient and no longer sees are tombstoned in the same transaction.
- **Where it lives.** A nullable `deleted_at` on `source_resource_head` (migration 0004). Snapshots stay immutable and nothing is deleted. The tombstone's timeline rows are superseded, so they leave the current timeline and stay in history. `source_record.state` is unused and left alone: it sits on an immutable row.
- **Revival.** Seeing the record again clears `deleted_at` and brings its rows back, with no new snapshot if the content is unchanged.
- **Guard: only types that came back non-empty.** A type for which the complete listing returned no record at all is never tombstoned. An empty answer is more likely an outage, a throttled search or a wiped source (fhir-candle restarts empty) than a patient whose every record of one type was deleted, and hiding a chart's history on that guess is the costlier mistake. The cost is the opposite error: if the last record of a type is deleted, it stays current until another record of that type appears.
- **A head is tombstoned only if it still points at the snapshot that was read.** Patients are ingested concurrently, so a record that moves between two patients in one run may be re-pointed by the other transaction; the update then matches nothing and leaves it alone.
- **Only the records of the patient being ingested** are considered, found through the patient on their current snapshot, and the patient's own record is never tombstoned.
- **Not covered.** A patient who disappears from the patient list is not tombstoned: that is a merge, a transfer or an archive, and a person decides which. Webhook-reported deletions are not handled either; a re-fetch that finds nothing is reported by the adapter, and acting on it waits for the Healthie normalizers.
- The run summary and its log line report `tombstoned` and `revived` counts. They are not stored on `import_run`; a run that tombstones far more than usual is visible in the log and in the `deleted_at` timestamps, and no ratio limit is built beyond the empty-type guard. The downgrade of 0004 refuses while tombstoned heads exist.

**Patients across sources: no automatic merge.**
- A source patient is found by its link in that source and nothing else. The same name and birth date from another source, or even the same external id, create a second patient.
- A wrong merge puts one person's record on another's chart, and cannot be undone without rebuilding the timeline. Two charts for one person is a nuisance a person can resolve.
- A merge, when it exists, is explicit, approved by a person, audited and reversible. Candidate suggestions would use the blind indexes, so no name or birth date is read in plaintext to find them. Neither is built; a test pins the no-merge behavior with the two seeded source systems.

## Consequences
- A re-run that finds a record missing now writes: one head update and the row supersession per missing record. A run that finds nothing missing writes what it wrote before.
- A source that returns a partial listing without an error would tombstone what it left out. The FHIR adapter refuses a short read, and a new adapter's contract must too; this is the reason the guard exists and the reason tombstones are reversible.
- Two charts for one person is the expected state once Healthie is ingested, until a merge exists.
