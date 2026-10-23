# graph_engineering 2: design

A local controller that takes a request from a person, has a local model implement it in a sandbox,
checks the result itself, and opens a pull request that a person merges.

The design follows Anthropic's published guidance on agents (sources at the end). In one sentence:
**a fixed workflow outside, a tool-using agent inside, ground truth from tests the harness runs itself,
the smallest useful context, and a person at the merge.**

## Where it stands (2026-10-22)

On the latest full measurement (9 trials, commit c86bc30, Qwen 27B on one GPU):

| | Result |
|---|---|
| Trials accepted by gates and review | 8 of 9 (89%) |
| Tasks solved in at least one trial (pass@k) | 3 of 3 |
| Tasks solved in every trial (pass^k) | 2 of 3 (67%) |
| Maintainers' hidden tests on accepted results | 2 of 3 |
| Time per ticket | 9 min to 2 h 12 min (median 52 min) |
| Prompt tokens per ticket | 0.6M to 6.8M (median 4.0M) |

Eight changes made since that run target the failures and the time it measured; they are not yet measured
together. The old 25-node pipeline never completed GE-FDD2275A; this design completes it in each trial.

## Principles, and where each lives

| Principle (source) | Here | Status |
|---|---|---|
| Start simple. Workflows for predictable steps, agents only where the steps can't be known in advance [1] | `workflow.py` is a fixed state machine; intake, plan, coding, review and explore are agent loops | done |
| Ground truth from the environment, plus stopping conditions [1] | the agent runs tests itself; step budget, stall and repeat detection, cancellation | done |
| The agent doing the work is not the one grading it; gate "done" deterministically [4][B] | the gates in `gates.py` decide; the agent's claims never do | done |
| Require evidence, not assertions [4] | `finish` must cite commands run after the last edit, checked against what ran; if missing, the harness runs the test command itself | done |
| Evaluator-optimizer; reviewer in a fresh context flags only correctness and requirement gaps; AI review is over-defensive [1][4][B][27] | read-only reviewer; only blocking findings send a change back, notes go to the PR | done |
| Same-model reviewers share blind spots; decorrelate [26] | reviewer prefers a model other than the coder's when one fits; with one strong model it is the same 27B | gap (hardware) |
| Poka-yoke the agent-computer interface [1][3][6] | six tools; `edit_file` needs a unique exact match; errors say how to recover | done |
| Tool output discipline [6][17] | reads paged at 400 lines / 24K characters, command output clipped to 8K | done |
| Context is a finite budget; smallest set of high-signal tokens [7][4] | 80K-token working budget (half the window); 40K caused re-read thrash in evals | done |
| Keep the prompt prefix stable for caching: append, don't edit [21][C] | history is append-only between rare, batched clearings | done |
| Right-altitude prompts; must-always rules belong in code [7][4] | rules the model ignored became code: forced finish on the last step, forced action after a cut-off | done |
| Just-in-time retrieval with a little preloaded [7][8] | opening brief (agent guide + layout); agentic search | done |
| Clearing, compaction, notes, sub-agents [7][C] | superseded reads and old outputs cleared in batches; notes file; compaction; `explore` | done |
| Fresh context per retry [4][13] | each round is a new session in the same workspace, opened with notes and feedback | done |
| Durable event log, restartable harness, disposable sandboxes [22] | SQLite event log; leased idempotent steps; one workspace per ticket | done |
| Routing: small model for easy inputs [1] | role routing by reachability and context fit; `explore` prefers the 9B | done, unused: no agent has called `explore` |
| Oversight at plan level; agree on "done" before coding [19][23] | plan step for tickets with 3+ criteria: a test per criterion, checked by the reviewer; unresolved concerns become the final review's checklist | partial: no person approves the plan |
| Protect the gate from the agent [13][B] | deleted tests, protected paths and test-configuration changes fail the gates | done |
| Containment: filesystem and network isolation, credentials outside, no-follow reads [10][25][A] | sandbox-exec / bwrap, no network, credentials in the controller | partial: see Limitations |
| Escalate to a person instead of looping [20] | round budget, then `failed`; publish and merge errors stop after 3 | done |
| Harness components encode model assumptions; ablate them [19][24] | each mechanism is a parameter; evals measure it | process |
| Evals: real tasks, reference solutions, isolated trials, pass^k, read transcripts [14][16] | 4 tasks from real tickets, validated by the miner; hidden maintainer tests; a fresh home per trial | partial: too few tasks |
| Transparency [1] | every turn and tool call is a transcript line; the control centre streams them | done |

"done" means implemented and covered by tests, not proven on many tasks.

## Shape

