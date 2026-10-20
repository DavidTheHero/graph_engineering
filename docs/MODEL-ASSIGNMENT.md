# Automatic model assignment

Give Graph Engineering a set of OpenAI-compatible endpoints. It measures each
model with real calls and assigns every graph role to the models that meet that
role's requirements.

```bash
graph-engineer models assign --endpoint http://192.168.0.60:18020/v1 --endpoint http://192.168.0.62:18035/v1
graph-engineer models assign --model http://host:8000/v1=some-model-id      # assess only specific ids
graph-engineer models assign --plan <home>/model-assignment.json --apply   # apply a reviewed plan
```

The default is a **dry run**: the plan and all evidence go to
`<home>/model-assignment.json`. The exit code is 2 when the assessment is
incomplete or some role has no model. The MCP tool `model_assignment` (read
profile) shows the applied assignment and the latest evidence.

## What is measured (through the production OpenCode worker)

| Probe | Pass condition |
|---|---|
| Discovery | `/v1/models` id, context window (`max_model_len` or `dmr.context_window`), parameters (server metadata or the id, e.g. `27b`) |
| Structured output (×trials) | Schema-valid JSON and the correct category for an unambiguous report |
| Grounding (×trials) | Exact file, the single correct line, and a quote copied from that line in numbered source, checked deterministically |
| Long grounding (context ≥16K) | The same question with the answer mid-way through a ~30 KB prompt, larger than a real investigation |

## Role requirements (`ROLE_REQUIREMENTS`)

| Roles | Context | Structured | Grounding | Long | Tier |
|---|---|---|---|---|---|
| classification | ≥4K | ≥0.9 | — | — | any |
| investigation, question_answering, review, regression_review | ≥8K | ≥0.9 | ≥2/3 | — | any |
| root_investigation, planning, implementation, regression_test, deep_review | ≥32K | ≥0.9 | 3/3 | required | large |

The tier is **large** for models with ≥14B parameters. Unknown sizes count as large
only with perfect grounding and long context. A long context window alone never
makes a small model large.

Ranking: bounded roles prefer the cheapest capable model (small tier, then accuracy,
then latency), with larger models as fallbacks. Complex roles prefer grounding, then
long-context success, then size, then latency. Each role's list is also its fallback
order; at run time routing still skips models observed down. Every exclusion is
stored with its reason under `decisions[role][model]`.

## Applying

`--apply` writes `<home>/models.json` (each model's `tier` plus a `roles`
allow-list) and `<home>/routing.json`. It sets `routing: "assigned"` and
`role_routes` in every registered repository configuration, and backs up the
previous registry and plan in `<home>/assignments/`. New registrations and
natural-language tickets inherit the assignment. Runs already in flight keep their
snapshot.

The apply refuses two cases unless you override them:

* an **incomplete** assessment (an endpoint was unreachable): `--allow-partial`.
  Without this, a temporary outage could strip a model's roles.
* **uncovered roles**: `--allow-uncovered`. Those graph steps will block with an
  explicit routing reason.

## Shared-server safety (incident, 2026-10-20)

Probing the 262K-context variant of the 4B on this Mac exhausted GPU memory
(`kIOGPUCommandBufferCallbackErrorOutOfMemory`). That left Docker Model Runner's
llama.cpp backend in an error state, so its healthy 8K sibling then failed every
probe as well. Recovery was `docker model unload --all`. The first implementation
scored those failures as "structured output 0.00", which would have stripped the 8K
model of its roles. The assessment now:

* treats transient worker/server faults (ADR 008 classification) as **unassessed**,
  not incapable. The plan is incomplete and `--apply` refuses it, naming the models;
* probes the models on each endpoint **smallest context first**;
* after an infrastructure fault, **skips** the remaining models on that endpoint
  instead of scoring them against a broken server.

Use `--model URL=ID` to assess only the models you intend to use. Do not probe
variants whose context the host cannot hold.

Measured on 2026-10-20 (`--model` for `qwen3.8-27b` and `docker.io/local/qwen35-4b:8k`,
3 trials, 59 s): both models scored 1.0 on structured output, triage and grounding,
and the 27B also passed the long-context probe. Result: the 4B is first for the
bounded roles with the 27B as fallback, and the 27B alone serves the complex roles.
