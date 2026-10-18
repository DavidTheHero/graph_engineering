# Executable acceptance contract

This implements the user's supplied specification, in incremental executable slices.
No additional spec approval is required by the user's explicit instruction to proceed.

| Module | Responsibility | Dependencies |
|---|---|---|
| evidence | adapters, immutable source/test/Git evidence, validated findings | Git, configured execution trust |
| workers | discovery/probing, V2 configuration, bounded OpenCode JSON tasks | local models, installed OpenCode |
| workflow | typed nodes, checkpoints, locks, retries, cancellation, repair | evidence, workers |
| publication | idempotent issue/PR lifecycle and head-bound merge gates | workflow, gh or explicit local backend |
| evaluation | two real disposable Git repositories, baseline, failure injection | all modules |

Build order: evidence/contracts, workers, workflow to local PR, independent gates,
publication/recovery, second stack, live GitHub demonstration, evaluation/reporting.

Python package `graph_engineering` with `python -m graph_engineering` and installed
`graph-engineering` CLI; no runtime package dependencies beyond Python 3.10+.
Tests use unittest and real Git subprocesses. Commands use argv without a shell.
The CLI supports init, scan, execute, status, resume, stop, approve, graph, doctor,
evaluation and a durable foreground controller suitable for service management.

Success requires observed failing source evidence, validated/deduplicated local or
remote issue, worker-authored patch in a worktree, independently executed frozen
regression tests, actual-diff review, PR policy/approval blocking, permitted merge
and postmerge verification, interruption recovery, and operation on Python and
JavaScript repositories. Live results, backend simulations and unavailable operations
must be reported distinctly. Security abuse cases and invalid model outputs must
fail closed. Defaults preserve existing repositories and reject untrusted execution.
