# Clinical Copilot: developer conventions

## Ground rules
- **Synthetic data only.** Never add real patient data, real identifiers, or data copied from a real system, in code, tests, fixtures or docs.
- **Describe safeguards, never claim compliance.** CI rejects compliance claims.
- **Secrets live in `.env`** (copied from `.env.example`) and are never committed. gitleaks runs in pre-commit and CI.
- **One concern per commit.** Refactors and behavior changes go in separate commits. `main` is protected; changes land through pull requests.

## Stack
- API: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async, asyncpg), Alembic, managed with uv.
- Web: Next.js App Router, TypeScript (strict), Vitest.
- Data: Postgres 17 with pgvector. A local HAPI FHIR R4 server plays the EHR.
- Docker Compose project `clinical-copilot`; GitHub Actions for CI.

## Ports (all bound to 127.0.0.1)
| Service | Port |
| --- | --- |
| web | 4600 |
| api | 4601 |
| postgres | 4602 |
| fhir | 4603 |

## Commands
```bash
cp .env.example .env              # then set the two passwords
docker compose up -d --wait       # whole stack; FHIR takes a few minutes on first start
docker compose down

cd api
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                     # database tests need TEST_DATABASE_ADMIN_URL (see .env.example)
DATABASE_URL=... uv run alembic revision -m "describe the change"

cd web
npm ci
npm run dev                       # http://127.0.0.1:4600
npm run lint && npm run typecheck && npm test
```

## Layout
- `api/app/timeline/`: schema models, canonical content hash, clinical time, snapshot ingestion.
- `api/app/ehr/`: the adapter interface (`ports.py`) and the in-memory fake.
- `api/migrations/`: Alembic revisions. They are hand-written, and each one grants the app role exactly what it needs.
- `api/tests/contracts/`: the contract suite every EHR adapter must pass.
- `web/`: the Next.js app.
- `docs/`: architecture, threat model, ADRs (`docs/adr/`).

## Conventions
- **Source snapshots (`source_record`) are immutable.** The app role can only insert and read them. What is current lives in `source_resource_head`; `timeline_event` is a rebuildable projection.
- **Columns ending in `_enc` hold ciphertext only.** Never write plaintext to them.
- **Clinical times keep the precision the source gave.** Date-only values stay dates. Display and ordering use `CLINIC_TIMEZONE`.
- **Payload content never goes into logs, exception messages or `repr`.**
- **New tables get an explicit grant to `copilot_app` in their migration,** with the narrowest privileges that work.
- **A new EHR adapter joins the contract suite** by adding a harness to `HARNESS_FACTORIES`.
- **Every architectural decision gets a short ADR** in `docs/adr/`.
