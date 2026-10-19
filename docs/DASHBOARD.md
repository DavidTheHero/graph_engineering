# Workflow dashboard

## Design contract

The screen answers three questions: where did a run get to, why did it stop,
and what evidence supports its result? Select a run, then select a graph step
to inspect its observed transitions and execution contract. Tests, actual diffs,
model receipts and the chronological audit trail remain one click away.

The layout uses a compact run navigator, four workflow lanes, and a step inspector.
Dark ink, a warm neutral canvas and teal accents distinguish structure from status.
Text and symbols accompany every status. The graph scrolls within its own region
on narrow screens; navigation and details stack. Native buttons, visible focus,
labelled filters, reduced motion and explicit empty/error states are required.
Avoid decorative metrics, invented progress, simulated live activity and external
fonts or CDNs. Recorded runs are labelled as snapshots; a local publication backend
is always labelled as simulated GitHub operations.

References studied before implementation:

- [GitHub Actions visualization](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-the-visualization-graph):
  dependency lines, status symbols and selecting a job to inspect its logs.
- [LangSmith Studio graph mode](https://docs.langchain.com/langsmith/studio):
  keep architecture, traversed nodes and intermediate evidence together.

## Runtime boundary

The dashboard uses the Python standard library and bundled HTML/CSS/JavaScript.
It binds only to `127.0.0.1`. It reads the selected controller's SQLite database
using [SQLite read-only URI mode](https://docs.python.org/3.10/library/sqlite3.html#how-to-work-with-sqlite-uris)
and a consistent transaction, without creating a controller or executing workers.
No HTTP endpoint can approve, stop, resume, merge, execute shell commands or read
arbitrary files. Host/origin checks, a restrictive content security policy,
secret redaction and text-only rendering protect the local read surface.

The source checkout also offers archived demonstrations from `evidence/final`.
These are observed historical receipts, not current GitHub checks or model health.
Installed wheels work with live state without requiring the demonstration archive.

## Run

```powershell
.venv\Scripts\graph-engineering.exe ui
# Point at another controller; --home comes before the command:
.venv\Scripts\graph-engineering.exe --home D:\controllers\example ui --port 8765
```

Open `http://127.0.0.1:8765`. The dashboard refreshes live data every three seconds
while its tab is visible. Use **Refresh** for an immediate read. Stop the web server
with Ctrl+C; workflow execution is managed separately by `serve` or `execute`.

## Verification

Backend tests use temporary real SQLite stores and real HTTP requests. They cover
read-only startup, live checkpoint visibility, recorded/live source boundaries,
credential redaction, failed/omitted/recovered steps, both history limits, missing
runs, invalid data, Host/Origin rejection, and denied mutation requests.

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
npm.cmd ci --prefix tests/browser
npm.cmd test --prefix tests/browser
```

Browser tests use an isolated, headless installed Edge via Playwright Core. Node
and the locked browser dev dependencies are required only for these tests. Set
`UI_TEST_PYTHON` to an installed project Python and `UI_TEST_BROWSER` to a supported
Playwright Chromium channel if needed. The test launches its own ephemeral local
server and temporary controller. It reads actual archived demonstrations; explicit
malicious-input/outage/race tests intercept API responses. These injected scenarios
are tests, not evidence that a new live GitHub workflow ran.

On 19 October 2026, all 70 Python tests passed, the distributable wheel included
all seven web assets, and an intentionally weakened Host guard was caught by its
HTTP regression test. Edge 155 passed 12 browser scenarios, including graph step
selection, recovery edges, shareable URLs, actual diffs and receipts, keyboard
navigation, live checkpoint polling, policy failures, filtering/empty states,
out-of-order responses, inert HTML/JavaScript URLs, and outage recovery. Responsive
checks passed at 320, 768, 1024, 1440 and 1600 pixels. Axe reported zero WCAG A/AA
violations in the desktop graph, mobile graph, evidence and activity views.
Automated accessibility checks do not replace manual assistive-technology testing.

See [browser results](../evidence/ui/browser-verification.json), screenshots beside
that receipt, and [Python/package checks](../evidence/ui/verification.json).

## Local model contribution

Both configured model identifiers were rediscovered from their real `/models`
responses, then invoked through the engine's isolated OpenCode 2.0.32 workers.
Qwen 3.8 27B proposed the activity-history repair. An overbroad proposal was
rejected; a narrower proposal was accepted after the supervisor normalized literal
key quoting and discarded a no-op edit. The reproduction failed before and passed
after the fix. Qwen 3.5 4B reviewed the small DOM/link rendering module within its
context budget. Qwen 27B independently reviewed actual Git new-file diffs and found
the second history bug, which received its own failing-then-passing regression.

These were scoped implementation/review worker calls, not a complete issue-to-PR
execution against this project. No new GitHub issue, PR or merge was performed for
the dashboard. [Worker receipts](../evidence/ui/qwen-development.json) and
[independent diff review](../evidence/ui/a-independent-review.json) retain the
observed outcomes, sessions and inference times.

The independent review's history finding was confirmed and fixed by streaming
all transitions to calculate graph state, while retaining only 2,000 displayed
events. A newer-request guard also prevents stale reads overwriting fresh data.
The review's speculative HTML warning was checked against all rendering modules
and browser injection tests: untrusted values use `textContent`, never `innerHTML`.
Host checks remain necessary for the loopback access boundary; connection closure
already rolls back failed SQLite read transactions. Model approval does not stand
in for these deterministic checks.

## Limits

The run navigator shows the latest 200 live runs. Activity displays the first
2,000 events with an explicit truncation notice; graph state is calculated from
the complete history. Individual text fields are capped at 24,000 characters.
Large histories require a streamed database scan per detail refresh. This is a
single-user local UI, not an authenticated multi-user service or an execution
control panel. Archived snapshots require the source checkout's evidence folder;
an installed wheel still visualizes its selected live controller.
