# 0007. API-owned sessions with MFA; deny-by-default RBAC with care-team scoping

Status: Accepted (built in Phase 5)

## Context
- **Roles:** physician, nurse and admin.
- **Safeguards to meet:** unique user identification, emergency access, automatic logoff and person authentication.
- **Constraint:** the stack runs on a memory-constrained machine.

## Decision
- **Authentication is owned by the API.** Passwords use argon2id, every role enrolls TOTP MFA, and login is rate-limited with lockout backoff.
- **Sessions are server-side in Postgres.** The cookie is `__Host-session` (Secure, HttpOnly, SameSite=Lax), the session id is rotated at login, and every state-changing request needs a CSRF token.
- **Timeouts:** 15 minutes idle, 10 hours absolute.
- **An external identity provider (Keycloak or OIDC) was rejected for now.** It is another JVM service for a three-role app. The session layer is behind an interface, so OIDC can replace it later.
- **Permissions:**

| Permission | Physician | Nurse | Admin |
| --- | --- | --- | --- |
| Read timeline, labs and medications (assigned patients) | yes | yes | no |
| Generate a summary draft | yes | no | no |
| View draft summaries | yes | yes | no |
| Edit or approve a summary; trigger write-back | yes | no | no |
| Acknowledge alerts | yes | yes | no |
| Manage users and roles; rotate webhook secrets | no | no | yes |
| Read audit logs | no | no | yes |
| Break-glass access to an unassigned patient | yes, with a reason; raises an alert | no | no |

- **Every route declares its permission;** a route without one is denied.
- **Patient scoping** goes through `care_team_assignment`, checked in the same query that loads the record.
- **Postgres row-level security** on patient-scoped tables, keyed on `SET LOCAL app.user_id`, is a second layer. The app role has no `BYPASSRLS`.

## Consequences
- Admins can run the system without reading charts (separation of duties).
- Every patient-data query carries the actor. Tests must cover the denied paths, not just the allowed ones.
