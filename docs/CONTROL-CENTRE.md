# Agentic Software Engineering Control Centre

Phase 2 extends the existing Workflow, Store, adapters and publication gates.
There is one execution cursor per run; the browser never owns ticket status.
The decision record is [ADR 002](ADR-002-control-centre.md).

## Start and connect

```powershell
cd D:\git_code\graph_engineering
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\graph-engineering.exe --home .runtime/general-ai-agent centre --port 8771 --workers 2
```

Open http://127.0.0.1:8771. Keep this process or a supervised service running.
Browser refresh/closure does not stop dispatch. Ctrl+C stops the owned admission
controller; already launched workers retain their locks and finish their current
bounded step. Restart against the same home to reconcile and continue.

Do not run the earlier `serve` loop alongside `dispatch` against the same home.
One scheduler owns the kernel scheduler lock. For an external service manager,
the worker service is independently executable:

```powershell
.\.venv\Scripts\graph-engineering.exe --home .runtime/general-ai-agent dispatch --workers 2
```

For an independently restarted UI, use `centre --port 8771 --attach-controller`
against the same home. This attaches without launching or owning another worker
service. The prepared workspace currently uses the combined `centre` command.

The dashboard displays the observed controller heartbeat. A stale/missing
heartbeat is not represented as a healthy worker service. Keep controller homes
private and back up SQLite plus its artifacts consistently. No startup service
or scheduled task is installed automatically.

1. **Connect repository** registers an absolute Git root. It does not edit that
   checkout, execute its code, enable automatic merging, or enable GitHub writes.
2. **Inspect** reads adapter, commands, Git SHA, dirty state and policy metadata.
3. **Start workflow** defaults to a read-only source scan of the committed HEAD
   in a controller-owned clone. Uncommitted source changes are excluded/preserved.
4. Optional Qwen investigation validates every returned file/line against the
   supplied snippets. Observations remain unconfirmed in Needs Investigation.
5. Assisted development requires the explicit code-trust checkbox. By default
   it stops after a validated ticket; uncheck that option to implement a fix.
6. Autonomous mode requires an already configured automatic-merge policy with
   explicit allowed paths. Selecting the mode cannot create that policy.

Provision dependencies before development. Snapshot clones do not inherit
`node_modules`, virtual environments or uncommitted setup. The controller does
not install packages or weaken failed checks. Worktrees and scrubbed process
environments provide change isolation; they are not a hostile-code sandbox.

## Board, network and controls

The nine columns are Backlog, To Do, In Progress, Blocked, Testing, Code Review,
Ready for Merge, Done and Failed. They derive from durable engine status/cursor.
The scheduling queue is shown separately: e.g. a Ready for Merge ticket can be
waiting for human approval, while an unconfirmed scan is in Backlog and Needs
Investigation. Cards cannot be dragged into a fabricated execution state.

Filter by repository, issue type, severity, priority, model, agent, status, or
search a ticket/run/branch. Group by repository, issue type, severity, priority,
current model, agent, status or workflow run. Board columns initially show up to
50 cards and explicitly load more; queues preview 12 items. Ticket details expose
source SHA, acceptance criteria, evidence, branch/worktree, tests, actual diff,
review, retries, local/GitHub links, model artifacts and the persisted timeline.

**View ticket journey** selects the same ticket in the network. Visited edges,
repair routes and current position come from its events. The accessible node
picker also opens queues and processing nodes, including their waiting/active
tickets and recent execution receipts. Queue entries explain dependencies,
backoff, approvals, resource contention or unavailable models.

The seven compound groups contain the actual registry's 21 graph nodes, observed
queue routes, model pools and dynamically observed execution instances. Multiple
tickets run in separate processes. Configured independent build commands can
explicitly opt into parallel quality execution; child start/result events create
child nodes/tokens and an all-results join. No speculative test-writing or
security agent is drawn as an active worker.

Pan, zoom, fit, drag and save layout affect browser preferences only. Groups can
be collapsed by double-click or the keyboard-accessible group controls. Low zoom
hides station labels; at most 100 ticket markers and 32 child instances are drawn.
Marker motion lasts 650 ms per observed transition and respects reduced-motion
preferences. Processing remains active for the actual backend execution duration.
There are no perpetual activity particles.

**Ticket Journey** replay uses recorded events with a HISTORICAL REPLAY banner.
Return to live restores the current snapshot. URL selection survives reload.
The in-memory console retains 500 events and displays the newest 60; durable
history remains in SQLite. A single ticket detail currently exposes up to 2,000
events; the cursor API can retrieve longer history in pages of 250.

**Pause admission** lets active steps checkpoint. Individual pause, cancel, retry,
model reassignment and triage edits are protected operator commands. Cancellation
uses the existing process cancellation policy; stopped runs require explicit
retry to re-enter scheduling. Manual retries have a three-attempt budget and
exponential backoff. Node attempts and repair budgets remain independently bounded.
Planning/implementation cannot be reassigned to the small model. Reassignment
never switches a running worker session.

Approval records require a named operator and the exact independently tested
commit. They do not bypass fresh checks, independent review, branch policies,
conflicts or changed base/head detection. Default local fixture PRs remain held.
The local backend is explicitly a simulation of GitHub publication; Git branches,
patches and regression commands still execute for real.

## Scheduler and persistence

Additive SQLite tables store ticket metadata, dependencies, leases, health
observations, repository registrations, settings and idempotent commands. Existing
run/event/effect/approval tables remain authoritative. Priority aging promotes
waiting work every five minutes, with original admission time as a tie breaker.
Repository locks, model availability/capacity, dependencies and trust gates still
apply. Worker concurrency is configurable from 1–8, with model capacity initially
one execution per model. A kernel lock also protects the actual selected model
after context routing: a busy fallback is retried within the bounded policy and
then requires operator reassignment/retry, never concurrent oversubscription.
Offline models wait with an explicit reason; operators
can reassign supported future roles rather than silently changing routing.