Two profiles share the same gates and the same person at the merge:

```
quick     request ─► intake ─► accept ─► coding ─► gates ─► PR ─► person reviews and merges
thorough  request ─► intake ─► accept ─► acceptance tests ─► [plan ─► plan review] ─► coding ─► gates ─► review ─► PR ─► person merges
                                         (from the criteria   (3+ criteria only)        ▲                 │
                                          alone, must fail)                              └ feedback, ≤ N rounds
```

The quick profile is what an interactive tool such as OpenCode does with a person watching: one agent, then
the machine checks, then the person judges intent. The thorough profile adds the steps that let a change be
trusted unattended. Which one pays, and when, is measured, not assumed.

## The agent loop

`agent.py` runs one loop for every agent against an OpenAI-compatible endpoint (vLLM serving Qwen with
native tool calls). It stops on the finish tool or a stopping condition:

- a step budget (80 coding, 40 intake, 30 plan and review, 20 explore);
- four turns without a tool call;
- three identical calls in a row with nothing changed in between;
- cancellation.

Each measure below came from a failure in a live transcript:

| Failure seen | Measure |
|---|---|
| Near-greedy decoding repeated one call forever | Qwen's recommended sampling |
| Reasoning hit the output limit without acting | reasoning tail handed back as notes; **next turn runs without thinking and must call a tool** |
| Re-reads of cleared results were refused as repeats | clearing a result also forgets it in the repeat guard |
| Explored for many steps without editing | nudge to plan and change, repeated every 15 steps |
| Reviewers read until the budget ran out, ignoring warnings | warning 3 steps before the end; **the last step offers only the finish tool** |
| `finish` was refused for stale evidence, costing turns | the harness runs the test command itself and accepts if it passes |
| Uncapped reasoning used every output token and gave no answer | **a thinking budget per agent, enforced by vLLM** (`thinking_token_budget`): planning 6,144, coding, review and intake 4,096, explore and memory 2,048 |
| Broken edits were found steps later by a failing suite | **an edit that breaks syntax is rolled back** with the error (Python, JSON, JavaScript); SWE-agent measured 3 points for this |
| The 9B explorer's two whole-file reads overflowed its budget by themselves; it compacted, re-read them and compacted again every step | **one tool result takes at most a quarter of the working budget**, cut at a line with where to read on; **compacting on two steps in a row asks for the final answer**; the explorer gets a slim brief |
| `npm test 2>&1 \| tail -25` reported tail's exit code, so a failing suite read as passing | commands run under **`bash -o pipefail`** |
| Three hung test runs cost 900 s of a 2,095 s coding session on an 11 s suite | **the command limit follows the base suite's time** (4× + 30 s, at least 60 s), and a timeout says the run probably hangs |
| A failed `edit_file` cost a re-read before the retry | the error shows **the closest lines of the file, numbered** (search/replace beats diffs for Qwen: Diff-XYZ, Aider) |

## Context engineering

- **Opening brief.** It contains:
  - the repository memory (below);
  - AGENTS.md / CLAUDE.md / CONTRIBUTING.md / README (first found, ≤ 5,000 characters);
  - a compact layout;
  - a **repository map** (after Aider): the files most related to the ticket, each with its main symbols. Files
    are ranked by the ticket's words found in them and by the import graph, about 3,000 characters in all.
- **Repository memory.** A guide per repository (layout, commands, test conventions, pitfalls), updated by a
  librarian after each accepted round. It holds facts about the repository, never a ticket's solution. The 9B
  wrote solutions into it despite instructions, so it runs on the 27B.
- **Append-only history.** Nothing is rewritten until the 80K-token budget is passed. Then, in one batch:
  - superseded reads are cleared: a newer read of the same lines, or a read from before an edit of that file;
  - then the oldest outputs, keeping the latest ten, then three, down to 60% of the budget.
- **Notes.** `update_notes` writes a file outside the context. It opens every later session on the ticket,
  for the planner and the coder alike.
- **Compaction.** The last resort: the model writes its notes and the conversation restarts from them.
- **Sub-agents.** `explore(question)` answers in a fresh read-only context.
- **Full transcripts on disk.** The context is a slice of them, never the only record.

## Gates: the harness decides

The harness snapshots exactly what the agent staged. It checks a clean checkout of that commit, plus only
the dependency directories setup installed:

1. Some test file and some source file changed; no test file deleted.
2. No protected path, generated or local file, or test configuration changed (test runner configs,
   conftest, `test*` package scripts), unless a person allowed it on the ticket.
3. **Red/green.** Each changed test fails on the untouched base and passes on the result. Without a
   per-file runner, the base suite must pass alone and fail with the new tests.
