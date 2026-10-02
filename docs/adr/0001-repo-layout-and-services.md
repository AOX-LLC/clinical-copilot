# 0001. One repo, five Compose services, loopback-only ports

Status: Accepted

## Context
The product needs a web UI, an API that owns all clinical logic and security, an application database, and an EHR to integrate with. It has to run with one command on a developer machine that also runs other work.

## Decision
- **One repository** with `api/` (FastAPI, Python 3.12, uv) and `web/` (Next.js, TypeScript).
- **Five Compose services** under project name `clinical-copilot`: `web` (4600), `api` (4601), `postgres` with pgvector (4602), `fhir` (4603), plus a one-shot `migrate`.
- **Every published port binds to 127.0.0.1.** Every long-running service has a healthcheck; every service has a memory limit, a process limit and `no-new-privileges`.
- **The browser talks only to `web`,** which rewrites `/api/*` to the API. Session cookies stay first-party and no CORS is opened.
- **Two database roles.** Migrations run as the owner. The API connects as `copilot_app` with only the grants each migration gives it.
- **No default secrets.** Compose refuses to start until `.env` provides them.

## Consequences
- Every clinical rule lives in Python, in one place, where tests and audits can see it. The web app stays a presentation layer.
- The role split makes write protection a database fact, not a code convention. The cost is a grant line in every migration that adds a table.
- Memory limits make an overloaded service fail visibly instead of starving the host.
