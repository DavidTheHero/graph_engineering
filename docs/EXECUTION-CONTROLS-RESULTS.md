# Execution controls: observed results, 19 October 2026

Implemented accepted-ticket development using the existing engine: five new
nodes, immutable generated regressions, exact source-fragment changes, persistent
proposal recovery, opt-in repository autonomy and a windowless Windows scheduled
controller. The board now distinguishes Cancelled from Blocked, and the UI exposes
the repository policy. The executable graph contains 37 node contracts.

## Actual local-model workflow

Fresh disposable Python repository ticket **GE-B7645158**, run
`a17b622fd52c47eb`, was discovered/investigated through real OpenCode sessions.
After supervisory acceptance of revision 2, policy dispatch started development.
Qwen 27B authored a regression; it failed twice against the pinned defective source.
Qwen implemented the percentage formula in an isolated branch. The complete suite,
frozen new regression and independent diff review passed. The observed tested head
is `f072bb52da8aef568a2245d84a2a4a663b48a9dd`.

It created **local simulated issue 1 and PR 1**, then stopped at `approval_gate`
with `allowed: false` and reason `human approval missing or stale`. No GitHub write
or merge occurred during this change. Existing live GitHub demonstrations are
documented separately and are not presented as new operations.

Twelve recorded OpenCode model calls were observed: three 4B calls (classification
and two regression reviews) and nine 27B calls (investigation, authoring, escalated
review, planning, implementation and final review). These are JSON-response
receipts, not twelve successful engineering decisions. In particular, 4B twice
rejected a correct regression for failing against the defective original source.
The controller blocked rather than accepting contradictory `approve` plus
critical findings. A clarified prompt and fresh 27B review completed after one
supervisory retry. Later review rejections now route to the deeper session within
the bounded review path. The final review also used 27B through measured context
budget routing, in an independent OpenCode session.

[Full ticket evidence](../evidence/execution-controls/fresh-demo-outcome.json),
[concise verification receipts](../evidence/execution-controls/live-summary.json).

## Failures retained

- Original seeded ticket **GE-81DF53DF** exhausted its retry budget after invalid
  model-generated tests. Those proposals were rejected; the ticket was not reset
  to hide failure. A new disposable repository exercised the corrected implementation.
- The 4B server rejected the 6000-character bounded source schema with HTTP 400,
  `failed to parse grammar`. Both models subsequently returned executable test
  proposals through OpenCode with the compatible transport shape. Full controller
  schema bounds remain enforced.
- A 4B review of the dispatcher produced an invalid/truncated response and claims
  that ignored SQLite's write transaction. A 27B review also supplied findings
  that required human engineering triage. See
  [review triage](../evidence/execution-controls/review-triage.json). Model review
  output alone is not a release gate.
- General_AI_Agent ticket **GE-2580F847** was dispatched automatically, installed
  277 locked npm packages in its isolated baseline, then failed setup: the full
  registered suite exceeded the 120-second limit, reported missing Python and a
  semantic-memory failure. It remains **Blocked**; there is no claimed token fix
  or PR. Python environment provisioning is not implemented. These failures must
  be resolved before the complete suite can serve as a trustworthy gate.
- Cancelled ticket **GE-DD0C3FE0** remains cancelled and is not dispatched.

General_AI_Agent's source checkout was preserved: before/after HEAD, status and
binary-diff hashes match. See
[source comparison](../evidence/execution-controls/source-comparison.json).

## Verification

`scripts/verify_phase3.py --output evidence/execution-controls/checks` completed:

| Check | Actual result |
|---|---|
| Python unit/integration/failure-injection tests | 125 passed |
| Python compilation | Passed |
| Intake browser | 9 checks passed, including persisted autonomy and cancellation |
| Centre browser | 8 checks passed |
| Legacy dashboard browser | Process exited 0 |
| Event motion tests | Process exited 0 |
| Browser accessibility audits | No reported violations in recorded tested views |
| Wheel build | Passed; Windows supervisor module included |

Backend/browser suites use explicitly labelled inference doubles with real
Git/HTTP/SQLite/test processes. They are separate from the real Qwen run above.
The new failure tests include empty/passing regressions, cancelled admission,
revoked policies, concurrent actions, partial dependency installation, interrupted
source application and redacted cached-source rejection.

Actual scheduled-task stop/restart was exercised. A first restart exposed an
orphan dispatch process; the Windows job lifetime fix removed all observed owned
processes and restored a fresh scheduler heartbeat. A console termination was
also recorded; the installed action now uses `pythonw.exe`. This remains a
logged-in-user deployment rather than a boot-time service.

[Check output](../evidence/execution-controls/checks/unittest.json),
[restart receipt](../evidence/execution-controls/supervised-restart.json),
[live dashboard capture](../evidence/execution-controls/live-autonomy-policy.png).

## Scope

Autonomy is enabled for General_AI_Agent and the controlled local fixtures only.
Its configured backend is local, so publication means labelled previews. New
repositories retain all permissions off. Acceptance of an enhancement, missing
requirements, unsafe/environment failures, exhausted retries and exact-commit
merge approval can still require supervision. OS containment for hostile code,
authenticated approvers and broad workload evaluation remain production work.
