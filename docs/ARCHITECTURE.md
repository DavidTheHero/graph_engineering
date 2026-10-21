# Architecture

Graph Engineering is a single-host engineering controller. It is independent of the
repositories it works on: everything repository-specific is discovered by adapters or
supplied as configuration. Codex (or any MCP client, or a human) supervises it through
one capability layer. Qwen models do bounded work through isolated OpenCode sessions.
Deterministic code decides whether that work passed.

## Components

```mermaid
flowchart LR
  subgraph Supervisors
    Codex[Codex / MCP clients]
    Human[Operator: browser + CLI]
  end

  subgraph GE[Graph Engineering controller]
    MCP["MCP server<br/>mcp_server.py<br/>stdio or loopback HTTP + bearer token"]
    CAP["Capability layer<br/>capabilities.py<br/>profiles read/operate/admin, audit, validation"]
    API["Control centre API + SSE<br/>dashboard.py, control_api.py"]
    CLI["CLI<br/>graph-engineer"]
    SCH["Durable scheduler<br/>scheduler.py<br/>priority queues, leases, capacity"]
    WF["Graph engine<br/>workflow.py + node mixins<br/>declared, enforced transitions"]
    RT["Routing<br/>routing.py<br/>role → tier → healthy model"]
    POL["Verification & policy<br/>repository.py, publication.py<br/>frozen tests, diff checks, merge gates"]
    AD["Repository adapters<br/>adapters.py<br/>py/js/go/rust/jvm/.NET/ruby/make/generic"]
    DB[("SQLite WAL<br/>runs, events, effects,<br/>tickets, revisions, approvals")]
  end

  subgraph Workers[Isolated workers]
    OC["OpenCode 2.0.32 sessions<br/>per-call profile, tools denied,<br/>JSON-schema output"]
    WT["Git worktrees / pinned snapshots<br/>under the controller home"]
  end

  subgraph Inference[Local inference]
    Q27["Qwen 3.8 27B<br/>large tier, vLLM"]
    Q4["Qwen 3.5 4B (8K)<br/>small tier, Docker Model Runner"]
  end

  GH[(GitHub via gh<br/>controller only)]
  Repo[(Target repositories<br/>read-only source)]

  Codex --> MCP --> CAP
  Human --> API
  Human --> CLI
  API --> CAP
  CLI --> CAP
  CAP --> DB
  CAP --> SCH
  SCH --> WF
  WF --> RT --> OC
  OC --> Q27
  OC --> Q4
  WF --> POL
  WF --> AD --> Repo
  POL --> WT
  WT -. clone / worktree .- Repo
  POL --> GH
  WF --> DB
  API -. SSE of persisted events .-> Human
```

### Layers and their contracts

| Layer | Modules | Responsibility | Must never |
|---|---|---|---|
| Interfaces | `mcp_server.py`, `dashboard.py`, `cli.py` | Speak MCP, HTTP/SSE and argv | Contain business rules |
| Capability | `capabilities.py`, `control_api.py` | Permission profiles, idempotent commands, audit | Expose approvals to agents |
| Orchestration | `scheduler.py`, `workflow.py`, `investigation.py`, `accepted_development.py`, `question_answering.py` | Leases, queues, graph nodes, checkpoints, retries | Take a transition not in `CONDITIONAL_EDGES` |
| Routing | `routing.py`, `models.py` | Pick a model by role, tier, profile and health; run OpenCode | Silently substitute a small model for a complex role |
| Repository | `adapters.py`, `repository.py`, `control_repositories.py` | Ecosystem detection, worktrees, snapshots, bounded commands | Execute code without `trusted_local` |
| Verification and policy | `repository.py`, `publication.py`, `workflow.py` gates | Frozen tests, diff scope, head-bound approvals, merge gates | Accept a model's claim as proof |
| Persistence | `runtime.py`, `tickets.py`, `report_store.py` | SQLite WAL, events, effect ledger, revisions | Overwrite history |

### Execution model

1. A ticket is a row in `tickets` pointing at a run in `runs`. The run's `node` is
   the durable cursor and `state` holds the validated artifacts.
2. `serve_scheduler` claims the highest effective-priority eligible ticket. A ticket is
   eligible when its repository is not leased, its model has capacity and is not
   observed down, and routing can place its next step.
3. A separate `worker-step` process executes exactly one node under an OS lock and
   checkpoints. The scheduler re-queues the ticket, so concurrent tickets interleave
   at node boundaries.
