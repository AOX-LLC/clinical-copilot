# Clinical Copilot: developer conventions

## Ground rules
- **Synthetic data only.** Never add real patient data, real identifiers, or data copied from a real system, in code, tests, fixtures or docs.
- **Describe safeguards, never claim compliance.** CI rejects compliance claims.
- **Secrets live in `.env`** (copied from `.env.example`) and are never committed. gitleaks runs in pre-commit and CI.
- **One concern per commit.** Refactors and behavior changes go in separate commits. `main` is protected; changes land through pull requests.

## Stack
- API: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async, asyncpg), Alembic, managed with uv.
- Web: Next.js App Router, TypeScript (strict), Vitest.
- Data: Postgres 17 with pgvector. A local in-memory FHIR R4 server (fhir-candle, base URL `http://127.0.0.1:4603/fhir/r4`) plays the EHR.
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
cp .env.example .env              # then set the two passwords and the two keys
docker compose up -d --build --wait   # whole stack; a plain `up` reuses an old API image after code changes
docker compose down
docker compose run --rm seed      # reload the dataset after the fhir service restarts
docker compose run --rm ingest    # read the FHIR server into the timeline again (idempotent)
python3 scripts/check-stack-counts.py   # compare a fresh stack with data/synthea/expected-counts.json
data/synthea/generate.sh --check  # regenerate the dataset and compare with the committed manifest

cd api
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                     # database tests need TEST_DATABASE_ADMIN_URL (see .env.example)
LIVE_FHIR_BASE_URL=http://127.0.0.1:4603/fhir/r4 uv run pytest -m live   # needs the stack up
DATABASE_URL=... uv run alembic revision -m "describe the change"

cd web
npm ci
npm run dev                       # http://127.0.0.1:4600
npm run lint && npm run typecheck && npm test
```

## Layout
- `api/app/timeline/`: schema models, canonical content hash, clinical time, snapshot ingestion.
- `api/app/ehr/`: the adapter interface (`ports.py`), the in-memory fake and the FHIR R4 adapter.
- `api/app/crypto/`: the field cipher, key handling and blind indexes. `FieldSealer` is the only production sealer.
- `api/app/timeline/normalize/`: the normalizers (source record to timeline rows) and the projector registry.
- `api/app/ingest/`: the ingest command (`python -m app.ingest`) the `ingest` service runs.
- `api/app/fhir_seed/`: Synthea bundle transforms and the loader the `seed` service runs.
- `data/synthea/`: the committed synthetic dataset, `generate.sh`, which regenerates it, and `expected-counts.json`.
- `scripts/`: repository checks that run outside the API's environment, such as the stack-smoke count check.
- `api/migrations/`: Alembic revisions. They are hand-written, and each one grants the app role exactly what it needs.
- `api/tests/contracts/`: the contract suite every EHR adapter must pass.
- `web/`: the Next.js app.
- `docs/`: architecture, threat model, ADRs (`docs/adr/`).

## Conventions
- **Source snapshots (`source_record`) are immutable.** The app role can only insert and read them. What is current lives in `source_resource_head`; `timeline_event` is a rebuildable projection.
- **Columns ending in `_enc` hold ciphertext only.** Never write plaintext to them, and never construct a stand-in sealer outside tests.
- **Clinical times keep the precision the source gave.** Date-only values stay dates. Display and ordering use `CLINIC_TIMEZONE`.
- **Payload content never goes into logs, exception messages or `repr`.**
- **New tables get an explicit grant to `copilot_app` in their migration,** with the narrowest privileges that work.
- **A new EHR adapter joins the contract suite** by adding a harness to `HARNESS_FACTORIES`.
- **Every architectural decision gets a short ADR** in `docs/adr/`.
- **Dataset and timeline counts live only in `data/synthea/expected-counts.json`.** The tests and the stack-smoke job both read it; change it in the same commit as the dataset or normalizer change that moves a count, never as a literal elsewhere.
