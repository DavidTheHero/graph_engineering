# graph_engineering 2: design

A local controller that takes a request from a person, has a local model implement it in a sandbox,
checks the result itself, and opens a pull request that a person merges.

The design follows Anthropic's published guidance on agents (sources at the end). In one sentence:
**a fixed workflow outside, a tool-using agent inside, ground truth from tests the harness runs itself,
the smallest useful context, and a person at the merge.**

## Principles, and where each lives

| Principle (source) | Here | Status |
|---|---|---|
| Start simple. Workflows for predictable steps, agents only where the steps can't be known in advance [1] | `workflow.py` is a fixed state machine. Only intake, coding, review and explore are agent loops | done |
| Ground truth from the environment at every step, plus stopping conditions [1] | the agent runs tests itself; step budget, stall and repeat detection, cancellation | done |
| The agent doing the work is not the one grading it. Give it a pass/fail check, gate "done" deterministically [4][B] | the gates in `gates.py` decide, the agent's claims never do | done |
| Require evidence, not assertions [4] | gate results go into the PR; the finish tool does not yet ask for the commands run | partial |
| Evaluator-optimizer; reviewer in a fresh context, sees the diff and the criteria, flags only correctness and requirement gaps [1][4][B]; AI review is over-defensive [27] | review agent: read-only tools, new context; only blocking findings send a change back, notes go to the person in the PR | done |
| Same-model reviewers share blind spots; decorrelate [26] | review uses the coder's model unless `review` lists a second model | gap |
| Poka-yoke the agent-computer interface: few non-overlapping tools, exact unique-match edits, errors that say how to recover, formats the model writes easily [1][3][6] | six tools in `tools.py`; `edit_file` needs a unique exact match | done |
| Tool output discipline: paginate, truncate, filter [6][17] | reads paged at 400 lines / 24K characters, command output clipped to 8K | done |
| Context is a finite budget ("context rot"); smallest set of high-signal tokens [7][4] | 80K-token working budget (half the 150K window); redundant reads superseded. 40K caused re-read thrash in evals | done |
| Right-altitude prompts; add instructions only for observed failures; must-always rules belong in code, not prose [7][4] | short prompts; rules such as "don't delete tests" are also gates | done |
| Just-in-time retrieval with a little preloaded (hybrid) [7][8] | opening brief (agent guide + layout); agentic search with grep/read | done |
| Clear old tool results first, then compact; notes outside the context; sub-agents return 1–2K-token summaries [7][C] | tool-result clearing → compaction into notes; `update_notes`; `explore` returns ~300 words | done |
| Fresh context per retry instead of piling on corrections [4][13] | each round is a new session in the same workspace, opened with notes and a short feedback note | done |
| Long-running work: progress file, git history, one unit at a time, clean state at the end of a session [13] | notes file per ticket; one ticket per workspace; the harness commits only gated snapshots | done |
| Durable event log outside a stateless, restartable harness; disposable sandboxes [22] | SQLite event log; leased idempotent steps; one workspace per ticket | done |
| Routing: easy inputs to small models, hard ones to large models [1] | roles in `models.json`; the 9B is configured but not routed to yet | gap |
| Multi-agent fan-out is a poor fit for coding (shared context, few parallel subtasks) [5][26] | one coding agent per ticket; sub-agents only answer questions | done |
| Oversight at plan level; agree on "done" before coding (sprint contract) [19][23] | acceptance criteria are accepted by a person before development | partial: no plan approval step |
| Protect the gate from the agent; changes to tests or test configuration are detected mechanically [13][B] | deleted tests and protected paths fail; test-runner configuration is not yet guarded | gap |
| Containment at the environment layer: both filesystem and network isolation; credentials never in the sandbox; resolve symlinks; don't run repo config before trust [10][25][B] | sandbox-exec / bwrap with no network; `gh` credentials stay in the controller | partial: see Known gaps |
| Escalate to a person instead of looping [20] | round budget, then `failed` for a person; publish and merge errors stop after 3 | done |
| Harness components encode assumptions about the model; ablate them when the model changes [19][24] | every loop mechanism is a parameter; evals measure them | process |
| Evals: real tasks with reference solutions, isolated trials, grade outcomes, read transcripts, report pass^k for reliability [14][16] | `evals/`: real past tickets pinned to base commits, hidden maintainer tests, a fresh home per run | partial: 3 tasks, no pass^k |
| Transparency: show the agent's steps; no framework that hides prompts [1] | every turn and tool call is a transcript line; the control centre streams them | done |

"done" means implemented and covered by tests. It does not mean proven on many tasks; only the evals can show that.

## Shape

```
 request ─► intake agent ─► ticket ─► accept ─► coding agent ─► gates ─► review agent ─► PR ─► person merges
            (read-only)     (spec)   (person   (sandboxed,       (harness  (read-only,     (GitHub,
                                      or policy) read/edit/run)   decides)  fresh context)  protections)
                                                     ▲                          │
                                                     └── feedback, ≤ N rounds ──┘
```

