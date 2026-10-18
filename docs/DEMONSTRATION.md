# Implementation and observed demonstration report

Date: 18 October 2026. Project: `D:\git_code\graph_engineering`.

## Delivered executable system

An independently initialized Git project contains a Python package/CLI, a 20-node
durable graph with conditional paths, SQLite WAL state/events/approval/effect ledger,
OS repository locking, bounded node deadlines and correction loops, real Git
worktrees, Python and JavaScript adapters, isolated version-pinned OpenCode workers,
strict JSON artifacts, selective retrieval, local/live publication backends,
head-bound independent gates, a restartable queue controller, evaluation and tests.

Actual code is in `src/graph_engineering`; templates in `configs`; tests in `tests`;
operating/probe/evaluation scripts in `scripts`; documentation and node contracts in
`docs`; original source, model, test, GitHub and CI receipts in `evidence`. Runtime
databases, profiles, fixture repositories and worktrees are retained on disk but
excluded from the project's Git history. [README](../README.md) contains the actual
directory tree, setup, CLI usage and model configuration. [ADR](ADR-001.md) records
the choice made before implementation; [research](research-frameworks.md) links
commit-pinned OpenCode/LangGraph/SWE-agent/OpenHands/GitHub Actions/API sources.

The prepared editable CLI installation runs from `.venv/Scripts/graph-engineering.exe`.
No Python runtime dependencies were added. A user can initialize a compatible clean,
current repository, scan or enqueue/execute it, inspect evidence, stop/resume, approve
an exact head, and publish/merge subject to the repository's current rules.

## Models actually used

- A: `qwen3.8-27b`, `http://192.168.0.60:18020/v1`. Actual OpenCode planning,
  implementation, A-only investigation/review, and disposable read-tool execution.
- B: `docker.io/local/qwen35-4b:8k`, `http://192.168.0.62:18035/v1`. Actual OpenCode
  bounded investigation, independent diff review, and disposable completed read call.
- Both passed chat, structured output, actual OpenCode sessions and tool capability
  probes. Authentication was not enforced in the observed LAN deployment. Context
  metadata, output-limit uncertainty and initial failures are documented in
  [OpenCode evidence](opencode-evidence.md), with raw requests/responses retained.
- Production workers execute **zero tools** intentionally. Complete-file proposals
  are applied by the controller after schema/path validation. Disposable tool probes
  separately observed one successful read per model; these tiny counts are not a
  general tool-calling reliability estimate.

## Actual tests

`python scripts/verify.py` executed **58 tests successfully**, including
real Git/worktree regressions, two different stacks, subprocess cancellation and
timeouts, unavailable/invalid model responses, bounded retries, path traversal,
wrong worktree identity/ancestry, escaped secrets, malicious proposals/instructions,
missing dependencies, queue/controller behavior, exact-head approvals, app-bound
checks, unknown branch rules, review requirements, scoped automatic local policy,
merge recovery and uncertain external writes. Runtime/source/scripts compilation
also passed. [Exact unittest output](../evidence/verification/unittest.json) and
[compile receipt](../evidence/verification/compile.json) retain commands and exits.
Worker-double unit tests are labelled; they are not presented as model demonstrations.

The repeatable evaluation called the actual models, ran existing frozen regressions,
and performed real Git worktree commits and local merges. Local GitHub issues/PRs
were explicitly simulated. Each repository's source-only defect has real correct
and defect commits with observed before/after tests in seed receipts. The JavaScript
repository includes malicious AGENTS text; it never expanded worker authority or
changed protected files. Both stacks have three independently executed tests.

Latest multi-model positive runs:

| Run | Stack | Outcome | Duration |
|---|---|---|---|
| `7076c760e1ef4f9d` | python | completed | 38.31s |
| `4f228a2014024f11` | javascript | completed | 31.39s |

The fixed repositories also returned `no_findings` in two negative scans. Forced
failed local checks and absent approvals blocked merge; stop/close/reopen/resume
retained the same issue/PR objects. A separate real queued controller sweep completed
a clean scan through the installed CLI.

## Evaluation versus a simpler baseline

The comparison retains the same deterministic safety graph and uses A for every
model role as the simpler routing baseline. An earlier direct-API microbenchmark
is retained separately; it is not a safe end-to-end engineering baseline.

| Metric, latest controlled sample | Multi-model | A-only baseline |
|---|---:|---:|
| Validated seeded defects / positive cases | 2/2 | 2/2 |
| Fixed negative scans | 2/2 | 2/2 |
| Detection precision | 100% | 100% |
| False positive rate on two known negatives | 0% | 0% |
| Correctly resolved / regression success | 2/2 | 2/2 |
| Mean positive task duration | 34.85s | 23.27s |
| Recovery success in local injected cases | 2/2 | 2/2 |
| Observed merge-policy violations / duplicate issues / duplicate PRs | 0 / 0 / 0 | 0 / 0 / 0 |

