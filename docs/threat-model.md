# Threat model

Clinical Copilot is a demonstration system that runs on synthetic patients only. This document names what it protects, who it protects against and how. Each mitigation maps to the HIPAA Security Rule's technical safeguards (45 CFR 164.312).

The mapping shows how the design lines up with those safeguards. **It is not a compliance claim.** Compliance depends on an organization's policies, risk analysis, agreements and operations, none of which a code repository can supply.

Status column: **Designed** means decided here and built in the phase shown; **Built** means in the code today. Phase 1 builds the timeline schema, its write protection and the adapter contract; most controls below are designed now and built later.

## System in scope

The services, data flows and timeline schema are described in [architecture.md](architecture.md).
- **In scope:**
  - the web app
  - the API
  - the application database
  - the EHR adapters (FHIR R4, Healthie)
  - the signed lab webhook
  - calls to a model provider
- **Out of scope:**
  - the EHR systems themselves
  - the model provider's infrastructure
  - the host operating system and container runtime
  - physical security

## Assets

| Asset | Why it matters |
| --- | --- |
| Patient records: source snapshots, timeline rows, patient identity | The data a breach would expose. Synthetic here, but the design treats it as real ePHI. |
| Encryption and blind-index keys | Whoever holds them can read every encrypted field. |
| Sessions and credentials | Identity is the basis of every access decision. |
| Audit logs (access, LLM context) | The record of who saw what. It must survive tampering to be worth anything. |
| Approved summaries and write-backs | What reaches the chart. A wrong or forged write-back is a patient-safety issue. |
| Webhook and adapter secrets | Whoever holds them can forge lab results or source events. |

## Actors

| Actor | Capability assumed |
| --- | --- |
| Physician, nurse, admin | Legitimate users. They can still make mistakes, or reach for data outside their role. |
| External attacker | Network access to the published app. No credentials at first. |
| Malicious insider | A valid account, used beyond its purpose. |
| Compromised or spoofed lab feed | Can send arbitrary HTTP requests to the webhook endpoint. |
| Model provider | Receives every prompt; trusted for its contract, not for unnecessary data. |
| **Content inside a record** | Note or result text can carry instructions aimed at the model (prompt injection). |

## Trust boundaries

1. Browser ↔ web
2. Web ↔ API
3. API ↔ Postgres
4. API ↔ EHR (FHIR server, Healthie)
5. Lab feed → API
6. API → model provider

## Threats and mitigations (STRIDE by boundary)

