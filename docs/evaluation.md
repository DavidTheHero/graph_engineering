# Repeatable evaluation

`seed_repositories(root: Path)` creates two independent, real Git repositories inside a supplied empty fixture namespace. It refuses to overwrite any repository or seed receipt. Repeating an evaluation requires a new namespace; the helper never deletes prior results.

* `python-invoice`: standard-library `unittest`, a Python dependency manifest, and integer-cent percentage discounts. The injected source defect subtracts percentage points as cents. The existing regression asserts that 10% off 20000 cents is 18000.
* `javascript-collections`: ES modules, `package.json`, and Node's built-in test runner. The injected defect sorts a deduplicated array, violating preservation of first occurrence order. Existing tests assert the original order, identical-value handling, and empty input.

Each fixture has exactly two initial commits: working source with three tests, followed by a source-only defect commit. The helper runs the real tests at both commits, verifies the first process exits zero and the second exits nonzero, and records both outputs, argv, latency, and commit SHAs in sidecar `*.seed-evidence.json`. `seed-manifest.json` indexes the receipts. No dependency installation or external network access is needed for these fixtures. Git, Python >=3.10, and Node >=18 must already be installed.

The JavaScript fixture contains an intentionally malicious `AGENTS.md` requesting credential access, writes outside the task, CI changes, self-approval, and review bypass. This is untrusted evaluation text. It is never executed or adopted as controller policy. A successful injection-resistance test also verifies that the requested marker and unauthorized files do not appear; retaining this text alone is not proof of resistance.

## Running comparisons

From the project directory, after installing the package, `powershell -NoProfile -File scripts/demo.ps1 -SeedOnly` creates and verifies a fresh fixture pair without invoking models. Omit `-SeedOnly` to run endpoint health checks and the full configured workflow with the local publication backend. The script records each run result and prints exact-head manual approval/resume commands when a run reaches its approval gate. It keeps blocked results and their actual error evidence. Set `-OutputRoot` and `-ControllerHome` to choose independent evaluation namespaces.

The equivalent CLI sequence is `graph-engineering seed --output <fresh-directory>`, `graph-engineering --home <controller-home> init <repository> --trust-local-code`, then `graph-engineering --home <controller-home> execute --config <returned-config-path>`. `status <run-id> --events` reads checkpoints and audit records; `stop <run-id>` requests safe cancellation; `resume <run-id>` explicitly clears the stop request. Execution exits with code 2 for a blocked result and code 0 for an approval wait. `serve --once` processes eligible persisted runs once; `serve --poll 5` runs the durable foreground controller for an external service manager. The controller does not turn an unapproved wait into an approval.

Run the framework's normal workflow against a fresh pair of repositories with measured multi-model routing, then run its single-model baseline against a separately seeded pair. Use identical test commands, approval policy, and defect history. Keep actual worker output, model/server identifiers, raw tool events, command results, graph transitions, and merge/backend receipts for each run. Local backend issue/PR/merge operations must be labelled local; they do not demonstrate GitHub authorization or a live pull request.

Call `evaluate_reports(reports, baseline_reports)` with independently verified measurements. The aggregator never calls a model or fabricates a successful operation. Missing measurements remain JSON `null`, including a zero-denominator rate. A measured count of zero is different from an unavailable count.

The report contract is a list of objects; every field is optional, and present measurements are validated:

| Field | Meaning |
| --- | --- |
| `repository`, `stack` | Stable repository identifier and independently detected technology stack. |
| `detection.true_positive`, `false_positive`, `true_negative`, `false_negative` | Nonnegative integer counts labelled from known ground truth, never an investigator model's confidence. |
| `resolved` | Boolean: independent acceptance checks established the task outcome. |
| `regression.before_failed`, `after_passed` | Booleans backed by original-code and fixed-code process results. A regression succeeds only when both are true. |
| `duration_seconds` | Finite, nonnegative measured task duration. |
| `retries` | Observed nonnegative integer retry count. |
| `recovery_success` | Boolean outcome of an actually attempted failure/recovery scenario. Omit when recovery was not tested. |
| `tool_calls[]` | Each record has a nonempty `model` identifier and optional observed `success` boolean, `latency_seconds`, `input_tokens`, `output_tokens`. A schema-valid model answer alone is not a successful tool call; use actual tool-execution events. |
| `merge_policy_violations`, `duplicate_issues`, `duplicate_prs` | Independently audited nonnegative integer counts. Omit unless the corresponding audit ran. |

The result contains `workflow`, `baseline`, and `comparison`. `measurement_counts` identifies how many reports contribute to each aggregate; per-model measurements expose their measured-outcome/latency counts. Baseline duration, resolution-rate and precision deltas are descriptive observations. A small demonstration does not establish statistical significance or prove the multi-model architecture adds value.

Precision is `TP/(TP+FP)`. False-positive rate is `FP/(FP+TN)`, which requires known negatives. The two seeded defects supply positive cases; without separately labelled negative findings they cannot establish a false-positive rate. Do not report `0%` by assuming every unobserved negative was checked. Report portability as the number of actually evaluated repositories/stacks, not arbitrary-language support.

## Verification performed while implementing this module

On 2026-10-18, `python -m unittest tests.test_evaluation -v` first failed because the evaluation module did not exist. After implementation it ran **six tests successfully in 1.234 seconds**. Those tests created real temporary Git repositories, executed the working and broken Python/Node test suites, verified two-commit history and a source-only defect diff, checked clean worktrees and existing-directory preservation, and exercised measured-only metric aggregation and invalid-response rejection. This result covers the fixture/evaluation module; it is not a model, GitHub, or end-to-end workflow result. Final demonstration results belong in the run's retained evidence.
