# Architecture decision records

Each record states one decision, why it was made, and what it costs. Records are not edited after acceptance. A changed decision gets a new record that supersedes the old one.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-repo-layout-and-services.md) | One repo, five Compose services, loopback-only ports | Accepted |
| [0002](0002-fhir-server-and-memory-budget.md) | fhir-candle as the local FHIR server, chosen by measurement | Accepted; loader superseded in part by 0013 |
| [0003](0003-provenance-snapshots-and-heads.md) | Immutable hashed snapshots, a head per resource, a rebuildable timeline | Accepted |
| [0004](0004-clinical-time-and-timezones.md) | Keep the precision the source gave; one practice timezone | Accepted |
| [0005](0005-ehr-adapter-contract.md) | One adapter interface and one contract suite for every EHR | Accepted |
| [0006](0006-agent-library-seam.md) | Model plumbing from a shared library; clinical logic stays here | Accepted |
| [0007](0007-authentication-and-rbac.md) | API-owned sessions with MFA; deny-by-default RBAC with care-team scoping | Accepted |
| [0008](0008-field-level-encryption.md) | AES-GCM field encryption with per-patient envelope keys | Accepted; built per 0014 |
| [0009](0009-llm-context-audit-and-minimization.md) | One context builder: minimize, audit every item, then call | Accepted |
| [0010](0010-signed-lab-webhook.md) | HMAC-signed lab webhook with timestamp and event-id replay protection | Accepted |
| [0011](0011-summary-citations-through-edits.md) | Citations point at snapshots; edits create revisions; approval checks staleness | Accepted |
| [0012](0012-synthetic-dataset.md) | A pinned, trimmed Synthea dataset, committed | Accepted |
| [0013](0013-fhir-seeding-and-adapter-limits.md) | Seed fhir-candle with PUT transactions, and adapt to what it cannot do | Accepted |
| [0014](0014-field-encryption-implementation.md) | How field encryption is built: key layout, blind indexes and what is not yet wired | Accepted |
