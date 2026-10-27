# graph_engineering 2: design

A local controller that takes a request from a person, has a local model implement it in a sandbox,
checks the result itself, and opens a pull request that a person merges.

The design follows Anthropic's published guidance on agents (sources at the end). In one sentence:
**a fixed workflow outside, a tool-using agent inside, ground truth from tests the harness runs itself,
the smallest useful context, and a person at the merge.**

## Where it stands (2026-10-24)

Run 13, the current baseline: the quick profile on commit 179ec84, 8 tasks from two repositories (4 from
General_AI_Agent, 4 from this project's own history) × 2 trials, Qwen 27B on one GPU:

| | Result |
|---|---|
| Trials accepted by the gates | 15 of 16 (94%) |
| Tasks solved in every trial (pass^k) | 7 of 8 (88%) |
| Tasks solved in at least one trial (pass@k) | 8 of 8 |
| Maintainers' hidden tests on accepted results | 8 of 11 (73%) |
| Time per ticket | 6 min to 1 h 43 min (median 20 min) |
| Prompt-cache hit rate on the model server | 94% |

Six tasks passed both trials and every hidden test. The misses are the two hardest: #20 (tool search: one
trial passed the gates but not the maintainers' tests, the other never got its own tests green in three rounds),
and the repository map (both trials miss Python `async def`, which the maintainers test). On #20 the same
maintainers' test failed in every run from 10 to 17 (a tool result carrying a report must end the run at once); the
coders gated that on the request's type, so the task's interface section now states it, as it states the other
interfaces the hidden tests depend on. The finish coverage check (5d0d992) targets the same miss: the coder cited
existing tests for that criterion and wrote none. Changes since 179ec84
(a loop restarts from the notes, a line-by-line ticket check before finish, the piped-timeout hint) target those
misses and are not yet measured.

Runs 15–17 measured the coder's thinking. Off (run 15): simple tickets 2–3× faster, but one-command-start ran
for more than 5 hours without finishing. **Adaptive** (`coding_thinking: -2048`, now the default: think on the
first step, after a failed command and after a repeat) against the run 13 baseline, same 8 tasks × 2 trials:

| | Run 13: thinking on every step (179ec84) | Run 17: adaptive (a5d12ae) |
|---|---|---|
| Trials accepted by the gates | 15 of 16 | 15 of 16 |
| Tasks solved in every trial (pass^k) | 7 of 8 | 7 of 8 |
| Maintainers' hidden tests on accepted results | 8 of 11 | 8 of 11 |
| Total time, 16 trials | 8.4 h | 5.1 h (−39%) |
| Time per ticket, median | 20 min | 10 min |
| Completion tokens | 1.42 M | 0.49 M (−65%) |
| one-command-start | 73 min, 62 min | 15 min, 12 min |

The misses are unchanged: #20 (one trial passes the gates but not the maintainers' test on finishing after a
report; the other fails three rounds) and the repository map's `async def`. Run 17's transcripts gave the loop
measures a2bf1f5, ff5f320, 5d0d992 and 4c237cc (in the table below).

Run 18 (7e08caf: those measures, plus the idle thinking trigger from d1c19c4 that run 17 did not have) completed
14 trials before the 27B server went down: 14 of 14 accepted, hidden tests 8 of 10, so the new checks cost no
quality; the red-check explanation turned #20's unfixable round into a 46-step fix. But the time rose to 9.8 h
for 14 trials (median 21 min): the coder thought on 31% of steps against 15%, 40% of those turns from the idle
trigger, and seconds per step doubled. The idle trigger was removed (8da13f5).

Run 19 (9ab5906: the checks without the idle trigger): 14 of 16 accepted, pass^k 6 of 8, hidden tests 8 of 10,
8.5 h, median 12 min. The six easier tasks ran at run 17's pace (4–13 min); the hours went to #20 (118 and
163 min, one trial failing three rounds) and one-command-start (28 and 40 min). The repository map passed the
maintainers' tests in one trial, the first time. Two transcripts gave the next measures: f123b5b4 wrote 56 probe
scripts (`dbg1.py`…) across three rounds and never a test (2e68d2a: the third scratch file is a repeat, and a
mid-session reminder when no test has changed); #20 spent about 40 minutes of one round waiting on nine 9B
explore calls (13630c4: explore prefers the 27B). Run 20 measures both.

