# ADR 007: keep the durable custom graph engine; make declared edges authoritative

Date: 2026-10-20. Status: accepted. Supersedes the "future host" note in ADR 001.

## Question

Should orchestration move to LangGraph (StateGraph, SQLite/Postgres checkpointers,
`interrupt()` for human-in-the-loop, `Send` for fan-out)?

## Evaluation against this system's requirements

| Requirement | Current engine | LangGraph |
|---|---|---|
| Durable checkpoint per node | SQLite WAL; state and event committed in one transaction | Checkpointer per super-step |
| Crash isolation | Each node runs in its own process under an OS lock; a dead worker's lease is fenced | In-process; isolation would need an external worker pool |
| Exactly-once-ish external effects | Effect ledger with intent, reconciliation and fail-closed handling of uncertain outcomes | Not provided; idempotency is the user's job (LangGraph docs, durable execution) |
| Human gates bound to a commit SHA | `approvals` table and head-bound gates | `interrupt()` pauses the run; binding is still custom |
| Cross-ticket scheduling and capacity | Priority aging, per-model capacity, repository serialization | Not in scope; would stay custom |
| Install footprint | Standard library only | langgraph and langchain-core plus a checkpointer |
| Existing evidence and tests | 170+ tests, recorded live runs, persisted user data | Would need a migration |

LangGraph's real advantages are that its edges are structural and its fan-out
primitives are mature. The audit found the first gap real in this engine:
`conditional_edges` was documentation, maintained separately from the handlers'
actual `return` targets, and the UI patched in one undeclared edge by hand.

## Decision

Keep the engine and close the gap:

* `CONDITIONAL_EDGES` in `workflow.py` is the single table of transitions.
  `Workflow.execute` raises `TransitionError` for any handler transition not declared
  there. The error is a bounded failure that blocks the run.
* The control-centre topology and the MCP `workflow_graph` tool are generated from
  the same `transitions()` function. What is drawn is what can execute.
* `tests/test_workflow.py::DeclaredGraphTests` parses every node handler's source
  (AST) and asserts that each literal transition is declared, that every destination
  is a node, and that an undeclared transition is rejected at runtime.

Parallelism remains at ticket granularity (separate processes) plus the opt-in quality
fan-out and fan-in (`quality.py`). Intra-ticket parallel branches are not added
speculatively.

## Revisit when

Deployment becomes multi-host (Postgres leases and an outbox would be needed anyway),
or intra-ticket fan-out across many agents becomes a measured requirement.
