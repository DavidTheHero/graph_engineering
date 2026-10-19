# Local Qwen contributions and supervisory decisions

All inference receipts here came from real OpenCode calls. Model output is a
proposal; only observed tool/test evidence establishes correctness.

* 27B proposed scheduling policy code. The proposal contained literal escape
  sequences that failed parsing and mixed node/status conditions. Codex rewrote
  it as the small deterministic scheduling_policy module and tested boundaries.
* 4B reviewed that proposal and generated ten board-status cases. Nine were
  accepted; one expected To Do for an arbitrary node and was rejected by the
  explicit policy. See `qwen-case-verification.json`.
* 27B audited scheduler code. The cancellation-admission guard was adopted.
  Recommendations to reset blocked runs automatically, assume that BEGIN
  IMMEDIATE permits simultaneous claims, or treat OS ownership locks as remotely
  releasable were rejected. Real concurrent-process and live/killed-worker tests
  verify those decisions. Protected live child PIDs were already present.
* 27B wrote the quadratic ticket-marker interpolation function. It passed syntax
  and ten numerical/boundary checks and is integrated in centre-motion.js.
* In the final live scheduler demonstration, 4B made five calls: one bounded
  General_AI_Agent source investigation, two defect investigations and two diff
  reviews. 27B made four calls: two plans and two implementations. Both real-model
  fixes passed frozen independent regressions and reached local PR approval gates.
  `live-results.json` records all nine calls and four cross-ticket overlaps.

No new conclusion about overall multi-model superiority is inferred from this
small sample. The earlier baseline evaluation remains separate. The Phase 2
sample establishes functional routing/concurrency, not a statistically reliable
quality or throughput advantage. The deliberate repair injection uses no Qwen
inference and is excluded from the nine-call count.
