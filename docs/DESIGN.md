# graph_engineering 2: design

A local controller that takes a ticket from a person, has a local model implement it in a sandbox,
checks the result itself, and opens a pull request that a person merges.

It follows Anthropic's guidance in *Building effective agents*: start with the simplest system that
works, use a fixed **workflow** where the steps are predictable and an **agent** where they are not,
invest in the agent's tools, and make every step visible.

## Shape

```
 request ─► intake agent ─► ticket ─► accept ─► coding agent ─► gates ─► review agent ─► PR ─► human merge
            (read-only)     (spec)   (person   (sandboxed,       (harness  (read-only,     (GitHub,
                                      or policy) read/edit/run)   decides)  fresh context)  protections)
                                                     ▲                          │
                                                     └──── feedback (≤ N rounds) ┘
```

The outer line is a **workflow**: a small, fixed state machine in plain code. Only two steps are
**agents**, because only they need to decide what to do next: understanding a request in a codebase
(intake) and changing the codebase (coding). The review is an **evaluator** in the
evaluator-optimizer pattern: its findings go back to the coding agent.

| Anthropic idea | Here |
|---|---|
| Augmented LLM: model + tools + memory | `agent.py`: one loop over an OpenAI-compatible endpoint with tools; the ticket store is the memory |
| Agents for open-ended steps | intake and coding are tool-using loops that choose their own steps |
| Workflows for predictable steps | `workflow.py`: accept → develop → gate → review → publish → merge, no model decides a transition |
| Ground truth from the environment | the agent runs the tests; the harness reruns them and decides |
| Evaluator-optimizer | review agent in a fresh context; findings and failed gates feed the next round |
| Routing | `models.json` assigns roles to models: small model for triage, large for coding |
| Agent-computer interface | `tools.py`: few tools, exact-match edits with helpful errors, line-numbered reads, clipped output |
| Context engineering | notes, tool-result clearing, compaction, explore sub-agent, opening brief (below) |
| Transparency | every model turn and tool call is a transcript line; the control centre shows them live |
| Guardrails and stopping conditions | step and token budgets, repeat detection, sandbox, human merge |
| Measure | `evals/`: fixed tasks, repeated runs, pass rate |

## Trust boundaries

Two zones, never mixed:

- **Sandboxed** (untrusted): everything the model does and every command from the target repository
  (setup, tests, build). It runs under the OS sandbox: writes only in the run directory, no network
  except localhost, no access to `~/.ssh`, `gh` or cloud credentials, a minimal environment.
  macOS uses `sandbox-exec`; Linux uses `bwrap`. Without either, agent execution is refused unless the
  repository is registered as trusted.
- **Controller** (privileged): Git pushes, GitHub API calls, merges. Only harness code runs here, on
  facts the harness measured. Model text never becomes a command here; it only becomes PR prose.

Repository content is data. The intake and coding prompts say so, and no instruction found in a
repository can reach the privileged zone, because nothing the model writes is executed there.

## Gates (the harness decides)

After the coding agent stops, on the agent's workspace:

1. Some test file and some non-test file changed. No test file was deleted.
2. No protected path changed (`.github/`, lockfiles unless the ticket allows, configured paths).
3. **Red/green**: each changed test file, copied onto the untouched base revision, fails; on the result it passes.
4. The full test command and the build command pass.

Then the review agent reads the ticket, the diff and the gate results with read-only tools, and
returns `approve` or `changes` with findings. Any failed gate or `changes` starts another round
(default up to 3): a fresh coding-agent session in the same workspace, told what failed.

## Merge

Publication is GitHub-native: an issue (when the repository requires one), a branch following the
repository's convention, a PR whose body is assembled from measured facts, and a commit status with the
harness's result. Merging requires, all at the same head commit: the person's approval in the control
centre or CLI, passing required checks, satisfied reviews, a mergeable state, and known branch
protection. The controller never uses admin rights and never merges without approval. Approval is not
an MCP capability.

## States

```
new ─► triaging ─► proposed ─► accepted ─► developing ─► ready ─► publishing ─► awaiting_merge ─► merging ─► done
          │           │                       │   ▲                                  │
          ▼           ▼                       ▼   └── changes requested              ▼
      needs_info   rejected                 failed (budget spent / error)       (rejected: back to accepted)
```

Each step is idempotent and leased, so a crash restarts the step, not the ticket.

## Layout

```
src/graph_engineering/
  llm.py         OpenAI-compatible chat with tools; model registry and role routing
  sandbox.py     OS sandbox for commands; workspace preparation
  tools.py       the agent-computer interface: read-only and read-write tool sets
  agent.py       the agent loop: budgets, context engineering, repeat guard, transcripts
  prompts.py     system prompts, finish tools and the opening project brief
  gates.py       the harness's checks
  repos.py       repository registration and detection of setup/test/build commands
  store.py       SQLite: tickets, append-only events, agent runs, declared transitions
  workflow.py    the state machine: intake, development rounds, publication, merge, a person's actions
  publish.py     GitHub (issue, PR, status, merge gate, merge) and local publication
  scheduler.py   leased worker threads with heartbeats and backoff
  web.py, web/   control centre: JSON API and one page
  mcp_server.py  MCP tools for other agents (no approvals)
  evaluation.py  eval runner
  cli.py         graph-engineer command
evals/tasks/     eval tasks from real tickets
```

## Context engineering

Following Anthropic's *Effective context engineering for AI agents*, each agent works from the smallest
high-signal context that lets it act:

- **Right-altitude prompts**: short principles and a workflow, no lists of special cases.
- **Hybrid retrieval**: the opening message carries the project's own agent guide (AGENTS.md, CLAUDE.md,
  CONTRIBUTING.md or README) and a compact layout; everything else the agent finds just in time with search and read.
- **Token-efficient tools**: clipped output, paged reads, errors that say how to recover.
- **Tool-result clearing**: past a 40K-token working budget, old tool output is elided, the latest results kept.
- **Notes**: `update_notes` keeps the plan and findings in a file outside the context; every session on the
  ticket, including later rounds, opens with them.
- **Compaction**: if clearing is not enough, the model writes its notes and the conversation restarts from task and notes.
- **Sub-agents**: `explore(question)` answers a broad question in a fresh read-only context; only the answer
  (about 300 words) enters the coder's context.

Python standard library only; `gh`, Git and the target repository's toolchain are external.
The MCP SDK is an optional extra.
