# 0006. Model plumbing from a shared library; clinical logic stays here

Status: Accepted

## Context
Generic LLM plumbing will come from a separate, versioned agent library, pinned by release tag: model calls, routing, structured outputs, tracing, record/replay, approvals, audit, evals. That library has no release yet. Phase 1 contains no LLM code, and nothing from it is imported or copied.

## Decision
| From the shared library | Stays in this project |
| --- | --- |
| Model client and cost-based routing | Context builder: RBAC check, minimization, context audit, prompt rendering ([0009](0009-llm-context-audit-and-minimization.md)) |
| Structured outputs with retries | Summary schema; citation handles and their validator |
| Tracing with tokens and cost | Span policy: ids, model, tokens, cost; never prompt or response text |
| Record/replay mock mode | Recorded responses from synthetic, minimized context |
| Approval queue | Clinical approval checks (citations, staleness, attestation) and EHR write-back |
| Append-only audit log | The `llm_context_item` table holding each call's record references |
| Eval runner | Datasets: summary faithfulness, citation accuracy |

- **LangGraph orchestrates state and edges only.** Its nodes are plain functions that call the library's model client, so routing, tracing, cost and replay apply to every call. No LangChain chat-model wrappers are used.
- **No persistent LangGraph checkpointer.** Graph state would contain patient data, and the summary graph is a single short run.
- **Graph nodes get no tools that query the database.** All record access goes through the context builder, so nothing the model reads escapes the audit.

## Consequences
- Upgrading the library is a deliberate pin change, reviewed like any dependency.
- The project keeps its own audit child table instead of extending the library's schema.