Run 20 (13630c4), the current baseline: **16 of 16 accepted, pass^k 8 of 8** (the first run with every trial
accepted), hidden tests 7 of 12, 6.3 h in all, median 9 min, 0.48 M completion tokens. Every task outside #20 ran
at or under run 17's time: one-command-start 9 and 11 min, f123b5b4 23 and 20 min in one round each (50 and 22
before), the 9B's 100–630 s explore answers became 39 s on the 27B. #20 alone took 3.8 h of the 6.3 and still
fails the maintainers' tests in both trials; with the repository map's `async def` (1 pass in 5 trials since
run 19) it holds every remaining hidden miss. The model-name-response hidden miss in one trial did not repeat.
Run 21 (3dc8ba1, green checkpoints): 16 of 16 accepted, pass^k 8 of 8, hidden tests 7 of 12, 6.9 h, median 9 min:
the same as run 20. Checkpoints were taken in every trial (1–9 each), but none of the four failed rounds had one
that passed the gates, so no restart from green happened; the measure cost nothing and is kept for the case it
was built for. #20 took two rounds in both trials and failed the maintainers' tests in both.
The two maintainers' tests #20 fails most (3 of the last 5 trials) encode criterion 5's edge: a request for the
report *and something else*, or to *explain* the report, must not end at the report. Every coder finished on any
report. The task's interface sentence now says so; run 22 (two trials of #20 alone) measures it.
The repository map's miss is of the same kind: `async def` is named nowhere in the ticket, and the coding prompt's
"cover async functions" was not enough in 7 of 8 trials; the task's interface now lists it (run 23 measures).
Run 22 (#20 alone): the first trial was lost to a model-server outage; the second was accepted in two rounds and
passed 6 of the 7 maintainers' tests, including both "report" tests that every earlier trial failed. The one miss,
`usage.tool_search` not counted, is now in the interface too (a756e39). The rerun of the first trial (run 22b, before that
sentence) was accepted in three rounds with the same 6 of 7: both trials now pass the two report tests.
Run 23 (the repository map alone, `async def` named): both trials accepted in one round (431 s, 561 s) and both pass
the maintainers' tests, after 1 pass in 10 trials before. Run 24 (7de4366: all of it) sets the baseline anew.
model-name-response's first trial failed its hidden test in runs 20, 21 and 24 while the second passed each time:
the first task of a run has no repository memory, and the memory is what said to read the model name from the
configuration rather than the chain object (the maintainers' test injects fake chains). The ticket now says so.

Run 24 (7de4366, before 0e6a463 and 63398ae): 15 of 16 accepted, pass^k 7 of 8, hidden tests 8 of 11, 7.6 h,
median 10 min. The failure was #20's second trial (three rounds); the first #20 trial passed the gates in two
rounds and failed the three "report" tests that run 22's trials had passed, so the clarified sentence narrows
the miss without removing it. one-command-start's second trial needed three rounds: 96 commands and no edit in
one of them, most of them probe scripts written with heredocs (3664d42: those count as scratch; a session with
no edit in 30 steps restarts from its notes and ends at 45; probes no longer count as green checkpoints).
Run 25 (3664d42 and the task clarifications): 15 of 16 accepted, pass^k 7 of 8, hidden tests 8 of 11, 6.2 h, median
11 min. #20's first trial passed all seven maintainers' tests, the first full pass in 25 runs; its second failed three
rounds. model-name-response passed both hidden checks (the memory-less miss is gone). f123b5b4 missed two different
hidden tests, one per trial, after passing in every run since 14: the finest compaction semantics, and sampling. The
no-edit escalation ended one zero-edit round at step 45 instead of 80. Over runs 20–25 the set sits at 15–16 of 16
accepted and 7–8 of 11–12 hidden tests; the remaining misses are at the model's edge on #20 and f123b5b4.

Earlier history:

Run 8: the quick profile on commit cd153d1, Qwen 27B on one GPU, 7 trials of the 4 General_AI_Agent tasks
before the session ended:

| | Quick, run 8 (cd153d1) | Thorough, run 5 (c86bc30) |
|---|---|---|
| Trials accepted by the gates (and the review, when thorough) | 7 of 7 | 8 of 9 |
| Maintainers' hidden tests on accepted results | 2 of 3 (the miss: the spec never named the interface the tests import; fixed in the task) | 2 of 3 |
| Time per ticket | 6 min to 48 min (median 25 min) | 9 min to 2 h 12 min (median 52 min) |
| Prompt-cache hit rate on the model server | 89% (run 7) | 68.5% before append-only history |

| Task | Run 8 trials | Run 7 (quick, older code) | Run 6 (thorough) |
|---|---|---|---|
| model-name-response | 350 s, 536 s | 554 s | 400 s |
| model-selector | 1,514 s, 834 s | 2,172 s | 3,333 s |
| one-command-start | 2,483 s, 2,868 s | 2,629 s | 2,586 s |
| #20 tool search | 2,552 s (2 rounds) | out of steps | — |

Runs 10 and 11 compared the profiles on the four hardest tasks (one trial each, the same commit apart from a
coding-prompt change, the same specs):

| Task | Quick (run 11) | Thorough (run 10) |
|---|---|---|
| #20 tool search | 1,716 s, hidden: 1 of 4 tests fail | 7,641 s, hidden: 2 of 4 fail |
| Repository map (this project) | 1,098 s, hidden: misses `async def` | 2,750 s, hidden: misses `export default` |
| Routing (this project) | 453 s, hidden pass | 1,776 s, hidden pass |
| Result cap (this project) | 2,782 s, hidden pass | 5,205 s, hidden pass |
| Total | 1.7 h, 2 of 4 | 4.8 h, 2 of 4 |

On #20 the thorough profile's own acceptance test encoded a misreading of the spec (it required `define` for
`tool_search`), and the coder may not change acceptance tests, so the misreading was locked in. Acceptance
tests written before coding are only as good as the test writer's reading; they need a way to be disputed.

Run 12 added the **probe** to the quick profile (after the gates, a tester writes tests from the ticket and the
harness reproduces each reported failure): on the same four tasks it found two real defects the gates had passed
(`tool_search` missing from `allowed_tools`; a leaf file with the ticket's words ranked below a central one), but
the hidden-test pass rate stayed 2 of 4 and the time doubled (3.4 h against 1.7 h). On #20 the round that answered
the probe broke the gates in the last round and the ticket failed; the probe now falls back to the earlier gated
result and names the failing probes in the PR. The probe stays opt-in (`probe: true`, `eval --probe`) until more
trials show it pays.

So the **quick profile is the default**; the thorough profile stays available per repository or ticket. Run 9
measures the quick profile on 8 tasks from two repositories (this project's own history added). The old
25-node pipeline never completed GE-FDD2275A; this design completes it in each trial.

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
| Context is a finite budget; smallest set of high-signal tokens [7][4] | 105K-token working budget (about 99K real tokens at the measured 3.7 characters per token); 40K, and 80K counted at 3 characters per token, both caused re-read thrash in evals | done |
| Keep the prompt prefix stable for caching: append, don't edit [21][C] | history is append-only between rare, batched clearings | done |
| Right-altitude prompts; must-always rules belong in code [7][4] | rules the model ignored became code: forced finish on the last step, forced action after a cut-off | done |
| Just-in-time retrieval with a little preloaded [7][8] | opening brief (agent guide + layout); agentic search | done |
| Clearing, compaction, notes, sub-agents [7][C] | superseded reads and old outputs cleared in batches; notes file; compaction; `explore` | done |
| Fresh context per retry [4][13] | each round is a new session in the same workspace, opened with notes and feedback | done |
| Durable event log, restartable harness, disposable sandboxes [22] | SQLite event log; leased idempotent steps; one workspace per ticket | done |
| Routing: small model for easy inputs [1] | role routing by reachability and context fit. `explore` preferred the 9B until run 19: the coder waits for every answer, and the 9B (16K context, compactions) took 103–631 s a question, nine times in one round of #20; it now prefers the 27B, with the 9B as fallback | done |
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
trusted unattended. Which one pays, and when, is measured, not assumed: on the same tasks the thorough
profile was no more reliable and two to three times slower (runs 5–8), so quick is the default.

## The agent loop

`agent.py` runs one loop for every agent against an OpenAI-compatible endpoint (vLLM serving Qwen with
native tool calls). It stops on the finish tool or a stopping condition:

- a step budget (80 coding, 40 intake, 30 plan and review, 20 explore);
- four turns without a tool call;
- three repeats in a row with nothing changed in between (an identical call, a command with the same output,
  or an edit back to an earlier content): a restart from the notes once, then the end;
- cancellation.

OpenHands' stuck detector names the same families, with its thresholds: the same action and observation four
times, the same action failing three times, three agent messages without a tool call, two action-observation
pairs alternating for six cycles ([agent stuck detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)).
This loop's thresholds are tighter (three repeats, four idle turns) because every step here costs 15–45 s of a
single GPU.

Each measure below came from a failure in a live transcript:

| Failure seen | Measure |
|---|---|
| Near-greedy decoding repeated one call forever | Qwen's recommended sampling; three identical calls restart the session from its notes once (run 13: a coder re-read the same lines in circles and lost the round), a second loop ends it |
| Reasoning hit the output limit without acting | reasoning tail handed back as notes; **next turn runs without thinking and must call a tool** |
| Re-reads of cleared results were refused as repeats | clearing a result also forgets it in the repeat guard |
| Explored for many steps without editing | nudge to plan and change, repeated every 15 steps |
| Reviewers read until the budget ran out, ignoring warnings | warning 3 steps before the end; **the last step offers only the finish tool** |
| `finish` was refused for stale evidence, costing turns | the harness runs the test command itself and accepts if it passes |
| Uncapped reasoning used every output token and gave no answer | **a thinking budget per agent, enforced by vLLM** (`thinking_token_budget`): planning 6,144, review and intake 4,096, coding, explore and memory 2,048 (coding was 4,096 until run 7: 75% of its output was reasoning) |
| Broken edits were found steps later by a failing suite | **an edit that breaks syntax is rolled back** with the error (Python, JSON, JavaScript); SWE-agent measured 3 points for this |
| The 9B explorer's two whole-file reads overflowed its budget by themselves; it compacted, re-read them and compacted again every step | **one tool result takes at most a quarter of the working budget**, cut at a line with where to read on; **compacting on two steps in a row asks for the final answer**; the explorer gets a slim brief |
| `npm test 2>&1 \| tail -25` reported tail's exit code, so a failing suite read as passing | commands run under **`bash -o pipefail`** |
| Three hung test runs cost 900 s of a 2,095 s coding session on an 11 s suite | **the command limit follows the base suite's time** (4× + 30 s, at least 60 s), and a timeout says the run probably hangs |
| A failed `edit_file` cost a re-read before the retry | the error shows **the closest lines of the file, numbered** (search/replace beats diffs for Qwen: Diff-XYZ, Aider) |
| `test \| head; echo EXIT=$?` exits 0 while the suite errors; one test was re-run 77 times changing only the `head -N` count | a command fails when its output says so, not only by its exit code; **the same output with nothing changed is a repeat** |
| Thinking on every step: most of the output was reasoning. Thinking off: a debugging-heavy ticket ran out of steps three rounds running | **adaptive thinking** (`coding_thinking: -2048`, the default): think on the first step, after a failed command and after a repeat. Run 17: same quality as run 13, −39% time. Run 18 added a trigger every third step once six passed without an edit: 40% of all thinking turns, the longest ones, and the time per step doubled, so it was removed |
| Three existing tests were updated inside a callback the code under test catches; they passed on the base too, and three rounds passed without the coder seeing why (run 17, #20) | the red check's detail says the changed tests pass on the base, names them, and says what to do; **a feedback round that changes nothing must say why under unresolved** |
| A coder had the whole suite green at step 67, then added tests it could not satisfy in 33 more steps; the round failed and the next one spent 90 steps decoding the broken state (run 20, #20) | **green checkpoints**: a snapshot each time a whole-suite command passes; a failed round's next round starts from the latest checkpoint that passes the gates, with the later edits attached as a diff |
| A coder wrote 56 probe scripts (`dbg1.py`…) across three rounds and never a test (run 19) | the third scratch file counts as a repeat; half-way through a session with no test file changed, the coder is told the gates will refuse the change; a finish is refused once while an added file looks like scratch |
| Told its call was a repeat, a coder made it three more times while saying it would redesign its test (run 17) | **the repeated tool is not offered on the next step**; alternating it with other calls still trips the restart-then-end guard |
| A coder applied an edit and its exact inverse twenty times over without running a test (run 17, #20, steps 56–97): each one changed the file, so nothing counted as a repeat | **an edit that leaves a file with a content it had before is a repeat**, with a hint to run the tests and see which version is right |
| A forced last-step finish was refused for lacking passing evidence, and the summary was lost with it | the last step's finish is accepted, with the problem recorded as unresolved |

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
- **Append-only history.** Nothing is rewritten until the 105K-token budget is passed. Then, in one batch:
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
11. **Failures already on the base are judged by name.** When the suite fails on the result, the gate runs it
    on the base too and passes only if every failing test already failed there (no new failures). It names
    failing tests from unittest, pytest, node --test, jest and vitest output; when a run names none, the gate
    stays strict. Another runner's output, or a pre-existing failure that changes its name, needs a person.
    The coder is told what already fails. Seen in run 9, where nested sandboxing made 23 of this project's own
    tests fail.

## What is missing, in order of expected value

| # | Item | Why | Cost |
|---|---|---|---|
| 1 | ~~Coder thinking: 2,048 tokens vs. off~~: **measured** (runs 15–17, above). Off is 2–3× faster on simple tickets but lost a debugging-heavy one; adaptive thinking kept the gain (−39% over 16 trials) at equal quality and is the default | 75% of the coder's output was reasoning; Aider found Qwen3 edits as well or better without thinking | done |
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
