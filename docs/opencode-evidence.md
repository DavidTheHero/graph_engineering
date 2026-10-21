# Installed OpenCode and model evidence

The installed Windows npm package is `@opencode/cli` **2.0.32**. The exact upstream
tag resolves to commit `843a6c514e606b436a7fe713422ace0a93ad3d61`.
[Config source](https://github.com/anomalyco/opencode/tree/843a6c514e606b436a7fe713422ace0a93ad3d61/packages/schema/src/config)
and the native provider implementation were examined, alongside actual CLI help and
captured requests. A local source checkout is retained but excluded from this project's
Git history. Research snapshots and source manifests pin other upstream references.

This version uses plural `providers` and `agents`, provider `package` and `settings`,
model `body`/`capabilities`/`limit`, agent `system`/`steps`, and ordered permissions
and policy arrays. It does not use the legacy V1 singular provider/options/prompt
schema. The sample in `configs/opencode-v2-implementation.example.json` is generated
by the same function as real invocations. Every worker keeps its exact generated
configuration in its invocation directory.

Workers use native `@opencode/ai/providers/openai-compatible`, `run --standalone`,
an explicit `local/<real-model-id>`, `--agent` and `--format json`. Prompts go through
stdin, avoiding Windows command-line size limits. A fresh profile uses isolated
`OPENCODE_CONFIG_DIR`, `OPENCODE_DB` and XDG directories; project configuration,
Claude imports and remote model fetching are disabled. Title generation is disabled.
All tools/integrations are denied, and any emitted tool event is rejected by the
proposal parser. Distinct scoped investigation/planning/implementation/review prompts
and separate sessions exchange validated artifacts, not shared chat histories.

## Observed servers

| Property | A | B, selected 8K alias |
|---|---|---|
| URL | `http://192.168.0.60:18020/v1` | `http://192.168.0.62:18035/v1` |
| Actual ID | `qwen3.8-27b` | `docker.io/local/qwen35-4b:8k` |
| Advertised context | `max_model_len: 150000` | `dmr.context_window: 8192` |
| Chat and strict JSON schema | Observed successful | Observed successful |
| Function call / response | Observed successful | Observed successful via protocol and actual OpenCode read tool |
| Invalid model / malformed messages | HTTP 404 / 400 | HTTP 404 / 400 |
| Authentication | Anonymous chat works; invalid `/models` auth accepted | Anonymous chat works; invalid chat auth accepted |
| Output probing | Oversized request rejected against combined server limit | `max_tokens:999999` accepted for a short response; maximum effective output not established |
| Controller output budget | 4096, operator choice | 1024, operator choice |

B also advertised `huggingface.co/unsloth/qwen3.5-4b-gguf:UD-Q4_K_XL` with context
262144. Initial probes of that alias returned compute errors for schema/tools.
They are retained, not treated as supported features. The intended Docker 8K alias
subsequently passed. Initial B connection timeouts are also retained. Final `doctor`
observed both servers healthy. Endpoint availability is checked again at runtime.

An initial OpenCode B request failed with `Failed to initialize samplers`. The
captured native request showed that `steps:1` injected an assistant prefill announcing
the maximum step count while JSON grammar was active. Changing the proposal budget
to two steps, denying tools and disabling title generation removed that prefill;
the captured corrected request and real session succeeded. This is a measured
compatibility change, not an assertion that a generic OpenAI example will work.

## Context and routing

B receives selective source snippets and failure-output slices, with at most a
6000-byte configured input budget further reduced to `8192 - 1024 - 2048 = 5120`
bytes for prompt plus schema. Byte count is a conservative bound, not an exact
tokenizer measurement. Essential large review input routes explicitly to A rather
than silently dropping part of the diff. A's configured input budget comes from the registry
(three fifths of its context by default, at most 120000 bytes; 40000 was too small for a
three-file change).
The server-advertised context is retained; no unsupported maximum-output capability
is invented. Deadline and cancellation apply to each OpenCode child process.

In the first evaluation B returned positive commentary marked critical while also
approving a diff. Those contradictions were blocked and led to exhausted repairs.
Numbered source, explicit defect-only review instructions, candidate correction,
and fresh deeper A review for contradictory results were added without weakening
the merge gate. Subsequent real workflows passed on both stacks. The A-only profile
was faster on the tiny measured sample, so it is the default; multi-model mode is
explicitly selectable and remains a useful comparison, not a proven superiority claim.

## Raw records

- `evidence/connectivity/a_*.json`: actual A protocol requests/responses.
- `evidence/connectivity/b_*.json`: initial connectivity/alias failures.
- `evidence/connectivity/b8k/*.json`: actual selected-alias protocol requests/responses.
- `evidence/connectivity/b8k-native-request/request.json`: captured successful native request.
- `evidence/connectivity/b8k-tool-opencode/result.json`: actual completed read call through OpenCode.
- `evidence/connectivity/opencode-tool-probe/result.json`: actual A completed read call, with a denied-policy probe also retained.
- `evidence/connectivity/final-doctor.json`: final CLI/version/endpoint observation.
- `evidence/evaluation/*/`: actual per-model task calls, latencies, outcomes, before/after logs and routing baselines.

Tool probes grant only read of a disposable fixture and do not represent production
worker permissions. Production tasks report zero executed tools by design. Model
artifact success and actual tool-call success are recorded separately.
