# Plain-language ticket investigation

Phase 3 extends the existing controller, SQLite store, queue scheduler, OpenCode
workers and live board/network. It does not run a second orchestration engine.
See [ADR 003](ADR-003-natural-language-intake.md), the executable
[node contracts](node-contracts.json), and [model artifact schemas](ticket-artifact-schemas.json).

## Use the dashboard

```powershell
cd D:\git_code\graph_engineering
.\.venv\Scripts\graph-engineering.exe --home .runtime/general-ai-agent centre --port 8771 --workers 2
```

Open http://127.0.0.1:8771. If that controller is already running, open its existing
dashboard; do not launch another listener on the same port.

1. Choose a registered repository, or use **Connect repository**. Registration
   defaults to read-only operation.
2. Select **+ Create Ticket**, describe the problem, and select **Create & Investigate**.
   Repository and problem description are the only required inputs.
3. The original report and ticket ID are committed before Git inspection or inference.
   Look for its lifecycle badge on the board; select it to inspect the report.
4. **View ticket journey** follows actual persisted transitions, model assignments,
   additional searches and validation feedback. The console and node inspector
   use the same run state as the board. Replay uses recorded events.
5. Compare **Original User Description** with **AI-Enhanced Ticket**. Expand the
   actual source evidence, reproduction receipt, model requests/responses and revision history.
6. **Provide information** or **Investigate further** queues another bounded
   investigation. Missing evidence can leave the ticket in **Needs Information**.
   During accepted development, **Provide information** is available only on a
   blocked ticket. It appends troubleshooting context without changing the accepted
   revision, retry counters, pause state, or execution position. Select **Retry with
   backoff** separately when the ticket is ready to run again. A rejected regression
   review returns to regression authoring with the saved review findings and context.
7. **Edit enhancement** appends an operator revision. **Accept enhancement** accepts
   that exact revision; editing or reinvestigation clears its acceptance.
8. Choose **Autonomy policy** for the selected repository to permit development
   of accepted revisions, optional publication, and locked npm dependency setup.
   With development enabled, accepting an enhancement dispatches it automatically.
   With the policy disabled, **Send to Development Queue** holds the ticket until
   **Start development** explicitly authorises isolated execution.
9. **Publish GitHub issue** requires the current accepted revision and a named
   publication approval. A local backend offers **Create local issue simulation**
   instead. The issue link appears only after the backend observes the write.

Optional fields include logs, expected behavior, context, priority override and
a related ticket. Image/file attachment upload is not implemented. Reproduction
is optional and requires both a previously trusted repository configuration and
the explicit form checkbox. It runs the registered argv in a pinned controller-owned
snapshot with a scrubbed environment. Ordinary intake does not execute repository code.

## Execution and evidence rules

The added processing nodes are manual intake, triage, duplicate review, repository
search, problem investigation, enhancement, reproduction, finding validation,
finalisation, publication and development admission. Conditional edges return from
investigation to search and from invalid references to investigation. Human actions
return waiting tickets to search or route accepted revisions to publication/admission.
Development admission conditionally enters isolated development under the current
repository policy or an explicit revision-bound start. The controller adds setup,
regression authoring, independent regression review, deterministic regression
validation and delivery nodes to the existing graph. The implementation agent
receives source edits only; the new regression is frozen before implementation.
It must fail with an assertion twice at the original commit and pass alongside
the registered suite after the fix. Non-executable tests, passing regressions,
environment errors and timeouts block implementation. Invalid proposals get a
bounded regeneration loop. See [autonomous operation](AUTONOMOUS-DEVELOPMENT.md).

Qwen 4B classifies a bounded report and suggests search terms. Deterministic policy
sets priority (security P1, otherwise P2 by default), preserving an explicit override.
Qwen 27B receives selected committed source excerpts and proposes technical content.
It cannot execute tools or change files. The controller performs literal Git search,
blob reads and history retrieval. Files explicitly named in clarification take priority.

Search selects at most ten regular text blobs, bounded by size and an approximately
19,000-character excerpt budget. Symlinks and known secret-like paths are excluded.
4B receives at most 1,400 report characters plus bounded expected behavior. The 27B
request also bounds the report, optional context, recent clarifications and prior
reproduction output; full originals remain in the persistent report. The existing
worker independently rejects requests exceeding its conservative context budget.

