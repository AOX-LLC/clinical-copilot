# 0018. The Healthie adapter, built from the published schema and never run against a live account

Status: Accepted. Builds the adapter [0005](0005-ehr-adapter-contract.md) designed.

**This adapter has not run against a live Healthie account.** There is no sandbox access: Healthie's sandbox comes with its marketplace partnership, which is not being pursued for now. Everything below was written from Healthie's public documentation and checked against fixtures that are hand-built from it. Passing the contract suite shows the adapter agrees with the reference as transcribed. It does not show that Healthie answers that way.

## Context
Healthie exposes a GraphQL API and webhooks. This phase builds the adapter, its fixtures and its place in the contract suite. Normalizers and a webhook route are not built (see Open items).

## Decision
**Endpoint, auth and version.** Confirmed against Healthie's own quick-start, authentication and versioning pages, which agree with what [0005](0005-ehr-adapter-contract.md) took from a secondary source:
- `https://api.gethealthie.com/graphql` (production) and `https://staging-api.gethealthie.com/graphql` (sandbox), configured, never defaulted.
- `Authorization: Basic <api key>` and `AuthorizationSource: API`, plus `Content-Type: application/json`.
- `Healthie-GraphQL-API-Version: 2025-11-30`. Without the header Healthie serves 2024-06-01.

**Why 2025-11-30, and not a newer version.** It is the version whose reference pages were fetched and read field by field for this adapter, and the excerpt below was built from them. The versioning guide lists 2026-01-01 and 2026-07-01, and their reference pages exist and differ from this version's, but they were not compared with it, so nothing says the adapter's fields are unchanged there. Moving the pin is one constant and a rebuild of the excerpt (`build_excerpt.py` fails if a field the adapter uses was renamed or removed), then a read of what changed.

**What it reads.** Patients (`users`, connection pagination, 50 at most per page), medications (`medications(patient_id)`) and care plans (`carePlans`, connection pagination). It also reads one record by id for each, and a document by id. Resource types are `User`, `Medication`, `CarePlan` and `Document`.
- **Supplements and protocols have no type of their own in Healthie's schema.** Supplements would arrive as medications and protocols as care plans. Fullscript's `treatmentPlans` exists but needs that integration, so it is not read.
- **No version ids, and no "changed after" filter on these queries.** `supports_versions` and `supports_since` are false, as for FHIR ([0013](0013-fhir-seeding-and-adapter-limits.md)). A record's version is its `updated_at`, which the canonical hash removes, and identity is the content hash. A timestamp is accepted as ISO 8601 with a zone or in the `YYYY-MM-DD HH:MM:SS +hhmm` shape; one without a zone is refused. Which shape Healthie actually sends is not verified.
- **Errors.** HTTP 429 and the `TOO_MANY_REQUESTS` code in the body are the rate-limited error with the retry hint; 5xx, timeouts and connection failures are retryable; anything else is permanent. Healthie's error messages are never repeated, only their codes.
- **Ids are checked** against a narrow pattern before they go into a request, and cursors are our own opaque wrapper around Healthie's.

**Query validation.** Every operation the adapter sends is validated by a test against `api/tests/recorded/healthie/schema/healthie-2025-11-30.graphql`, using graphql-core (a dev dependency).
- Healthie publishes no machine-readable schema: the reference is about 2,400 HTML pages per version, with no SDL or introspection download, and introspection needs an API key. The excerpt is built by `build_excerpt.py` from the Definition section of each reference page.
- It holds type definitions only (names, fields and types) with Healthie's descriptions removed, and only the fields and arguments the adapter uses. Its header says it is an excerpt of Healthie's public reference used to check this adapter's queries, and lists each definition's source page.
- Pages each operation was written from: `queries/users`, `queries/user`, `queries/medications`, `queries/medication`, `queries/careplans`, `queries/careplan`, `queries/documents`, `queries/document` and `mutations/createdocument`, with the object and input pages listed in the excerpt header, all under `https://docs.gethealthie.com/reference/2025-11-30/`. Pagination, rate limits (complexity 2000, depth 25) and authentication come from the guides under `/guides/api-concepts/`.

**Webhooks.** `parse_notification` verifies the signature as the [webhooks guide](https://docs.gethealthie.com/guides/webhooks/) documents it: `Signature: sig1=<hex>` is the hex HMAC-SHA256, under the webhook secret, of `post <path> <query> <digest> application/json <length>`, where the digest is the hex SHA-256 from `Content-Digest: SHA-256=<hex>`.
- The adapter is configured with the path and query it expects, since the signature covers them. It also requires the digest to match the body received and compares in constant time. A non-ASCII or malformed header is a signature error, not a crash.
- The guide's sample measures the length on a re-serialized body, in characters. The adapter uses the bytes received. These agree for ASCII bodies and are not verified for others.
- The guide documents no timestamp or event id, so there is no replay protection to build. A replayed event does what the first did: `refetch` reads the named record again.
- Events are mapped by prefix: `patient.*` to `User`, `medication.*` to `Medication`, `care_plan.*` to `CarePlan`. Others are ignored. The malformed-but-signed cases are permanent errors.

**Write-back.** The mutation is `createDocument`: a document in the patient's chart (`include_in_charting`) holding the approved text. `createNote` is a chat message needing a conversation, so it does not fit. It is behind `write_back_enabled` (off by default) and only fixtures exercise it.
- Healthie has no idempotency key, so the key goes in the document's `metadata`, and a write first looks through the patient's documents for it. The look-up and the create are not atomic across processes, and a patient with more than 1,000 documents makes the adapter refuse rather than guess.
- How `file_string` must be encoded (a data URI of base64 text is used) is not documented in the reference, and is not verified.

**Fixtures.** `api/tests/recorded/healthie/world.json` is hand-built and synthetic, with a README that says it is not a recording. A fixture server answers the adapter's queries by executing them against the excerpt, so a query invalid against the excerpt fails the test that sent it. The contract suite gained a `healthie-fixture` harness, and its notification tests now take their bodies and headers from the harness instead of assuming one source's shapes.

## Open items
- **No Healthie normalizers.** Medications, care plans and patients from Healthie are not turned into timeline rows, and ingest is configured for FHIR only. A Healthie record reaching it would raise "no normalizer".
- **No webhook route.** The adapter verifies and parses; nothing serves a webhook endpoint yet, and mounting one needs a signature-authenticated route in the auth design.
- **Live verification.** Timestamp shape, `file_string` encoding, signature length semantics, the shape of Healthie's errors and whether a care plan's `patient` is reachable the way the excerpt says, all wait for a Healthie account.

## Consequences
- A new source is done when it passes the suite, and this one does against fixtures. That is weaker than the FHIR adapter's live run, and the README says so.
- A wrong guess about Healthie shows up as a typed error on the first live read, not as a bad record: shapes are checked and unexpected ones are refused.
