# 0010. HMAC-signed lab webhook with timestamp and event-id replay protection

Status: Accepted (built in Phase 3)

## Context
Lab results arrive through a simulated feed over HTTP. A forged or replayed result would put false data on a patient's timeline.

## Decision
- **Headers:** `Lab-Event-Id` (UUID), `Lab-Timestamp` (Unix seconds) and `Lab-Signature: kid=<key id>, v1=<hex>`.
- **Signature:** HMAC-SHA256 over `v1\n{METHOD}\n{path}\n{timestamp}\n{event id}\n{sha256(body)}`. Up to two secrets are active at once, so keys rotate without downtime.
- **The receiver checks, in order, on the raw body before parsing:**
  1. body at most 256 KB, and content type
  2. known `kid`
  3. constant-time signature compare
  4. timestamp within ±300 s
  5. insert of (source, event id) into `webhook_receipt` under a unique constraint, in the same transaction that processes the result
- **A duplicate event id returns 200 and is not reprocessed,** so honest retries are safe and replays do nothing.
- **The payload is a strict schema** that rejects unknown fields, and the endpoint is rate-limited per source.
- **Healthie webhooks** follow Healthie's documented signing (HMAC-SHA256 over method, path, query, content digest, type and length). They carry no timestamp or event id, but only resource ids. Each one triggers an idempotent re-fetch over the authenticated API, so a replay can only cause a harmless re-read.

## Consequences
- The simulator and the receiver share a secret per environment. Secrets live in `.env`, never in the repo.
- Clock skew beyond five minutes rejects valid events. Hosts must run NTP.
