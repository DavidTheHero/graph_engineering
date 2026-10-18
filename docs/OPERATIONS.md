# Operational contract and remaining production work

## Trust boundaries

Repository files, logs, model output, issues, comments, docs and diffs are data.
The operator-owned JSON configuration is authority. Worker profiles disable project
configuration, Claude imports, global integrations, title generation and every tool.
Proposed complete-file edits pass strict JSON and canonical path checks. Only planned
tracked source files may change; tests and policy files are frozen. Each worker has
a new OpenCode session/profile/database, never the target repository as its cwd.

The controller holds GitHub authority. Tests receive a minimal environment without
controller tokens, user Git configuration or model keys. Controller Git preserves
line-ending configuration, disables hooks, filesystem monitors and external protocols.
Privileged pushes verify the GitHub slug, reject URL rewrites and use a fixed gh
credential helper against an explicit HTTPS URL, ignoring origin.pushurl.

These controls do not isolate arbitrary code from same-user files, process inspection,
network access, native extensions or detached children. Trust local execution only for
known repositories. No Docker daemon was available, and no sandbox integration was
invented. A VM/isolated runner under a separate credential-free identity is required
before enabling hostile repositories. Model serving authentication was not enforced
in the observed deployment. Restrict LAN access and add an authenticated gateway.

Approval actors are local CLI audit labels, not cryptographically authenticated users.
Protect the controller directory/OS account. A production approval service must bind
an authenticated identity to the exact head SHA and evidence digest. SQLite files,
artifacts and worker records contain source and should inherit restrictive OS ACLs.

## Execution, retry and recovery

Every graph node exposes JSON input/output contracts, responsibility, permissions,
pre/postconditions, success/failure edges, timeout, attempt count, evidence and
escalation in `node-contracts.json` and `graph` CLI output. Default node deadlines
are 120 seconds and attempts are limited to two. The deadline propagates to model,
test, Git and GitHub subprocess boundaries. File edits and bounded local bookkeeping
are synchronous. A controller kill can interrupt between boundaries; it resumes the
persisted node, not an assumed Python stack frame.

The next node and corresponding audit transition commit in one SQLite transaction.
An external-effect intent commits before the request. Completed effects return their
stored receipts. An intent with no receipt can **only** call a read-only reconciliation
handler for issue/PR/comment creation. Missing, duplicate or conflicting objects block
the run. HTTP no-cache headers reduce stale reads but are not a correctness guarantee.
A negative search result never authorises an uncertain create. Repeating a merge or
setting an issue state is only allowed through operations with independently checked
idempotent server/local semantics.

The first live crash test demonstrated why marker search alone was insufficient:
it produced issues #3 and #4 in the public fixture. Both are closed and retained as
evidence. The corrected test killed the child with exit 23 after creating #5 but before
recording its receipt, reopened SQLite, reconciled #5 and observed one matching issue.
No duplicate was created in that corrected test. We do not claim distributed exactly-once
GitHub operations; a single shared controller home and one mutating controller per
repository are deployment requirements. Concurrent independent homes need a shared
database/lease and fencing design first.

After a terminal escalation, investigate and repair the cause. `resume` resets only
the current node's attempt budget, clears a stop request, and records the reset.
After two invalid finding proposals or exhausting repair attempts the run stops.
Do not edit approval/verification rows to force completion. Controller restarts use
OS locks that are released on process death; another controller cannot take over a
live owner just because a timestamp expired.

## GitHub and CI

Live initialization records Git and adapter information; live publication requires
an explicit owner/repository slug, matching origin and push access. Full branch-policy
verification is performed at PR/merge time, when actual head checks/reviews exist.
Required reviews cannot be satisfied by the implementing account's model comment.
Controller regression statuses do not replace external CI or required GitHub App checks.
GitHub accepts neutral/skipped check conclusions under its rules; the independently
mandatory controller test process must actually return success with a nonempty suite.

Private fixture policy reads were blocked by GitHub's plan restriction (HTTP 403).
The framework left that PR open. Public fixture main requires `regression`, strict
up-to-date checks and enforced administrator policy. The human-approved normal merge
was observed by REST; no administrative bypass or protection removal was used.

Unknown rules, merge queues, deployment gates, required workflows and scanning gates
currently stop for manual handling. Base drift stops for rebase/retest; automatic
rebasing is not implemented. A remote merge preserves the original local checkout;
the operator must fetch/fast-forward before subsequent runs.

## Storage and deployment

Back up the whole controller home while no controller is mutating it, or use SQLite's
online backup API for the database plus an artifact snapshot. Do not copy only the
main SQLite file while WAL writes are active. Schema version 1 is checked on open;
unknown versions are refused. Retention/deletion is manual; worktrees are retained
for inspection. Remove only known finished worktrees with Git's own `worktree remove`
after retaining their receipts. Never recursively delete a computed target repository.

`serve` is a restartable foreground process. A service manager should restart it after
failure and capture its JSON output/stderr; authentication, installation as a Windows
service and alert delivery are intentionally not installed automatically. `enqueue`
adds ready runs. `serve` processes ready/interrupted runs and exactly approved waiting
runs, never escalated/blocked/scanned runs. Pending GitHub checks require explicit
resume in this release. A stopped running worker/test is terminated; Git/API operations
finish or hit their bounded timeout before cancellation proceeds.

For multi-host use, preserve stable side-effect keys while migrating SQLite tables to
PostgreSQL with unique constraints, transactional claims/outbox, owner fencing and
credential-isolated workers. LangGraph can host the same handlers/checkpoints when
fan-out/subgraphs earn the added dependency; GitHub reconciliation remains necessary.

## Measured scope

Windows was exercised; Linux/macOS code paths are not claimed verified. Only two small
seeded defects and their fixed negative scans inform routing. Existing frozen tests
serve as regression tests; generating new tests and proving a new test against the
old implementation is future work. Compatible repositories must be clean, have a
detected Python/JavaScript stack, runnable pre-provisioned tests, and allow source-only
fixes. Adapter command overrides are operator input, not model input.

There is no unattended production readiness claim. Mandatory next steps are an OS
execution boundary, authenticated approval/control APIs, storage and secret controls,
broader independent real-world evaluations, operational alerting and explicit support
for the target organisation's rules. Source-only repair and known rule support should
remain the default while that evidence is gathered.