Two steps are **agents** because only they must decide what to do next: understanding a request in a
codebase (intake) and changing the codebase (coding). The coding agent can ask an **explore** sub-agent
broad questions. The **review** is the evaluator in an evaluator-optimizer loop. Everything else is plain
code: which state comes next, what counts as passing, and when to stop, are never a model's decision.

## The agent loop

`agent.py` runs one loop for every agent. It sends the conversation and tool definitions to an
OpenAI-compatible endpoint (vLLM serving Qwen with native tool calls), runs each tool call, and appends
the result. It stops when the agent calls its finish tool, or on a stopping condition:

- a step budget (80 for coding, 40 intake, 30 review, 20 explore);
- four turns without a tool call (stalled);
- the same call repeated three times with nothing changed in between (looping);
- cancellation by a person.

Measures added after live runs with the local 27B:

- **Sampling.** Use Qwen's recommended sampling (temperature 0.6, top-p 0.95, top-k 20 with thinking).
  Near-greedy decoding made the model repeat one call indefinitely.
- **Reasoning cut off at the output limit is handed back as notes.** The model then acts instead of
  restarting its analysis, because the chat template drops earlier reasoning.
- **Repeat answers.** A repeated identical call is answered with "you already have this result" rather than
  run again.
- **Exploration nudge.** After 15 steps without changing a file, the agent is asked to state its plan and
  make the first change.

## Context engineering

Each agent works from the smallest high-signal context that lets it act [7]:

- **Opening brief (hybrid retrieval).** The first message carries the project's own agent guide (AGENTS.md,
  CLAUDE.md, CONTRIBUTING.md or README, first one found, up to 5,000 characters) and a compact layout. The
  agent finds everything else just in time, with search and read.
- **Token-efficient tools.** Output is clipped, reads are paged, and errors say how to recover.
- **Superseded reads.** A read made redundant by a newer read of the same lines, or by an edit of the file, is cleared
  at once, so one current copy of each file stays in context.
- **Tool-result clearing.** Past an 80K-token working budget (about half the 27B window), the oldest tool output is elided, keeping
  the latest ten results, then the latest three.
- **Notes.** `update_notes` keeps the plan and findings in a file outside the context. Every session on the
  ticket opens with them, including later rounds.
- **Compaction.** Only if clearing is not enough, the model writes its notes and the conversation restarts
  from the task and the notes. This is the last resort: the model is at its weakest when compacting [C].
- **Sub-agents.** `explore(question)` answers a broad question in a fresh read-only context. Only the answer,
  about 300 words, enters the coder's context.
- **Full transcripts on disk.** Every transcript stays on disk; what enters the context is a slice of it,
  never the only record [22].

## Gates: the harness decides

After each coding session, the harness snapshots exactly what the agent staged as an unreferenced commit.
It runs every check on a **clean checkout of that commit**, with only the dependency directories that setup
installed added:

1. Some test file and some non-test file changed. No test file was deleted.
2. No protected path changed (`.github/` by default, plus configured paths), and no generated or local file
   was added.
3. **Red/green.** Each changed test file, copied onto the untouched base revision, fails; on the result it
   passes. Without a per-file runner, the base suite must pass alone and fail once the new tests are added.
4. The full test command and the build command pass.

Then the review agent reads the ticket, the diff and the gate results with read-only tools, and returns
`approve`, or `changes` with blocking findings. A failed gate or a `changes` verdict starts another round
(three by default). The published commit has exactly the tree that was gated and reviewed.

## Trust boundaries

Two zones, which must never mix:

- **Sandboxed (untrusted).** Everything the model does, and every command from the target repository
  (setup, tests, build). It runs under the OS sandbox (`sandbox-exec` on macOS, `bwrap` on Linux) with a
  minimal environment, no network except loopback (setup may install dependencies), and credentials
  directories unreadable. Without a sandbox, only repositories registered as `trusted` run.
- **Controller (privileged).** Git pushes, GitHub API calls and merges, using the person's `gh`
  credentials. Only harness code runs here, on facts the harness measured. Model text enters this zone only
  as PR prose, with closing keywords and @mentions neutralised.

Repository content is data. The prompts say so. The design goal is that no instruction found in a
repository can reach the privileged zone. **The current implementation does not yet meet that goal**;
see Known gaps.

## Merge

Publication is GitHub-native:

- an issue, when the repository requires one;
- a branch following the repository's convention (checked against General_AI_Agent's own `pr-policy.mjs`);
- a PR body built from measured facts;
- a commit status carrying the harness's result.

Merging requires all of the following at the same head commit:

- the person's approval in the control centre or CLI;
- passing required checks;
- satisfied reviews;
- a mergeable state;
- readable branch protection.

The controller never uses admin rights, and never merges without approval. Approval is not an MCP
capability. Since AI review tends to be over-defensive [27], the person reviews intent and evidence: the PR
shows the criteria, the gate results and the review.