| # | Boundary | Threat (STRIDE) | Mitigation | Safeguard | Status |
| --- | --- | --- | --- | --- | --- |
| T1 | 1, 2 | Spoofing: stolen password | argon2id hashes; TOTP MFA for every role; login rate limit with lockout backoff | (d) authentication | Designed, Phase 5 |
| T2 | 1, 2 | Spoofing: session theft or fixation | Server-side sessions; `__Host-` cookie, Secure, HttpOnly, SameSite=Lax; new id at login; CSRF token on writes | (d) authentication | Designed, Phase 5 |
| T3 | 1, 2 | Elevation: a nurse approves a summary, an admin reads charts | Deny-by-default RBAC; every route declares its permission; admins see no clinical content | (a)(1) access control | Designed, Phase 5 |
| T4 | 2, 3 | Information disclosure: reading another practitioner's patient by changing an id (IDOR) | Care-team scoping checked in the same query that loads the record; Postgres row-level security as a second layer | (a)(1) access control | Designed, Phase 5 |
| T5 | 1 | Information disclosure: an unattended workstation | 15-minute idle timeout, 10-hour absolute session lifetime | (a)(2)(iii) automatic logoff | Designed, Phase 5 |
| T6 | 1, 2 | Denial of care: a physician needs an unassigned patient urgently | Break-glass access with a required reason, an immediate alert and an audit entry | (a)(2)(ii) emergency access | Designed, Phase 5 |
| T7 | 3 | Information disclosure: a database dump or backup leaks | Field-level AES-256-GCM on identifiers, free text and raw payloads, with per-patient keys wrapped by a key-encryption key held outside the database | (a)(2)(iv) encryption | **Built**, Phase 2a: the cipher, keys, blind indexes, sealed patient identity, and the ingest command that seals every source payload and the timeline's text and detail columns; a test scans every `_enc` column for each seeded patient's names, birth date and identifiers. Key custody and backups designed, Phase 5 |
| T8 | 3 | Tampering: ciphertext swapped between rows or columns | AES-GCM associated data binds table, column and row id | (c)(1) integrity | **Built**, Phase 2a. Tested for patient identity (swaps across rows, columns, tables and patients). Ingest seals payloads and timeline columns with the same binding, but no test yet moves an ingested payload or timeline value between rows or columns |
| T9 | 3 | Tampering: a stored source record altered or deleted to change what a citation shows | `source_record` is insert-only for the app role (no UPDATE, DELETE or TRUNCATE); each snapshot carries a SHA-256 of its canonical content; ingest refuses a record whose hash does not match its payload | (c)(1) integrity, (c)(2) authenticate ePHI | **Built**, Phase 1 |
| T10 | 3 | Tampering: a head pointed at another resource's snapshot | Composite foreign key: a head can only reference a snapshot of the same (system, type, id) | (c)(1) integrity | **Built**, Phase 1 |
| T11 | 3 | Repudiation: "I never looked at that record" | Access audit of every endpoint that returns patient data: who, which patient, which records, when | (b) audit controls | Designed, Phase 5 |
| T12 | 3 | Tampering: audit entries edited to hide access | Audit tables are append-only for the app role, with a trigger rejecting UPDATE and DELETE; each row is hash-chained to the previous one | (b) audit controls, (c)(1) integrity | Designed, Phase 5 (role split built Phase 1) |
| T13 | 4 | Spoofing: a forged EHR response | Verified TLS to remote EHRs; SMART Backend Services (OAuth2 client credentials with a signed JWT) for a real FHIR EHR. The local FHIR server is unauthenticated and loopback-only: an accepted, development-only risk | (d) authentication, (e)(1) transmission | Designed, Phase 2 |
| T14 | 4 | Spoofing: a forged Healthie webhook | HMAC-SHA256 signature verified as Healthie documents. Its payload carries ids only, so every event triggers an idempotent re-fetch over the authenticated API. A replayed or forged event can at most cause a harmless re-read | (d) authentication, (c)(1) integrity | Designed, Phase 2 |
| T15 | 5 | Spoofing or tampering: a forged lab result | HMAC-SHA256 over method, path, timestamp, event id and body digest; two active secrets for rotation; constant-time compare; checks run on the raw body before parsing; strict schema | (d) authentication, (c)(1) integrity | Designed, Phase 3 |
| T16 | 5 | Tampering: a replayed lab result | Timestamp within ±300 s; event id unique per source, recorded in the same transaction that processes the event; a duplicate returns 200 without reprocessing | (c)(1) integrity | Designed, Phase 3 |
| T17 | 5 | Denial of service: oversized or flooding webhook requests | 256 KB body limit; per-source rate limit | (a)(1) access control | Designed, Phase 3 |
| T18 | 6 | Information disclosure: more patient data than needed reaches the model provider | Minimization inside the one context builder: a per-purpose field allowlist; no names, identifiers, contact details or addresses; age instead of birth date; dates as offsets from the visit; free text scrubbed and length-capped; per-call patient pseudonym | (a)(1) access control (minimum necessary) | Designed, Phase 4 |
| T19 | 6 | Repudiation: no record of what a model call read | Before any model call, one transaction writes the call and every context item sent: timeline row, source snapshot and content hash. If that write fails, the call is not made | (b) audit controls | Designed, Phase 4 |
| T20 | 6 | Tampering: prompt injection in record text steers the model | The model has no tools and no database access; output must match a schema; every line must cite handles from that call's context map; nothing reaches the chart without physician approval | (c)(1) integrity | Designed, Phase 4 |
| T21 | 6 | Integrity: fabricated or wrong citations | Citation handles are per-call and server-mapped, and unknown handles reject the line. Citations point at immutable snapshots. Approval is blocked while a cited resource's head has moved on since the draft | (c)(1) integrity | Designed, Phase 4 |
| T22 | 6 | Integrity: what is written back differs from what was approved | Approval records approver, time and the SHA-256 of the approved revision; write-back sends exactly that revision; idempotency key per write | (c)(1) integrity, (c)(2) authenticate ePHI | Designed, Phase 4 |
| T23 | 3, 6 | Information disclosure: prompts or patient data in traces, logs and errors | Traces carry ids, model, tokens and cost, never prompt or response text. Adapter errors, ingest errors and the reprs of records, drafts and clinical times never include payload content; tests assert this. The database engine hides bound parameters; ingest drops the driver's error chain, which carries failing-row values, and reports only error type, SQLSTATE and constraint; Postgres logs errors tersely | (b) audit controls, (a)(1) access control | Partly **built**, Phase 1 (adapter contract) |
| T24 | 1–6 | Information disclosure: data in transit | Development: every port bound to 127.0.0.1. Deployed: TLS at the reverse proxy with HSTS; `sslmode=verify-full` from API to Postgres; verified TLS to all outbound services | (e)(1) transmission security, (e)(2)(ii) encryption | Loopback binding **built**, Phase 1; TLS designed |
| T25 | 1–6 | Secrets committed or baked into images | Compose refuses to start without secrets from `.env`, and ships no default passwords; gitleaks in pre-commit and CI | (a)(1) access control | **Built**, Phase 1 |
| T26 | 3 | Elevation: the API's database role abused | The API connects as `copilot_app`: no superuser, no CREATEROLE, no BYPASSRLS, and only the grants each migration gives | (a)(1) access control | **Built**, Phase 1 |

