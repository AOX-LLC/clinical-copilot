# 0011. Citations point at snapshots; edits create revisions; approval checks staleness

Status: Accepted (built with the summary agent)

## Context
Every summary line cites the record it came from. Physicians will edit drafts before approving them. An edit, or a source update after drafting, must never leave a line claiming support it does not have.

## Decision
- **A `summary` has immutable revisions.** An edit creates a new revision; nothing is updated in place.
- **Lines and their citations:**
  - Each `summary_line` (ordinal, encrypted text, origin: generated, edited or physician-added) has `summary_line_citation` rows.
  - Each citation references a source snapshot, a timeline row and the content hash.
  - Because snapshots are immutable ([0003](0003-provenance-snapshots-and-heads.md)), citations always resolve.
- **Approval is blocked until every line either:**
  - cites at least one snapshot of the same patient, or
  - is marked physician-attested, attributed to the physician and carrying no source claim.
- **Edited lines keep their citations,** shown beside the line. The physician confirms or removes each one before approval.
- **A line goes stale when a cited resource's head no longer points at the cited snapshot.** It is flagged "source updated since draft" and must be re-acknowledged.
- **Approval binds the record.** It stores approver, time and the SHA-256 of the approved revision, and write-back sends exactly that revision under an idempotency key.

## Consequences
- The chart only ever receives text a physician approved, with every claim either sourced or attributed.
- The editor must show sources inline. That is the point of the product, not overhead.
