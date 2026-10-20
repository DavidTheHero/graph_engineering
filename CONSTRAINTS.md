# Enforced quality bar

- All framework unittest tests must pass. No skipped security/merge checks.
- No false-green tests: frozen baseline tests must fail before and pass after a fix.
- GitHub merges require fresh head-bound gates and server-side protections; never admin.
- Worker shell execution, target instruction execution and policy/test modification denied.
- Side-effect duplicates and merge-policy violations must be zero in recovery tests.
- Model failures use bounded retries and explicit blocked states, never canned patches.
- No dependencies needed at runtime except Python, Git, OpenCode and optionally gh/Node. The MCP SDK is an optional extra.
- Every graph transition is declared in CONDITIONAL_EDGES; undeclared transitions fail. Approvals are never MCP capabilities.
- No claims of OS sandboxing or unattended production readiness without the relevant evidence.