4. The full test command and the build pass.

The reviewer then returns blocking findings or notes. Its checklist includes plan concerns left unresolved
before coding, and any edit the coder made to an acceptance test. The published commit has exactly the tree
that was gated and reviewed.

**Acceptance tests (thorough profile).** Before planning and coding, a separate session writes tests from the
acceptance criteria and the current code alone, at the outermost interface the criteria name:
- only test files survive the session;
- the harness keeps only tests that fail now;
- the coder must make them pass.

Editing one is allowed, since it may be wrong, but the reviewer judges the edit. This answers the most common
failure the research names: 29.6% of patches that pass their tests behave differently from the real fix
(PatchDiff). Agentless resolved 81 → 96 issues with reproduction tests; Otter raised patch precision from 61% to
92%.

## Trust boundaries

- **Sandboxed (untrusted).** Everything the model does, and every command from the target repository.
  The sandbox runs `sandbox-exec` or `bwrap` with:
  - a minimal environment and a neutral git identity;
  - no network except loopback (setup may install dependencies);
  - credential directories unreadable.
- **Controller (privileged).** Pushes, GitHub API calls, merges with the person's `gh` credentials. Model
  text enters only as PR prose, with closing keywords and @mentions neutralised.

The goal, that no instruction in a repository can reach the privileged zone, **is not yet met**; see
Limitations.

## Merge

- **Publication:** an issue when required, the repository's branch convention (checked against
  General_AI_Agent's `pr-policy.mjs`), a PR body built from measured facts, and a commit status.
- **Merge:** needs, at one head commit, the person's approval, passing required checks, satisfied reviews,
  a mergeable state and readable branch protection.
- **Never:** admin rights, or approval over MCP.

## States

```
new ──intake──► proposed ──accept──► accepted ──round──► ready ──publish──► awaiting_merge ──approve──► merging ──► done
 │                 │                  │    ▲               │                     │                        │
 ▼                 ▼                  │    └─ next round ──┘ (changes requested) ┘                        ▼
needs_info ──answer──► new         failed ──retry──► accepted                                   awaiting_merge (blocked)
                   rejected
```

Compare-and-set transitions against a declared table; an append-only event log; leased, idempotent steps.

## Where the time goes

Measured across the 9 trials (9.4 agent-hours):

| Phase | Share of time | File reads |
|---|---|---|
| Coding | 43% | 208 |
| Planning | 25% | 219 |
| Plan review | 23% | 223 |
| Final review | 9% | 105 |

Setup and gates take 1–2 minutes per ticket; the rest is model inference at 20–45 s per step. Planning
and plan review together use **48%** of the time, mostly re-reading code the next agent reads again.
Without the append-only change, the prefix-cache hit rate was 83%.

## Limitations

1. **Containment is not sound (deferred, prototype first).** An independent review found the following,
   so use the controller only on your own repositories until they are fixed:
   - the sandbox can write the workspace's git metadata, which the controller later uses unsandboxed;
   - the macOS profile denies writes only under `$HOME`;
   - controller-owned files sit where the sandbox can write;
   - loopback reaches the control centre;
   - some reads follow planted symlinks.

   A partial fix is parked in git stash `17a0331`.
2. **Tests can be weaker than the criteria.** Red/green proves the new tests detect the change, not that
   they cover every way the code is used. That is how a change passed every gate and failed the
   maintainers' test. The plan-concern checklist addresses the case seen; the general problem remains.
3. **One model reviews its own kind of work.** The reviewer is the same 27B as the coder; the 9B's 16K
   window cannot hold a real diff.
4. **Slow: 10 minutes to 2 hours per ticket**, serial on one GPU, nearly all of it inference.
5. **The agent cannot add dependencies.** It has no network, so a ticket that needs a new package cannot
   succeed (General_AI_Agent commit 8e9d7c1 is an example).
6. **No base-drift handling.** If the base branch moves during a ticket, nothing rebases; the local
   backend refuses, GitHub may report the PR as behind.
7. **No live end-to-end run yet.** Publication and merge are covered by tests, including the repository's
   own PR policy, but no real PR has been opened by this design.
8. **Evals are thin.** 4 tasks from one repository; Anthropic suggests 20–50. Every tuning step so far fits
   these tasks. Intake (request → spec) is not evaluated at all; tasks start from a written spec.
9. **No memory across tickets.** Each ticket rediscovers the repository: test conventions, layout, where
   things live.
10. **Windows.** No sandbox mechanism, so only `trusted` repositories run there.

## What is missing, in order of expected value

