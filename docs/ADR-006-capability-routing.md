# ADR 006: capability-based model routing and a controller model registry

Date: 2026-10-20. Status: accepted and implemented (`routing.py`, `models.load_models`).

## Context

The audit found model selection hard-coded across seven modules:

* the literal keys `"a"` and `"b"` in `workflow.ask`, `scheduler.ROLES`,
  `question_answering`, `accepted_development`, ticket reassignment, the capacity
  settings and six places in the web UI;
* the 27B/4B identity inferred from model-ID substrings (`'qwen3.8'`);
* every repository configuration embedding a copy of `default_models()`.

Adding a provider meant editing code. A down large model blocked everything, and
nothing explained why.

## Decision

* Graph nodes request a **role** (`classification`, `root_investigation`, `planning`,
  `implementation`, `review`, …) and optionally a tier. They never name a server.
* Each model entry declares `tier` (`large` or `small`), a `label` and an optional
  `roles` allow-list. Legacy entries without a tier infer it from the context window
  (≤16K = small).
* `routing.select(config, role, tier, health)` returns the model and a decision
  record:
  * `baseline` profile: the large tier for every role (the measured default);
  * `multimodel` profile: each role uses its own tier (`ROLE_TIERS`);
  * bounded roles may **escalate** from small to large;
  * complex roles fall back to a small model **only** when the operator lists them in
    `small_fallback_roles`;
  * the latest observed health is respected. A model observed down is skipped.
    Every fallback is a persisted `degraded_routing` event.
  * When no permitted model is healthy, `RoutingError` (a `ModelError`) blocks the
    step with an explicit reason. The scheduler shows that reason on the queued
    ticket instead of dispatching a step that cannot succeed.
* `<home>/models.json` is the controller's provider registry. Keys are arbitrary
  names. The registry rejects unknown fields and inline credentials (use
  `api_key_env`).
* `graph-engineer models` and `doctor` compare each configured `model_id` with the
  server's `/v1/models` listing. A mismatch is reported, never guessed.
* The UI reads labels and tiers from the registry in the snapshot.

## Consequences

* An OpenAI-compatible remote provider is added by configuration only
  (`tests/test_routing.py::test_arbitrary_provider_names_route_by_tier`).
* Existing configurations and stored runs keep working: the keys `a` and `b` remain
  valid names.
* Discovery showed that the 4B server also advertises
  `huggingface.co/unsloth/qwen3.5-4b-gguf:UD-Q4_K_XL` with a 262,144-token window. It
  can be registered as another small-tier entry, but it has **not** been evaluated
  here, so it is not a default.
