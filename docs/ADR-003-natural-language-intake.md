# ADR 003: natural-language intake in the existing execution graph

Use the current run cursor, scheduler, worker sessions, event stream and effect
ledger. Add an immutable report table and append-only ticket revisions; do not
create another ticket engine. Intake commits its run, ticket, original report and
idempotency receipt in one SQLite transaction before any Git/model work.

Investigation reads committed Git blobs using controller-owned, bounded search
operations. Models receive selected line-numbered excerpts and propose structured
artifacts; they cannot execute repository commands. File/line/quote validation
proves source provenance, not runtime behavior or causality. Root causes remain
hypotheses. Optional reproduction uses only the registered test argv in an
explicitly authorised controller-owned snapshot with a scrubbed environment.

4B performs bounded classification. 27B investigates and proposes technical
content. Deterministic nodes handle duplicate suggestions, retrieval, enhancement
formatting, reference checks, finalisation and publication. Additional-search and
invalid-reference loops have independent budgets. Missing evidence waits for user
input. A successful model response never independently confirms a bug.

Accept, edit, clarify, reinvestigate, reject, duplicate, development admission and
publication are revision-bound, idempotent operator commands. Accepting content
does not authorise execution. GitHub publication requires the repository's
configured GitHub backend plus explicit named approval; the existing effect
ledger reconciles uncertain issue creation instead of repeating writes.

Retain the current Cytoscape/SSE interface and expand its executable topology.
Show intake lifecycle alongside the existing nine delivery columns. Reuse the
same shared snapshot for board, graph and ticket details.
