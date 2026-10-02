# 0002. fhir-candle as the local FHIR server, chosen by measurement

Status: Accepted

## Context
The FHIR R4 adapter needs a real FHIR server holding synthetic patients, on a development machine with about 4 GB free that it shares with other stacks.

The common open-source servers are JVM applications sized for production:
- HAPI FHIR's maintainers suggest 4 GB of RAM ([FAQ](https://hapifhir.io/hapi-fhir/docs/appendix/faq.html)).
- Blaze defaults to a 4 GB heap.

The plan was to cap HAPI and measure it, and to switch to a lighter server if its idle memory exceeded 1.2 GiB.

## Measurements (2026-10-02, empty store, on a heavily loaded host)
| Server | Configuration | Idle memory (cgroup anon) | Time to healthy |
| --- | --- | --- | --- |
| HAPI FHIR JPA 8.12 (Tomcat image, H2) | `-Xmx768m`, metaspace 256 MiB, 1280 MiB limit | 1214 MiB, at its limit | about 12 minutes |
| fhir-candle (R4 tenant only, headless) | 768 MiB limit | 53 MiB | 33 seconds |

HAPI failed the 1.2 GiB gate.

## Decision
- **Run [fhir-candle](https://github.com/FHIR/fhir-candle):** the FHIR community's small in-memory R4 server, pinned by digest, with one R4 tenant at `/fhir/r4`.
  - It runs headless, as uid 65532, with a read-only root filesystem and tmpfs for its key store and package cache.
  - It is capped at 200,000 resources and 768 MiB.
- **Client-assigned ids work** (`PUT Patient/<uuid>`, checked against the running server). The Synthea loader writes `PUT Type/<synthea-uuid>`, so server ids match Synthea's and stay stable across reloads.
- **HAPI stays the documented alternative** for a host with memory to spare. The adapter speaks plain FHIR REST, so switching back changes only Compose.

## Consequences
- **Data lives in memory.** It is gone after a restart, so the seed reloads it. The app's own snapshots in Postgres are unaffected.
- **No versioned reads.** fhir-candle does not support `_history`/vread yet.
  - Version ids also restart at 1 after a reload.
  - Neither matters to provenance: snapshot identity is the canonical content hash, and citations resolve to stored snapshots ([0003](0003-provenance-snapshots-and-heads.md)).
  - The FHIR adapter declares `supports_versions=False` against this server.
- **Partial transaction and search support.** fhir-candle is a test server, so its search and transaction support is narrower than HAPI's. Phase 2's loader uses individual PUTs, and the contract suite catches any search the adapter needs but the server lacks.
- **Memory with a loaded dataset is measured in Phase 2.**
- **The server is unauthenticated and loopback-only:** an accepted development risk (threat model, T13).
