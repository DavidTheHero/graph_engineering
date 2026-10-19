# Accepted-ticket autonomous development

Choose a repository in the dashboard, then **Autonomy policy**. A named operator
can enable development, publication and locked npm setup independently. Publication
and setup require development to be enabled. All three permissions default off
for new repositories. Policies persist in the existing SQLite scheduler settings
and generate audit events; there is no separate frontend state machine.

An accepted current enhancement dispatches automatically under the development
policy. The controller pins its source commit, prepares isolated worktrees and
runs the registered suite. Qwen 27B proposes a new regression test; an independent
Qwen 4B session reviews small tests, with explicit 27B routing for larger evidence.
Deterministic processes require the test to fail with an assertion twice against
the original source. The controller freezes the test, then reuses the existing
planning, source implementation, test, independent review and repair nodes.

All model work uses separate OpenCode V2 proposal-only sessions. Actual prompts,
responses, version, model ID, session IDs and latency are retained. Large source
string lengths are enforced by the controller's full JSON schema; the transport
schema omits length repetitions above 1024 to avoid observed local server grammar
failures. This does not relax controller validation or output/context budgets.

If publication is enabled, verified work proceeds to the configured backend.
For `local`, issue and PR objects are **local simulations**; no GitHub objects are
created. For `github`, enabling publication permits real writes to the already
configured slug. The worker rechecks publication configuration before writing.
Merge approval remains required for accepted-ticket runs, bound to the tested
commit, with current checks and branch protections still enforced.

If autonomy is off, the existing development queue requires **Start development**
and delivery requires explicit publication approval. Accepting a proposal alone
does not override that policy.

Model failures and dependency-install failures retry with the existing exponential
backoff, at most three ticket retries and two node attempts per retry. Source or
environment verification failures remain blocked. Exhausted work stays visible
in Dead Letter; it is not silently restarted. Refine the environment or create a
scoped follow-up after diagnosis. A cancelled ticket has its own board column and
queue, never automatically resumes, and requires explicit **Retry with backoff**.
Pause, cancellation, dependencies and resource locks remain authoritative.

Locked npm setup runs `npm ci --ignore-scripts --no-audit --no-fund` only inside
controller-owned worktrees. A successful receipt binds the worktree and lockfile
hash. A partially populated `node_modules` directory is insufficient evidence of
success. No Python dependency installation or arbitrary setup scripts are inferred.
Worktrees and scrubbed environments are suitable for trusted repositories; they
are not OS containment for hostile code.

## Run under Windows Task Scheduler

```powershell
cd D:\git_code\graph_engineering
.\scripts\install-controller-task.ps1 -Start
Get-ScheduledTask -TaskName GraphEngineeringController-8771
# Open http://127.0.0.1:8771
```

This creates a manual-start task under the current logged-in user with limited
privileges, no password, no logon trigger, unlimited task duration and up to three
failure restarts one minute apart. It runs the existing controller and scheduler
independently of an interactive Codex turn. A Windows job object binds descendant
processes to the service lifetime, including on forced task termination. Logs append to
`.runtime/general-ai-agent/controller-service.log`. The task requires a logged-in
user and is not an unattended boot service.

Use the dashboard to pause admission and wait for active steps to checkpoint
before maintenance. `Stop-ScheduledTask -TaskName GraphEngineeringController-8771`
stops that controller; `Start-ScheduledTask` restarts it. Existing kernel locks and
the scheduler's recovery logic reconcile interrupted workers. Do not launch two
controllers against the same home or listener. The install script accepts
`-HomePath`, `-Port` and `-Workers` for another instance.

## Verify

```powershell
.\.venv\Scripts\python.exe scripts/verify_phase3.py --output evidence/execution-controls/checks
.\.venv\Scripts\python.exe scripts/export_ticket.py GE-2580F847 --home .runtime/general-ai-agent --output .runtime/token-ticket.json
```

Unit/integration/browser inference doubles are labelled separately from live
OpenCode receipts. Browser tests exercise policy persistence, cancellation,
revision acceptance, synchronized queues, journey events and accessibility.
See [the execution results](EXECUTION-CONTROLS-RESULTS.md) for observed outcomes.
