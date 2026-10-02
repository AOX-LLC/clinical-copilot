# 0005. One adapter interface and one contract suite for every EHR

Status: Accepted

## Context
The product integrates with FHIR R4 servers and with Healthie, which has a GraphQL API and webhooks. Sandbox access to Healthie is pending, so its adapter starts as a stub designed from the public documentation:
- [webhooks](https://docs.gethealthie.com/guides/webhooks/)
- [event reference](https://docs.gethealthie.com/guides/webhooks/event-reference/)
- [pagination](https://docs.gethealthie.com/guides/api-concepts/pagination/)
- [versioning](https://docs.gethealthie.com/guides/api-concepts/versioning/)
- [rate limits](https://docs.gethealthie.com/guides/api-concepts/rate-limits/)

## Decision
- **One `EhrAdapter` protocol** (`api/app/ehr/ports.py`) with these operations:
  - `capabilities`
  - `list_patients` (opaque cursors)
  - `fetch_changes` (strictly after a time)
  - `get_record` (latest or an exact version, to re-resolve citations)
  - `write_back` (idempotency key)
  - `parse_notification` (signature verified)
- **Adapters do transport and identity only.** Normalization into timeline rows lives in separate pure functions.
- **Errors are typed:** not found, not supported, rate limited (with a retry hint), retryable, permanent, signature invalid. Messages and reprs never contain payload content.
- **Every adapter must pass one parametrized contract suite** (`api/tests/contracts/`):
  - pagination terminates without duplicates
  - hashes match payloads
  - listed records re-resolve to the same content
  - change queries are strictly later and repeatable
  - no naive datetimes
  - errors are typed
  - write-back is idempotent or declared unsupported
  - notification signatures are verified and unknown events ignored
  - no payload text reaches logs, errors or reprs
- **Phase 1 runs the suite against an in-memory fake.** The fake also simulates new versions, throttling and a wiped-and-reloaded server.
- **What the Healthie adapter must handle,** from the docs:
  - Basic API-key auth with `AuthorizationSource: API`.
  - A pinned `Healthie-GraphQL-API-Version`.
  - Cursor pagination (`after`, `page_info.end_cursor`).
  - `TOO_MANY_REQUESTS` mapped to the rate-limited error.
  - No version ids: version = `updated_at`, identity = content hash.
  - Webhooks carry ids only, and every event triggers a re-fetch.
  - The write-back mutation and where supplements and protocols live are confirmed against the schema reference before the stub is written.

## Consequences
- A new source system is done when it passes the suite, not when it "seems to work".
- The fake lives in `app/` so ingestion tests and later demos can use it. It is never wired into production configuration.
