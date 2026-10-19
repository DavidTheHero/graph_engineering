# Blocked-ticket recovery evidence

Observed on 19 October 2026 against the running controller at
`http://127.0.0.1:8771`.

Ticket `GE-2CD12338` was initially blocked at `regression_authoring` after using two of two
node attempts. It had used zero of three ticket retries and zero of two implementation
repair loops. The independent review rejected the generated regression because it
checked only for method existence while the deletion and permission assertions were
commented out. `live-ticket.json` retains that read-only controller projection.

After the operator saved real troubleshooting information and issued retries, the
ticket reached `regression_review` with two of three ticket retries used. The next
generated test manipulated database rows directly and the reviewer rejected it.
`live-ticket-current.json` records the current counters and the browser-observed,
enabled recovery control. The corrected retry edge returns this state to regression
authoring instead of spending the final retry on the unchanged test.

The recovery UI now explains all three limits and enables **Provide information**
for blocked accepted-development tickets. Saving information is idempotent, preserves
the accepted revision, counters, pause state, and node, and does not restart work.
An explicit **Retry with backoff** restarts the blocked step within the remaining
ticket retry limit.

Verification:

- `final-checks/unittest.json`: 135 Python tests passed.
- `final-checks/intake-browser.json`: the complete intake browser scenario passed,
  including recovery controls, persistence, accessibility, cancellation, and the
  stale-action safety regression. The same directory records successful compilation,
  centre, legacy-dashboard, and reduced-motion checks.
- `checks/intake-browser.json` records the earlier failed browser run that exposed
  stale previous-ticket actions during asynchronous detail loading. The final run
  proves those controls are cleared before a new ticket becomes actionable.

No retry was issued and no troubleshooting note was added to the real user ticket.
It remains blocked for deliberate operator recovery.