Each node dispatch gets a fresh execution attempt ID and a separate subprocess.
The worker holds its kernel ticket lock through its checkpoint. Recovery checks
kernel ownership, preserves live workers, clears dead leases and fences delayed
workers by owner ID. It does not turn blocked policy failures into ready runs or
discard uncertain publication intents. Repeated start requests with different
command keys deduplicate the same scan before creating another run.

The event contract contains event ID/time, run/ticket/attempt IDs, parent/child
IDs, source/destination, type, role/model, status and artifact references. SSE
supports cursor and Last-Event-ID replay. Refreshes read a consistent SQLite
snapshot. The browser has no independent queue or workflow state machine.

## API and security boundary

GET endpoints: `/api/session`, `/api/centre`, `/api/centre/ticket?id=...`,
`/api/centre/history?after=...`, `/api/centre/events?after=...`.
POST endpoints under `/api/centre/`: `register`, `inspect`, `start`, `health`,
`ticket-action`, `scheduler`. UI modules are examples of the request contracts.
Writes require an exact loopback Host/Origin, per-boot session CSRF header and
idempotency key. Commands use bounded JSON, not executable shell text. Foreign
origins are denied. Eight bounded SSE connections are allowed concurrently.

Repository/model strings render as text, external links are constrained, worker
artifacts must resolve under the controller's worker directory, and display
payloads are bounded/redacted. The graph renderer is bundled locally; CSP retains
`script-src 'self'` and `style-src 'self'`. No CDN or frontend runtime build is
required. Credentials remain in environment variables under existing policies.

This is a single-user loopback operator surface. It has no multi-user identity,
remote RBAC, cryptographically authenticated human approval or distributed lease
service. Do not expose it through a network reverse proxy as a production console.

## Actual acceptance evidence, 19 October 2026

| Scenario | Observed result | Evidence |
|---|---|---|
| Discovery | Real deliberately defective Python/JS Git repositories reproduced failures, validated findings and created local tickets | `GE-CED6601A.json`, `GE-B0E4AC26.json` |
| Dispatch | Actual scheduler leases and OpenCode assignments at recorded nodes | `live-results.json` |
| Parallel work | Nine real Qwen calls, four cross-ticket call overlaps | `live-results.json` |
| Retry loop | Explicit scripted inference double forces first review failure; actual repair, test/build fan-out and second review run | `injected-repair.json` |
| Waiting queue | Dependency and same-repository/model-capacity blocking keep eligible independent work dispatchable | Python scheduler/recovery tests; browser dependency check |
| Synchronisation | Ticket journey, node → ticket selection and a marker transition correlated to a persisted event ID | `browser-tests.json` |
| Persistence | Browser reload retains edited triage; live lock survives recovery; killed worker is fenced | Browser and recovery tests |
| Real inference | 4B investigates/reviews; 27B plans/implements through OpenCode 2.0.32 | Three ticket receipts and worker artifacts |
| Safety | Both real-model fixture PRs are local and waiting for approval, with no merge receipt | `live-snapshot.json` |
| General_AI_Agent | Real read-only 4B investigation of commit `a5488e73`; verified source references, unconfirmed observations; source status/diff hashes unchanged | `GE-B3AA13AB.json`, `live-results.json` |

All named files are in [`evidence/phase2`](../evidence/phase2).
Live-model tickets are `GE-B3AA13AB`, `GE-CED6601A`, `GE-B0E4AC26`.
The separate injection ticket is `GE-78065EAE`, labelled `failure-injection` and
`scripted-model-responses`. Its model responses are test doubles; its Git/test/
repair/parallel-child events are real. The older blocked General run remains
visible rather than being silently relabelled as recovered.

There were **no live GitHub writes in Phase 2**. Earlier authorised public fixture
operations and the private rules-API 403 are documented in
[the original demonstration](DEMONSTRATION.md). They are not new UI test results.

Final verification: **92 Python tests, eight control-centre browser checks, twelve
legacy browser checks, and ten marker-motion checks passed**. Five accessibility
views had zero violations; the dependency audit reported zero vulnerabilities.
The wheel contains all 18 web assets. Receipts are in `verification.json`,
`browser-tests.json`, `python-tests.txt`, `npm-audit.json` and `package-check.json`.

## Reproduce verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd tests/browser
npm ci
npm run test:centre
node motion.cjs
npm test
# Only when rebuilding the checked-in graph renderer:
npm run build:graph
```

Browser tests use an isolated headless Edge profile and the actual HTTP/SQLite
backend. The browser fixture explicitly uses scripted inference, with actual Git,
frozen tests, local publication and an injected review failure. Accessibility
audits run on dark board/network/monitor, light board and mobile. No console
warning or error is ignored. `UI_TEST_BROWSER=chromium` can select an installed
Chromium channel; the exercised version is recorded in browser receipts.

`evidence/phase2/start_live_demo.py` runs actual inference and trusted disposable
fixtures against the local centre. It does not publish to GitHub. Do not confuse
that script with `run_injected_repair.py`, which is explicitly a test-double demo.
The scripts reference development tests and are not production CLI commands.

Before unattended production use: provision hostile-code isolation and dependency
setup, authenticated approval/service management, artifact retention/backups,
larger workload/volume benchmarks, and distributed repository coordination if
multiple controller homes/hosts can target the same repository. General's full
assisted test/build workflow still needs its committed dependencies/interpreter
provisioned in the isolated snapshot. It was not claimed as a completed repair.