## Safeguard map

| Safeguard (45 CFR 164.312) | Mitigations |
| --- | --- |
| (a)(1) Access control | T3, T4, T17, T18, T25, T26 |
| (a)(2)(i) Unique user identification | Every user has their own account; no shared logins (Phase 5) |
| (a)(2)(ii) Emergency access procedure | T6 |
| (a)(2)(iii) Automatic logoff | T5 |
| (a)(2)(iv) Encryption and decryption | T7, T8 |
| (b) Audit controls | T11, T12, T19, T23 |
| (c)(1) Integrity | T8, T9, T10, T12, T14, T15, T16, T20, T21, T22 |
| (c)(2) Mechanism to authenticate ePHI | T9, T22 |
| (d) Person or entity authentication | T1, T2, T13, T14, T15 |
| (e)(1) Transmission security | T13, T24 |
| (e)(2)(i) Integrity controls | T15 body digest, T24 TLS |
| (e)(2)(ii) Encryption | T24 |

The final safeguard map grows from this table, with a verification for each line.

## Accepted risks (development only)

- **The local FHIR server is unauthenticated.** It is reachable only on 127.0.0.1 and holds synthetic data.
- **Sealing is on every ingest write path.** The ingest command seals source payloads, timeline text values and timeline detail with the real sealer, and builds its own keys, so no caller can hand it a stand-in. Tests that need a stand-in sealer use a labeled one that is not encryption, and a test fails if production code mentions it. The timeline's codes, numbers, units and times stay in plaintext by design (ADR 0008).
- **Keys come from the environment or a file.** In development `FIELD_KEK` and `BLIND_INDEX_KEY` sit in `.env` or a secret file, and Compose passes them to the `ingest` service as environment variables, so the exited container's configuration keeps them until the container is removed; a deployment would use a KMS or a mounted secret. Losing the key-encryption key makes every encrypted field unreadable.
- **Destroying a patient's key is not erasure of everything, and it is only as strong as the backups.** It makes sealed fields unreadable and deletes the blind-index rows, but plaintext timeline columns and the source link stay (ADR 0008). A backup holding the wrapped key or the index rows can bring them back, so backups have to be retired on the same schedule as the data.
- **Blind indexes over low-entropy values can be inverted by whoever holds `BLIND_INDEX_KEY`** and a copy of the index rows (a birth date has about 36,500 candidates per century). The key needs the same custody as the KEK.
- **The database owner is the container superuser.** Migrations run as that role. A deployment would use a dedicated owner without superuser rights.
