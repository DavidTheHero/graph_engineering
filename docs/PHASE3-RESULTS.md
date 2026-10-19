# Phase 3 observed results — 19 October 2026

The plain-language intake form, persistent API, actual Qwen investigation, shared
board/network projection, operator review and approved issue publication were
executed. General_AI_Agent was inspected at committed HEAD without changing its
working tree. No General repair or merge is claimed.

## Real local inference and repository evidence

| Ticket | Repository | Observed result |
|---|---|---|
| `GE-DD0C3FE0` | General_AI_Agent | Created through the live browser form with only “The AI agent sometimes freezes when using tools.” Four source references validated, including `backend/agent-loop.js`. Enhanced report, proposed tests and questions persisted. Final **Needs Information**; no runtime code executed or freeze reproduced. |
| `GE-81DF53DF` | Seeded Python invoice fixture | Both models investigated the real committed defect. An explicitly authorised isolated regression failed with `19990 != 18000`; three source references validated. Accepted revision 3 and sent to the held development queue through the real UI. No implementation branch was created. |
| `GE-FD7232CD` | Authorised public invoice fixture | Both models investigated existing percentage validation. Five references validated. The report explicitly says existing validation is present and runtime code was not executed. Accepted revision 6 was published as real GitHub issue [#6](https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/issues/6). |

The three runs recorded **3 actual 4B classification calls** (mean 10.141 seconds)
and **15 actual 27B investigation calls** (mean 27.916 seconds), all through
OpenCode 2.0.32. All 18 returned valid structured responses; that does not mean
their references or conclusions were all correct. Invalid lines, quote substitutions,
clarification and unsuccessful controller transitions are retained in the audit trail.
Model catalogs were queried again and returned the configured IDs. Workers attempted
zero model-side tools; deterministic Git/test operations are separately recorded.

Recorded invocation intervals show 4B and 27B working on separate tickets concurrently
(one observed overlap was 8.515 seconds). Same-model requests remain serialised by
the existing scheduler and model locks within a controller home.

An additional independent 27B source-review session ran through OpenCode in 65.047
seconds. It produced several inaccurate critical findings, including claims about
missing methods and an inverted reading of the acceptance check. Codex assessed
each against source, tests and observed operations. The raw review and
[assessment](../evidence/phase3/review-assessment.json) are retained; its judgment
was not used as automatic approval.

## Publication, persistence and recovery

The public fixture operation was a live GitHub issue creation, not a local backend
simulation. Named supervisory approval was recorded against the accepted revision.
The issue URL/number and completed write effect persisted. Repeating the same accept
and publication commands returned their original receipts; a subsequent real GitHub
query found exactly one issue carrying this ticket's marker: issue 6. A subsequent
real GET/PATCH synchronised the same approved revision through the updated adapter;
the observed browser URL and published revision persisted. Repeating that completed
revision with a new command key scheduled no additional effect. See the
[synchronisation receipt](../evidence/phase3/live-github-synchronisation.json). No PR or merge
was performed in Phase 3. Earlier Phase 1 merge evidence remains separately documented.

The controller was restarted with all three tickets at checkpoints and no active
leases. The same IDs, source commits, revisions, waiting states and issue link
survived. Browser tests independently close/reload the page and recover authoritative
content. A model-outage injection exercises two failed attempts, preserved original,
explicit retry and successful recovery without another ticket.

The first live General investigation exposed a retry-budget bug: successful search
visits consumed the same budget as failures. Per-generation/search/validation attempt
keys now distinguish logical visits. A regression test executes additional search,
invalid-reference feedback and successful completion. Publication tests also exercise four accepted revisions updating one issue, repeated completed requests, marker retention and the observed GitHub browser URL. The original failure remains
in the real ticket's timeline. A separate test protects the final allowed manual
retry from premature dead-letter classification.

Models also returned curly quotation marks in source quotes. The validator now
retains the original AI revision and appends an auditable correction only when
the exact claimed captured line matches after typographic-quote normalization.
It does not relocate lines or accept invented content.

## Executed verification

| Check | Actual result | Receipt |
|---|---|---|
| Python unit/integration/failure injection | **112 passed**, 46.461 seconds | [unittest.json](../evidence/phase3/unittest.json) |
| Intake browser acceptance | **7 checks passed**, zero Axe violations in form, enhanced ticket and mobile board | [browser-tests.json](../evidence/phase3/browser-tests.json) |
| Existing control-centre browser acceptance | **8 checks passed**; accessibility, replay, real injected retry, admission and synchronization | [centre receipt](../evidence/phase3/centre-browser/browser-tests.json) |
| Existing evidence dashboard browser acceptance | **12 checks passed**, including inert malicious HTML/links and outages | [legacy receipt](../evidence/phase3/legacy-browser/browser-verification.json) |
| Ticket motion mathematics | **10 checks passed** | [motion.json](../evidence/phase3/motion.json) |
| Python compilation | Passed | [compile.json](../evidence/phase3/compile.json) |
| Built wheel/import smoke | Passed; 32 graph nodes and bundled intake asset | [wheel-smoke.json](../evidence/phase3/wheel-smoke.json) |
| Acceptance-gate mutation | Removing the accepted-revision requirement in an isolated package copy caused the intended regression test to fail | [mutation-check.json](../evidence/phase3/mutation-check.json) |
| General preservation | HEAD, status hash and diff hash match before investigation | [general-after.json](../evidence/phase3/general-after.json) |

Browser fixtures explicitly use scripted inference with real backend execution.
They do not prove model quality. The three live runs above use actual Qwen responses
and are separately exported with their requests, responses, events and revisions.
Server outage and malicious-instruction cases are controlled failure-injection tests;
the shared inference servers were not deliberately disconnected.

## Components and retained evidence

`report_store.py` adds atomic immutable intake and revision storage;
`report_commands.py` adds revision-bound operator commands; `investigation.py`
adds bounded retrieval and eleven graph handlers. Workflow, scheduler, tickets,
status policy, control API and shared projections reuse existing persistence and
execution. `centre-intake.js` adds the form, professional comparison and review
controls; existing board/network/dialog modules expose the new lifecycle and nodes.
The HTTP server serves the packaged asset and accepts bounded Unicode intake.

Tests are in `tests/test_intake.py`, the existing HTTP suite and
`tests/browser/intake.cjs` with an explicitly labelled fixture. Browser harnesses
accept a separate output directory so new checks preserve historical receipts.
`scripts/verify_phase3.py` reruns verification; `scripts/export_ticket.py` exports
an auditable ticket. Setup and API details are in the [operating guide](TICKET-INVESTIGATION.md).

Raw/current receipts are under `evidence/phase3/`: `live-demonstration.json`,
`general-agent-ticket.json`, `seeded-defect-ticket.json`, `public-fixture-ticket.json`,
`live-github-issue.json`, `publication-approval.json`, model review/assessment,
test output and real-controller screenshots. These exports contain repository
source and must be reviewed before external sharing.

Remaining integration limits: development admission is held rather than an automatic
bridge from a natural-language report into implementation; image attachments and
semantic remote issue deduplication are absent. Operator identity is a local audit
label. Hostile-code containment, authentication, distributed locking and larger
workload/volume evaluation remain necessary before unattended production use.
