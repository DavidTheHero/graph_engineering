# Graph Engineering

An executable, single-host software engineering controller supervised by Codex.
It sends scoped JSON tasks to local Qwen models through **OpenCode 2.0.32**,
validates proposals, edits isolated Git worktrees, runs frozen regression tests,
reviews actual diffs, and manages issues, pull requests and policy-bound merges.

The project is independent of its target repositories. No target CI, security
policy or agent instruction file is rewritten by the framework.

## Verified outcome

On 18 October 2026, both supplied model servers were used through OpenCode.
Two unrelated real Git repositories (Python unittest and JavaScript node:test)
completed local issue-to-merge workflows and post-merge tests. A real public
[fixture PR](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/pull/2)
merged with explicit human approval, required GitHub CI and branch protection.
Its merge commit is `0b1dbf9444e7287116dd46a71b1fe2411d675f67`; local post-merge
tests and the GitHub push workflow passed.

A private fixture PR remains blocked because its rules API returns HTTP 403.
An actual controller-crash test exposed an issue-creation duplicate in the first
implementation. The corrected ledger forbids creation during uncertain recovery;
a second real crash reconciled exactly one remote issue. Earlier failures are
retained. See [the demonstration report](docs/DEMONSTRATION.md) for receipts,
limitations, operations, counts and evaluation results.

## Install

Requirements: Python 3.10+, Git, the verified OpenCode 2.0.32 CLI, reachable model
servers; Node for JavaScript repositories; authenticated `gh` for live GitHub.
The Python runtime has no third-party dependencies. Building uses setuptools.
Windows 11, Python 3.10.9, Node 24.20.0, Git 2.39.2 and gh 2.102.0 were exercised.

```powershell
cd D:\git_code\graph_engineering
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\graph-engineering.exe doctor
```

The prepared `.venv` already contains the editable installation. If using the
source without installation, set `PYTHONPATH` to this project's `src` directory
and run `python -m graph_engineering`. Other OpenCode versions are rejected until
their source/schema and real provider invocation have been verified.

## Connect a repository

Start with a **clean, current base checkout**. Provision its dependencies yourself;
the controller does not install packages or run unreviewed setup commands.
Supported initial adapters are Python (unittest/pytest) and JavaScript/TypeScript
(package scripts or node:test). Custom test argv may override detection.
For nonstandard regression helpers, add their glob paths to `protected_paths` in
the operator configuration. Conventional test names and explicit test script files
are frozen automatically; baseline execution must leave the repository clean.

```powershell
$ge = 'D:\git_code\graph_engineering\.venv\Scripts\graph-engineering.exe'
& $ge init 'D:\path\to\repository' --trust-local-code --routing multimodel
# Use the exact config path printed by init:
& $ge scan --config '<printed-config-path>'
& $ge status '<run-id>' --events
& $ge resume '<run-id>'
```

`scan` reproduces a failure, validates/deduplicates it and creates a local ticket,
then stops before planning. `execute --config <path>` starts a complete new run.
Use `resume` to continue the existing scan; starting another run against the same
failing base is deduplicated. Only a reproduced failing test/exception becomes an
actionable finding. Speculative, environmental and low-confidence findings block
for supervisor investigation. Existing unmarked issues with matching titles or
file/test evidence block speculative duplicate creation; confirmed linkage needs
the operator to attach the displayed marker to the existing issue, then resume.

Use a single controller `--home` for each coordinated deployment. The default is
this project's `.runtime`, which keeps all controller files in this folder.
Different repositories may run in separate processes; mutations within a repository
are serialized by an OS file lock. Independent controller homes/machines are not
coordinated. Preserve the whole home directory to resume a run.

Local execution is **disabled by default**. `--trust-local-code` explicitly permits
repository code to run as your current OS account. Worktrees, tool denial and a
filtered environment are not a hostile-code sandbox. Use a credential-free VM or
separate OS execution account for untrusted code; this initial release has no
built-in Docker/VM runner. GitHub credentials stay in the controller, not worker or
test environments, but same-user hostile processes could still access host files.

## Live GitHub and approvals

```powershell
gh auth status
& $ge init 'D:\path\to\repository' --backend github --slug owner/repository `
    --trust-local-code --routing multimodel