4. A handler returns `(updates, next_node, status)`. The engine validates the output
   schema and checks that the transition is declared, then commits the state and an
   event in one transaction.
5. External side effects (issues, PRs, comments, merges) go through `Store.effect`:
   the intent is recorded first and reconciled on resume. An uncertain outcome is
   never blindly retried.
6. The browser animates only persisted events, delivered over SSE. MCP clients read
   the same events through `run_events` / `ticket_timeline`.

### Graph clusters (from `workflow.NODES`)

* **Intake and investigation:** `manual_intake → request_triage → request_duplicate_check →
  repository_search ⇄ problem_investigation → ticket_enhancement → reproduction_check →
  finding_validation (→ problem_investigation retry) → ticket_finalisation`.
  Human gates follow, either `development_admission` or `ticket_publication`.
* **Accepted development:** `development_setup → regression_authoring ⇄ regression_review →
  regression_validation → task_planning → model_selection → isolated_implementation →
  test_execution → independent_review (→ repair → isolated_implementation) →
  development_delivery / pull_request_creation`.
* **Repair from a reproduced failure:** `repository_discovery → evidence_collection →
  issue_detection ⇄ issue_validation → deduplication → ticket_creation → prioritisation →
  task_planning …`.
* **Delivery:** `pull_request_creation → ci_policy_verification → approval_gate (waits for
  a human) → merge → post_merge_verification → completion`.

`graph-engineer graph` and the MCP `workflow_graph` tool export the exact set of
transitions that the engine enforces.

## Autonomous investigation (2026-10-21)

Intake investigation establishes repository facts itself instead of asking the requester:

* the large model sees the committed source file listing and may name up to six `read_files`
  for the next search round (only paths in the filtered listing; requested files are read first,
  whole when short, and lines over 300 characters are truncated so one line cannot hide a file);
* the prompt forbids questions the repository can answer; product choices with a safe default
  are recorded as `Assumption:` limitations;
* before a ticket waits for a person, `ticket_finalisation` sends the model's own questions back
  for `SELF_RESOLUTION_ROUNDS` (1) additional search-and-investigate round per operator generation,
  with its own reference-correction retry, recorded as a `self_resolution` event;
* a grounded ticket (all references verified, at least one finding) proceeds to review with any
  remaining questions visible; only an ungrounded ticket waits in Needs Information.

Measured on a copy of GE-30D05C40 (Qwen 3.8 27B): the two repository questions it previously asked
were answered from `backend/llm.js:84` and `frontend/src/App.jsx`, and the ticket reached review with
four verified references and no questions. Review, development authorisation and merge approval
remain human gates unless a repository's autonomy policy says otherwise.

## Directory structure

```text
graph_engineering/
  pyproject.toml                 core has no runtime dependencies; [mcp] extra adds the SDK
  src/graph_engineering/
    cli.py                       graph-engineer: doctor, repo, ticket, run, ui, mcp, models, dispatch…
    mcp_server.py                MCP protocol adapter (tools, resources, stdio/HTTP)
    capabilities.py              protocol-neutral capability layer, profiles, audit
    control_api.py               idempotent operator command dispatch (shared by UI and MCP)
    workflow.py                  node registry, CONDITIONAL_EDGES, engine, repair/delivery nodes
    investigation.py             natural-language intake and evidence validation nodes
    accepted_development.py      regression-first development nodes
    question_answering.py        bounded small-model answers to ticket questions
    scheduler.py                 durable multi-process dispatch, capacity, crash recovery
    scheduling_policy.py         priority aging, backoff, board column mapping
    routing.py                   role → tier → model selection with health and policy
    models.py                    model registry, discovery, OpenCode V2 worker
    adapters.py                  ecosystem detection (no code execution)
    repository.py                discovery, bounded commands, worktrees, edit validation
    control_repositories.py      registration, pinned snapshots, workflow starts
    publication.py               local and GitHub backends, merge gates
    runtime.py                   SQLite WAL store, events, effect ledger, OS locks
    tickets.py, report_store.py  tickets, queues, natural-language reports, revisions
    centre_data.py, dashboard*.py, web/   control centre projection, SSE, UI
  tests/                         unit, integration, protocol and failure-injection tests
  scripts/                       evaluation, benchmarks, probes, evidence capture
  configs/                       models.example.json, repository and OpenCode examples
  docs/                          ADRs, audit, operations, MCP, evaluation results
  evidence/                      raw receipts from real runs
```
