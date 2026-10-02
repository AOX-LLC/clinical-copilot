# Clinical Copilot

[![CI](https://github.com/AOX-LLC/clinical-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/AOX-LLC/clinical-copilot/actions/workflows/ci.yml)
[![Evals](https://github.com/AOX-LLC/clinical-copilot/actions/workflows/evals.yml/badge.svg)](https://github.com/AOX-LLC/clinical-copilot/actions/workflows/evals.yml)

Clinical Copilot is a physician-facing intelligence layer for a concierge medical practice. It shows a patient timeline, lab trends with deterministic out-of-range flags, and the medication, supplement and protocol history. It drafts pre-visit summaries that cite a source record on every line, and a summary reaches the chart only after the physician approves it. It is built on synthetic patients only.

Built by [AOX](https://automatedoperationsexperts.com).

## Synthetic data only

Every patient in this project is synthetic. The 28 patients are generated with Synthea and committed with the script that regenerates them (`data/synthea/`), and a small simulated lab feed will add the lab results. The project never uses, stores or accepts real patient data, and you should not load real data into it.

Security controls are mapped to the HIPAA Security Rule's technical safeguards in [docs/threat-model.md](docs/threat-model.md). This is a demonstration project and makes no compliance claim.

## Status

In development. Phase 1 provides the architecture, the normalized timeline schema, the EHR adapter contract, the Compose stack and CI. Phase 2a adds the pinned synthetic dataset, a service that loads it into the local FHIR server, a FHIR R4 adapter on the same contract suite, and field-level encryption. Normalized import, labs and summaries come in later phases. Mock mode, which replays recorded model responses and needs no API key, arrives with the first model call.

## Architecture

- The web app (Next.js) talks only to the API.
- The API (FastAPI) owns all clinical logic, authorization and model calls.
- Postgres stores the normalized timeline; every row keeps provenance back to the exact source record it came from. Audit logs join it in later phases.
- A local in-memory FHIR R4 server (fhir-candle) stands in for the EHR.
- EHR adapters for FHIR R4 and Healthie sit behind one interface, so the rest of the system does not depend on a particular EHR.

See [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md) and the decision records in [docs/adr/](docs/adr/).

## Run it

Prerequisite: Docker with Compose v2.

```sh
cp .env.example .env    # then set the two passwords
docker compose up -d --wait
```

| Service     | Port |
| ----------- | ---- |
| web         | 4600 |
| API         | 4601 |
| Postgres    | 4602 |
| FHIR server | 4603 |

All ports are bound to 127.0.0.1.

Stop the stack with:

```sh
docker compose down
```

## Develop

API:

```sh
cd api
uv sync
uv run pytest
uv run ruff check . && uv run mypy
```

The API tests need `TEST_DATABASE_ADMIN_URL` to point at a Postgres server, for example the one from the Compose stack. See `.env.example`.

Web (Node 22):

```sh
cd web
npm ci
npm run dev          # http://127.0.0.1:4600
npm run lint
npm run typecheck
npm test
```

## License

MIT. See [LICENSE](LICENSE).