Validation checks every structured file/line/quote against captured source at the
pinned commit. A curly-quote substitution may be corrected only at that exact
captured line; the original proposal, captured text and a new validator-authored
revision are retained. Missing files, wrong lines and invented text remain invalid.
Source provenance does not prove runtime causality. Root causes are displayed as
hypotheses, including when a reproduction has failed. Free-form model observations
and proposed solutions still require operator judgment.

Two search rounds and one invalid-reference feedback round are permitted per
generation. Every logical node visit has two failure attempts. Up to three explicit
follow-up generations are available. Model outages block the persistent ticket;
the existing manual retry/backoff and dead-letter policies apply. Duplicate wording
within the same registered repository produces a review hold; it never silently
deletes a report. Semantic deduplication against arbitrary existing GitHub issues
is not implemented.

The dashboard reports three separate execution limits: node attempts (normally
two for the current step), ticket retries (three), and implementation repair loops
(two). These are loop-safety counters, not token, credit, or spending budgets.
`Attempt budget exhausted` means the current graph node used all of its attempts.
The ticket remains recoverable while its ticket retry count is below its limit.
The queue reason includes the independent review or failure that caused the stop.

## API and persistence

All writes use the existing same-origin loopback session: obtain `/api/session`,
send its token as `X-GE-CSRF`, and include a unique `Idempotency-Key` (8–100 letters,
digits, underscores or hyphens). HTTP writes return 200 on admission, 409 on a
validation/policy conflict, 403 for an invalid operator session and 404 for an
unknown identity. An accepted write is not a claim that its asynchronous work completed.

| Endpoint | Contract |
|---|---|
| `POST /api/centre/create-ticket` | `repository_id`, `description`; optional `logs`, `expected`, `context`, `priority`, `related_ticket`, `reproduce`, `trust_execution` |
| `POST /api/centre/intake-action` | `ticket_id`, exact integer `revision` (0 before enhancement), named `actor`, `action`; action-specific fields below |
| `POST /api/centre/repository-policy` | `repository_id`, named `actor`, boolean `development`, `publish`, `prepare_dependencies`; all default false |
| `GET /api/centre/ticket?id=GE-...` | Original, revisions, investigation evidence, timeline, actual worker artifacts |
| `GET /api/centre` | Shared authoritative board, queues, topology and execution projection |
| `GET /api/centre/events` | Existing persisted SSE stream; resumable event IDs |

Actions are `accept`, `edit_enhancement` (`changes`), `clarify`/`reinvestigate`
(`text`), `reject_enhancement`, `mark_duplicate` (`related_ticket`),
`send_development`, `start_development` (`trust_execution: true`),
`development_information` (non-empty `text`, at most 4,000 characters and five
stored notes on a blocked accepted-development ticket),
`publish_development` (`approve_publication: true`), and `publish` (`approve_publication: true`; also
`simulation: true` for the local backend). Reusing a key with different content
is rejected. Active worker leases prevent concurrent editing/review.

`ticket_reports` stores the immutable original and optional context;
`ticket_revisions` stores append-only numbered content/evidence revisions. Existing
runs, events, execution leases, operator command receipts and publication effects
remain authoritative. A controller/browser restart preserves them. The publication
worker rechecks the current configured backend/slug and `allow_issue_publication`
before writing. Published revisions record the observed outcome separately from approval intent. Later accepted edits update the same issue title/body while preserving its reconciliation marker and browser URL; repeating a completed revision does not schedule another write. Uncertain creation is reconciled through the existing effect ledger.

This remains a trusted single-operator console. Named operators are audit labels,
not authenticated human identities. Do not expose it on a network. Original reports
are preserved verbatim in SQLite and may contain sensitive text; restrict storage
access. Existing redaction is applied to displayed/exported evidence, not a promise
that arbitrary secrets are detected. OS isolation for hostile repository code,
authenticated approval services and distributed coordination remain production work.

## Verify and export

```powershell
# Install browser test tools once; requires installed Edge or configured Chromium.
cd tests/browser
npm ci
cd ../..
.\.venv\Scripts\python.exe scripts/verify_phase3.py
.\.venv\Scripts\python.exe scripts/export_ticket.py GE-DD0C3FE0 --home .runtime/general-ai-agent --output .runtime/exported-ticket.json
```

The verification command records actual subprocess output in `evidence/phase3`.
Browser fixtures label scripted inference and use real HTTP/Git/SQLite/scheduler
operations. They are separate from the real local-model demonstrations described
in [the Phase 3 results](PHASE3-RESULTS.md). Exported evidence can contain repository
source; review it before sharing.