## States

```
new ──intake──► proposed ──accept──► accepted ──round──► ready ──publish──► awaiting_merge ──approve──► merging ──► done
 │                 │                  │    ▲               │                     │                        │
 ▼                 ▼                  │    └─ next round ──┘ (changes requested) ┘                        ▼
needs_info ──answer──► new         failed ──retry──► accepted                                   awaiting_merge (blocked)
                   rejected
```

Every state change is a compare-and-set against a declared transition table, with an event in an
append-only log. Steps are leased and idempotent, so a crash restarts the step, not the ticket. A worker that
loses its lease stops.

## Layout

```
src/graph_engineering/
  llm.py         OpenAI-compatible chat with tools; model registry and role routing
  sandbox.py     OS sandbox for commands; workspace preparation
  tools.py       the agent-computer interface: read-only and read-write tool sets
  agent.py       the agent loop: stopping conditions, context engineering, transcripts
  prompts.py     system prompts, finish tools and the opening project brief
  gates.py       the harness's checks on a clean snapshot
  repos.py       repository registration and detection of setup/test/build commands
  store.py       SQLite: tickets, append-only events, agent runs, declared transitions
  workflow.py    the state machine: intake, development rounds, publication, merge, a person's actions
  publish.py     GitHub (issue, PR, status, merge gate, merge) and local publication
  scheduler.py   leased worker threads with heartbeats, backoff and escalation
  web.py, web/   control centre: JSON API and one page, localhost only
  mcp_server.py  MCP tools for other agents: file and follow tickets, no approvals
  evaluation.py  eval runner
  cli.py         graph-engineer command
evals/tasks/     eval tasks from real tickets
```

Python standard library only. `gh`, Git and the target repository's toolchain are external. The MCP SDK
is an optional extra.

## Known gaps

**Containment (deferred by decision on 2026-10-21, prototype first).** An independent review found the
sandbox boundary not yet sound. Until these items are fixed, use the controller only on your own
repositories, and don't describe it as isolating untrusted code:

- the sandbox can write the workspace's git metadata, which the controller later uses outside it;
- the macOS profile denies writes only under `$HOME`;
- controller-owned files (notes, transcripts, gate reports, profiles) live where the sandbox can write;
- loopback is open, including the control centre's port;
- some reads follow symbolic links planted in the workspace.

Planned, following [10][25] and Claude Code's sandbox [A]: deny-by-default writes with only the box
directory writable, git metadata read-only inside the sandbox, controller files outside the box, a
per-process control-centre token and a closed port, and no-follow reads.

**Against the guidance:**

- No plan step before coding [19][23]. Candidate: a read-only planning turn whose plan the reviewer checks
  against the criteria before edits.
- Test configuration (package scripts, test-runner configs) is not guarded [13]. Candidate: flag such
  changes as a failed gate unless the ticket allows them.
- The finish tool does not require evidence (commands run and their results) [4].
- The reviewer is the same model as the coder unless a second model is configured [26].
- The 9B is not routed to [1]. Candidates: intake triage and explore questions, which are short and
  read-only.
- Evals: 3 tasks, one trial each. Anthropic suggests 20–50 tasks from real failures, several trials, pass^k
  for reliability, and fixed resources [14][16].

## Sources

Anthropic engineering and blog (anthropic.com/engineering unless noted). Publications after June 2026 were
read through a research agent's fetches on 2026-10-21, not first-hand.

1. Building effective agents (2024-12-19)
3. Raising the bar on SWE-bench Verified (2025-01-06)
4. Claude Code best practices (living docs, code.claude.com/docs/en/best-practices)
5. How we built our multi-agent research system (2025-06-13)
6. Writing effective tools for agents — with agents (2025-09-11)
7. Effective context engineering for AI agents (2025-09-29)
8. Building agents with the Claude Agent SDK (2025-09-29, claude.com/blog)
10. Beyond permission prompts: Claude Code sandboxing (2025-10-20)
13. Effective harnesses for long-running agents (2025-11-26)
14. Demystifying evals for AI agents (2026-01-09)
16. Infrastructure noise in agentic coding evals (2026-02-05)
17. Building a C compiler with parallel Claudes (2026-02-05)
19. Harness design for long-running app development (2026-03-24)
20. Claude Code auto mode (2026-03-25)
22. Scaling Managed Agents (2026-04-08)
23. Trustworthy agents in practice (2026-04-09)
24. April 23 postmortem (2026-04-23)
25. How we contain Claude (2026-05-25)
26. Patterns and problems in multiagent systems (2026-08-13)
27. Redesigning source control (2026-10-13)

Claude Code documentation (code.claude.com/docs/en/...):

- A. sandboxing
- B. best-practices (verification, writer/reviewer)
- C. how-claude-code-works (agent loop, compaction) and the session-management post (claude.com/blog)
