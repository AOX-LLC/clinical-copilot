# 0002. HAPI FHIR, capped at 1280 MiB, with a lighter fallback

Status: Accepted

## Context
The FHIR R4 adapter needs a real FHIR server holding synthetic patients. The common open-source servers are JVM applications sized for production:
- HAPI FHIR's maintainers suggest 4 GB of RAM ([FAQ](https://hapifhir.io/hapi-fhir/docs/appendix/faq.html)).
- Blaze defaults to a 4 GB heap.

The development machine has about 4 GB free, shared with other stacks.

## Decision
- **Run HAPI FHIR JPA server** (the `-tomcat` image variant, which has `curl` for its healthcheck), R4, with embedded H2 on a named volume.
- **Cap the JVM:** `-Xmx768m -XX:MaxMetaspaceSize=256m`, a container limit of 1280 MiB, and a 180 s start period.
- **Accept client-assigned ids** (`client_id_strategy: ANY`). The Synthea loader then writes `PUT Type/<synthea-uuid>`, so server ids stay stable across reloads.
- **Measure idle memory** after the healthcheck passes and record it in [architecture.md](../architecture.md).
- **Fallback:** if HAPI cannot stay healthy under the cap, switch to [fhir-candle](https://www.nuget.org/packages/fhir-candle), a small in-memory R4 server. The adapter speaks plain FHIR REST, so nothing else changes.

## Consequences
- A few dozen synthetic patients fit easily, but large Synthea populations won't fit the cap. That is fine for a demo.
- fhir-candle loses data on restart and reissues version ids from 1. The provenance model ([0003](0003-provenance-snapshots-and-heads.md)) already tolerates both, because snapshot identity is the content hash, not the version id.
- The FHIR server is unauthenticated and loopback-only: an accepted development risk (see the threat model, T13).