& $ge execute --config '<printed-config-path>'
& $ge status '<run-id>' --events
# Review the actual diff, test receipts, review and GitHub policy snapshot first:
& $ge approve '<run-id>' --head '<full-tested-sha>' --actor '<human-name>'
& $ge resume '<run-id>'
```

The controller pushes only its scoped branch to the verified GitHub repository.
It never force-pushes, changes protections, supplies `--admin`, or counts model
comments as required human reviews. Required external CI and GitHub App sources
are checked independently of the controller's own test status. Missing/failed or
pending checks, unknown policy, stale heads, changed base, conflicts and missing
approval block merging. Resume after pending CI completes. The live backend
leaves the original local base checkout untouched after remote merge: fetch and
fast-forward it explicitly before starting the next run.

Human approval is the default. Automatic merge is opt-in (`--auto-merge`), additionally
requires a configured `auto_merge_paths` allowlist, trusted code, low severity,
small changes, no sensitive paths, independent passing tests/review and current
repository policy. An empty allowlist permits no automatic merges. No automatic
live merge was performed in the demonstration.

`resume` resets only the current node's attempt budget and records that action.
For an uncertain external write it performs read-only reconciliation. If GitHub
does not show a unique matching object, the controller stops rather than trying
another create. Resolve ownership/visibility with evidence; do not delete ledger
rows just to make a blocked run continue.

## Persistent controller, monitoring and cancellation

```powershell
& $ge enqueue --config '<printed-config-path>'
& $ge serve --poll 5
# In another terminal:
& $ge status '<run-id>' --events
& $ge stop '<run-id>'
& $ge resume '<run-id>'
```

`serve` is a separate restartable foreground controller, independent of an
interactive Codex session. Run it under your service manager or, on Windows,
`Start-Process -WindowStyle Hidden` with log redirection. `serve --once` processes
one queue sweep. It never auto-approves; blocked CI/policy runs require explicit
resume. Cancellation is persisted and checked between nodes and during worker/test
execution; process trees are killed on active worker/test cancellation or timeout.
Git/API operations have bounded timeouts; these operations finish or time out before
the next cancellation boundary. Detached hostile children require OS containment.

SQLite WAL persists versioned run state, transitions, approvals and write intents.
Each attempt retains hashed input/output receipts. Worker invocations keep separate
OpenCode profiles, session IDs, prompts, generated V2 configuration and raw output.
Artifacts and logs can contain proprietary source: restrict access to controller
storage. Known environment secrets and GitHub token patterns are redacted; this is
not a general-purpose detector for arbitrary secrets committed to a repository.

## Models and routing

| Profile | Investigation | Planning/implementation | Independent diff review |
|---|---|---|---|
| `baseline` (default) | A | A | A in a separate session |
| `multimodel` | B with selective retrieval | A | B; contradictory output escalates to A |

A is `qwen3.8-27b` at `http://192.168.0.60:18020/v1` (advertised context 150000).
B is `docker.io/local/qwen35-4b:8k` at `http://192.168.0.62:18035/v1` (8192).
Output limits 4096/1024 are controller choices, not claims about server maxima.
Missing identifiers and unavailable servers fail closed; configured A fallback is
explicitly logged where used. Large review tasks route to A when B's byte budget
cannot safely include the evidence. Set `allow_same_model_review=false` if a
different model is mandatory; no model can approve a merge without deterministic gates.

The current two-defect evaluation measured 34.85 seconds per multi-model task and
23.27 seconds per A-only task, with equal observed completion. Consequently A-only
is the default; the multi-model mode is available for controlled comparisons.
These are small descriptive samples, not a general model ranking. Both endpoints
accepted invalid authentication in probes. Protect the inference network and add
authentication/TLS before production use. If credentials become required, configure
`api_key_env` per model; never put their values in configuration files.

## Test and repeat the demonstration

```powershell
python scripts/verify.py
python scripts/run_evaluation.py --output evidence/evaluation/new-run --mode both --merge-local
.\scripts\demo.ps1
& $ge graph
```

The evaluation creates fresh real Git repositories and calls real local models.
`--merge-local` explicitly authorises local test approvals and real local Git merges;
GitHub objects are simulated in that backend. Omit it to stop at approval.
The PowerShell demo stops for approval. Existing fixture directories are never
overwritten. Tests using worker doubles are explicitly labelled as unit tests.

Run `probe_tool.py` only for the disposable read capability probe; production
workers deny all tools. `live_recovery.py` and `setup_public_fixture.py` target the
specific authorised demonstration repository and are not general setup commands.
Do not rerun their creation/setup steps against a repository you have not authorised.

## Project layout

```text
graph_engineering/
  pyproject.toml              # package and CLI
  src/graph_engineering/
    cli.py                   # init/scan/execute/enqueue/status/resume/stop/approve/serve
    workflow.py              # node registry, contracts and conditional handlers
    runtime.py               # SQLite, lock, deadlines, artifacts and effect ledger
    repository.py            # Python/JS adapter interface, worktrees and verification
    models.py                # probes, V2 config and isolated OpenCode sessions
    publication.py           # local and live backends; independent merge gates
    evaluation.py            # real defect seeds and measured evaluation
  tests/                     # unit, integration and failure injection
  scripts/                   # repeatable demos, probes, verification and recovery
  configs/                   # repository and V2 examples
  docs/                      # ADR, research, contracts, operations and actual results
  evidence/                  # raw model/API/test/source/CI receipts
  .runtime/                  # default durable controller storage (ignored)
  .venv/                     # prepared installation (ignored)
```

The current scope is bounded source-only repair driven by existing failing tests.
Tests/manifests/instructions/CI/security files cannot be changed by workers.
Dependency installation, new regression-test generation, broad vulnerability
discovery, semantic issue deduplication, automatic rebasing, merge queues and
distributed scheduling need further implementation. For unattended production,
add real execution isolation, an authenticated approval service, storage access
controls/backups, broader workload evaluations and supported GitHub rules.
See [security and operation limits](docs/OPERATIONS.md) and the [ADR](docs/ADR-001.md).
