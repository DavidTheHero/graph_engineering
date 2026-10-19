# ADR 004: Continue accepted tickets through the existing execution engine

Accepted 2026-10-19.

An accepted intake ticket previously remained at development admission. Repeated
operator prompts could not bridge it into implementation. Cancellation was
projected as Blocked, obscuring the actual persistent state.

Reuse the existing workflow, scheduler leases, SQLite store, worktree tools and
delivery gates. Add five nodes for isolated setup, regression authoring, independent
regression review, deterministic validation and approved delivery. Add a persisted
repository policy for automatic development/publication/locked npm preparation.
Require current revision acceptance before dispatch; check the policy revision
inside the admission transaction. Concurrent operator commands defer admission
without terminating the controller. Cancelled work stays cancelled.

A new regression is a distinct controller-selected artifact, reviewed before it
executes, required to fail twice on the original source, and frozen before the
source implementation agent runs. Exact source-fragment proposals avoid rewriting
large files. Full registered tests, regression, independent diff review and existing
merge policy remain required. Failed evidence never becomes a passing receipt.

Use Windows Task Scheduler as the local OS supervisor instead of assuming the
interactive assistant or a detached shell remains alive. This initial supervisor
requires a logged-in user and supports one controller per durable home.

Observed model evidence also requires a transport adjustment: the 4B server
returned HTTP 400 "failed to parse grammar" for a 6000-character bounded string;
a long-string-unbounded schema returned code. Keep the complete schema and all
length checks locally, while omitting large string-length repetitions only in
the transport schema. The [llama.cpp grammar implementation and limitations](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)
support treating JSON Schema conversion as a subset capability. This observation
does not identify the server's exact underlying library/version or prove that all
bad generation was caused by the grammar.

Rejected: a second ticket execution engine, worker-written arbitrary test paths,
LLM assertions as test evidence, silently removing failed checks, unlimited retries,
or implicit merge approval. Hostile-code OS isolation and authenticated approval
identities remain outside this trusted single-operator deployment.
