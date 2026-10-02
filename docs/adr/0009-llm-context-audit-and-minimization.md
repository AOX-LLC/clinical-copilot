# 0009. One context builder: minimize, audit every item, then call

Status: Accepted (built with the first model call)

## Context
Two requirements:
- **Every model call must leave a record of exactly which patient records it read.**
- **The model provider should receive no more patient data than the task needs.** Both fail if any code path can build a prompt on its own.

## Decision
- **One path into a prompt:** `ContextBuilder.build(actor, patient, purpose)`. In order, it:
  1. Checks the actor's permission for that patient and purpose.
  2. Selects current timeline rows.
  3. Minimizes them:
     - a per-purpose field allowlist
     - no name, identifiers, contact details or address
     - age in years instead of birth date
     - dates as day offsets from the visit
     - free text scrubbed (phone, email, SSN and MRN patterns, plus this patient's own identifiers) and length-capped
     - a per-call patient pseudonym
  4. Assigns per-call citation handles (`[c1]…[cN]`).
  5. Writes the audit rows in one transaction:
     - `llm_call`: actor, patient, purpose, model, template and policy versions, prompt hash, and the prompt encrypted
     - one `llm_context_item` per item sent: handle, timeline row, source snapshot, content hash
  6. Only then dispatches the call. If the audit write fails, no call is made.
- **After the call:** the response hash, tokens, cost and latency are appended.
- **"Read" means sent to the model.** Rows excluded by minimization are counted, not listed.
- **Audit tables are append-only for the app role** (a trigger rejects UPDATE and DELETE) and hash-chained.
- **The model never sees internal ids.** Output lines must cite handles from that call's map.

## Consequences
- "Which records did this summary read?" is one indexed query.
- Changing the minimization policy changes prompts. Recorded responses must then be re-recorded, which is why the policy carries a version.
