# 0003. Immutable hashed snapshots, a head per resource, a rebuildable timeline

Status: Accepted

## Context
Every summary line cites the source record it came from. A citation must keep resolving to the exact content cited after any of these:
- the source edits the record
- the record is re-imported
- the FHIR server is wiped and reloaded, which reissues version id 1 for content that may differ
- content reverts to an earlier state

Healthie has no version ids at all.

## Decision
- **`source_record`** stores one immutable snapshot per distinct content of a resource. Its columns:
  - (system, resource type, id, version id), source update time
  - the payload encrypted exactly as received
  - `content_sha256`
  - Uniqueness is (system, type, id, content hash). The version id is recorded but not trusted as unique.
- **The content hash** is SHA-256 over a canonical form: sorted-key JSON, no insignificant whitespace, UTF-8, number tokens kept as their source text.
  - Server-assigned metadata is removed before hashing: FHIR `meta.versionId`, `meta.lastUpdated` and `meta.source`, and Healthie's `updated_at` (stored separately as `source_updated_at`).
  - Duplicate JSON keys are rejected because they are ambiguous.
- **`source_resource_head`** maps (system, type, id) to the current snapshot and `last_seen_at`.
  - Every import upserts it with the row locked, in the same transaction as the snapshot insert. The last import wins.
  - A composite foreign key lets a head point only at a snapshot of the same resource.
- **`timeline_event`** is a projection of current snapshots.
  - When the head moves, the previous snapshot's rows get `superseded_at`, and the new head's rows are upserted with it cleared.
  - Event ids derive from (snapshot, source path), so a re-projected row keeps its id and its encryption binding.
- **The app role may only insert into and read `source_record`.** It may update heads and timeline rows, which are derived.

## Consequences
- Reloading identical content into a reset server creates zero snapshots. A → B → A leaves two snapshots and A current again. Both are tested.
- Citations never dangle. Staleness ("the source changed since this draft") is a comparison of the cited snapshot against the head.
- Snapshots accumulate forever. Patient deletion is an owner-role operation that destroys the patient's key ([0008](0008-field-level-encryption.md)).
- Canonicalization must stay stable. Changing it changes every hash, so it would need a migration that rehashes from the stored payloads.