The latest comparison contains 16 real OpenCode model invocations: A supplied 12
valid artifacts, B supplied 4. Latency and per-model success counts are in
[the exact summary](../evidence/evaluation/run-03/summary.json). These artifact
successes are not tool calls. Full-workflow token totals are unavailable in the
observed CLI event stream and are not invented; direct protocol responses retain
their actual usage fields. The graph is auditable, but these two tiny tasks do not
establish general correctness or a multi-model benefit. A-only is now the default;
`--routing multimodel` explicitly enables the measured two-model roles.

The first evaluation failed safely: B produced contradictory review labels, and
models supplied an invalid line reference/speculative category. Those failures
remain in `evidence/evaluation/run-01`. Numbered source, bounded candidate correction
and explicit deeper review were added without interpreting blocking findings as
success. Runs 02 and 03 subsequently completed both stacks and both profiles.

## Live GitHub operations actually performed

1. Created a private disposable repository,
   [issue #1](https://github.com/DavidTheHero/graph-engineering-demo-20261018/issues/1)
   and [PR #2](https://github.com/DavidTheHero/graph-engineering-demo-20261018/pull/2).
   Local tests and B review passed. Branch-rule inspection returned HTTP 403 because
   that private-repository feature requires a paid plan; the controller left it
   **blocked and open**, with no merge or protection bypass.
2. After explicit user permission, created a separate
   [public fixture](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018).
   Only generated example code/tests and a pinned checkout regression workflow were
   published. Its newly configured main branch requires `regression`, strict checks,
   administrator enforcement and disallows force pushes/deletion.
3. Created [public issue #1](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/issues/1)
   and [PR #2](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/pull/2),
   pushed a scoped task branch, published a model review as COMMENT and an exact-head
   independently observed test status. Initial pending CI and missing approval blocked
   merge. The original defective base's real CI run failed.
4. The user explicitly approved head `d82a946f2555033d7b706929bc46f452f6606715` in
   chat. That approval was recorded under the human actor label and exact SHA. Fresh
   tests and current policy passed. GitHub's normal merge endpoint completed, and the
   controller observed merge commit **`0b1dbf9444e7287116dd46a71b1fe2411d675f67`**.
5. The controller fetched and ran all three frozen regression tests at that merged
   commit in a separate detached worktree. They passed. The post-merge GitHub push
   workflow also passed. Issue #1 is closed. No required review was impersonated,
   no admin override or force push occurred, and the original local checkout was
   preserved. A subsequent attempted run on that stale checkout was blocked at
   discovery, before model work.
6. A separate actual hard-crash experiment initially exposed a duplicate (#3 then
   #4), now both closed. The corrected test killed the child after the real creation
   of [issue #5](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/issues/5),
   left a durable intent without a receipt, reopened the database and reconciled that
   exact issue. Observed matching issues: **1**;
   corrected-test duplicates: **0**. Issue #5 is closed.
   The final library's read-only issue/PR/review reconciliation was also executed
   against the actual remote objects. Earlier duplicates are disclosed, not erased
   from the measurement history.

Actual CI links:

- [Defective baseline: failed](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/actions/runs/39156746520)
- [PR regression: passed](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/actions/runs/39156848318)
- [Post-merge push: passed](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/actions/runs/39157033913)

Raw [public state](../evidence/final/public-run.json),
[transitions and approval](../evidence/final/public-events.json),
[GitHub/API/CI receipts](../evidence/final/live),
[original recovery failure](../evidence/final/recovery-original-failure.json) and
[corrected hard-crash receipt](../evidence/final/recovery-corrected.json) distinguish
live operations, real local Git operations and simulated local GitHub objects.

## Remaining work before unattended production

This is a verified initial system for clean, compatible, pre-provisioned Python and
JavaScript repositories and bounded source-only fixes. It is not an unattended
production readiness claim. Existing tests serve as frozen regressions; worker test,
manifest, dependency, CI/security and instruction edits are prohibited. New-test
generation, dependency installation, broad security/build discovery, semantic issue
deduplication, automatic rebasing, additional adapters/rules and automatic CI polling
are not implemented. Pending/blocked checks need explicit resume.
External GitHub review requests are escalated; the automatic repair loop currently
runs before publication rather than updating an already published PR after external review.

All coordinated controllers must use one home; multiple hosts/homes need shared
leases/fencing and a transactional database migration. Worktrees and environment
filtering do not sandbox hostile same-user code. Add a credential-isolated execution
runner, authenticated approval/control service, storage ACLs/backups/retention,
production alerting and broader workload evaluation. GitHub merge queues and unknown
rules fail closed. Windows was verified; other OS paths need their own runtime tests.
The operator must synchronize a clean local base after a remote merge. Endpoint
authentication, effective maximum outputs and large-repository budgets need explicit
deployment validation. [Operations](OPERATIONS.md) records these boundaries and the
SQLite-to-PostgreSQL/LangGraph migration direction.