| # | Item | Why | Cost |
|---|---|---|---|
| 1 | **Measure both profiles on the current code**: quick, then thorough, 4 tasks × 3 trials each | Repository memory, notes-first plan review, thinking budgets, syntax checks, the repository map and acceptance tests are all unmeasured | 10–15 GPU-hours |
| 2 | **Mutation gate** on the changed lines: the new tests must kill a share of mutants of the diff | LLM test suites catch at most ~36% of mutated solutions (SWE-Mutation); Meta's ACH | 2–4 days per language |
| 3 | ~~Speculative decoding on the model server~~: **already on** (MTP, 3 draft tokens, 67% accepted, from the server's `/metrics`) | — | done |
| 4 | **More eval tasks** from more repositories (`eval-mine` on other projects or small public libraries) | Avoid tuning to 4 tasks | small, GPU time to run |
| 5 | **One live ticket to a real PR** on General_AI_Agent | Proves the publish/merge path | 1 ticket; needs the person's go-ahead |
| 6 | **Containment fixes** from the stash | Required before any repository you do not own | medium |
| 7 | **Reviewer that can probe**: a throwaway checkout where the reviewer may write and run a scratch test | Would have caught the `model: undefined` case | medium |
| 8 | **Dependency requests**: a tool that asks for a package; the harness installs it with network after a person or allow-list approves | Unblocks tickets that need a library | medium |
| 9 | **Rebase round** when the base moves: re-run gates on the new base, a coding round only on conflict | Keeps PRs mergeable | medium |
| 10 | **Two concurrent tickets** to use vLLM batching (KV cache holds about 2.6 working budgets) | Throughput, not latency | small |
| 11 | **Intake eval**: requests with known good specs | The first step is unmeasured | small |
| 12 | **Optional plan approval by a person** [23] | Oversight before 30–60 minutes of coding | small |

## Layout

```
src/graph_engineering/
  llm.py         OpenAI-compatible chat with tools; model registry; routing by reachability and context fit
  sandbox.py     OS sandbox for commands; workspace preparation
  tools.py       the agent-computer interface
  agent.py       the agent loop: stopping conditions, context engineering, evidence, transcripts
  prompts.py     system prompts, finish tools, opening brief, plan and review tasks
  gates.py       the harness's checks on a clean snapshot
  repos.py       repository registration, command detection, planning policy
  store.py       SQLite: tickets, events, runs, declared transitions
  workflow.py    the state machine and a person's actions
  publish.py     GitHub and local publication, merge gate
  scheduler.py   leased worker threads with heartbeats, backoff and escalation
  web.py, web/   control centre, localhost only
  mcp_server.py  MCP tools for other agents (no approvals)
  evaluation.py  eval runner: pass@k, pass^k, hidden tests
  mining.py      eval tasks mined from history, validated against the reference commit
  cli.py         graph-engineer command
evals/tasks/     eval tasks; evals/tasks/mined/ from the miner
```

Python standard library only; `gh`, Git and the target toolchain are external; the MCP SDK is optional.

## Sources

Anthropic engineering and blog (anthropic.com/engineering unless noted). Posts after June 2026 were read
through a research agent's fetches on 2026-10-21, not first-hand.

1. Building effective agents (2024-12-19)
3. Raising the bar on SWE-bench Verified (2025-01-06)
4. Claude Code best practices (code.claude.com/docs/en/best-practices)
5. How we built our multi-agent research system (2025-06-13)
6. Writing effective tools for agents — with agents (2025-09-11)
7. Effective context engineering for AI agents (2025-09-29)
8. Building agents with the Claude Agent SDK (2025-09-29, claude.com/blog)
9. Equipping agents for the real world with Agent Skills (2025-10-16)
10. Beyond permission prompts: Claude Code sandboxing (2025-10-20)
13. Effective harnesses for long-running agents (2025-11-26)
14. Demystifying evals for AI agents (2026-01-09)
16. Infrastructure noise in agentic coding evals (2026-02-05)
17. Building a C compiler with parallel Claudes (2026-02-05)
19. Harness design for long-running app development (2026-03-24)
20. Claude Code auto mode (2026-03-25)
21. Agent harness design: 3 patterns (2026-04-02, claude.com/blog)
22. Scaling Managed Agents (2026-04-08)
23. Trustworthy agents in practice (2026-04-09)
24. April 23 postmortem (2026-04-23)
25. How we contain Claude (2026-05-25)
26. Patterns and problems in multiagent systems (2026-08-13)
27. Redesigning source control (2026-10-13)

Claude Code documentation (code.claude.com/docs/en/...): A. sandboxing; B. best-practices;
C. how-claude-code-works, and the session-management post (claude.com/blog).
