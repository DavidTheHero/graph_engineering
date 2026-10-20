# Measured results: 2026-10-20 (macOS)

These are real runs on this date unless a row says otherwise. The samples are small,
so the numbers are descriptive and not general rankings. Raw receipts are listed
under each section.

## Environment

macOS 26 (Darwin 25.6, arm64), Python 3.11.13, OpenCode 2.0.32, Git 2.53, gh 2.102
(authenticated), Node 25.8, `mcp` 2.3.0. Models, both verified against `/v1/models`
by `graph-engineer doctor`:

| Key | Server | Advertised id | Context | Tier |
|---|---|---|---|---|
| `a` | vLLM at 192.168.0.60:18020 | `qwen3.8-27b` | 150,000 | large |
| `b` | Docker Model Runner at 192.168.0.62:18035 | `docker.io/local/qwen35-4b:8k` | 8,192 | small |

At the start of the audit, the 27B host timed out on both ICMP and TCP. It was
reachable about 40 minutes later, and every 27B result below comes from after that
point.

## 1. Automated tests

| Suite | Result |
|---|---|
| Python unit, integration, protocol and failure-injection (`python -m unittest discover -s tests`) | **167 passed, 6 skipped** (Windows-only), 42.8 s. Baseline before this work: 136 passed. |
| New in this session | adapters (6), routing and registry (10), declared-graph enforcement (3), MCP capability and protocol (9), citation drift (2), scheduler hold (1) |
| Browser, Microsoft Edge + axe (`tests/browser`: `centre.cjs`, `intake.cjs`, `dashboard.cjs`) | **26/26 checks passed**, 0 console errors, WCAG 2.1 AA with no violations at 320–1600 px. The fixtures use **labelled inference doubles**. |

## 2. Model reliability (`scripts/benchmark_models.py`)

Receipt: `evidence/benchmarks/2026-10-20-macos.json`.

| Model | OpenCode structured output (production path) | Triage accuracy (5 categories) | Native OpenAI tool calls | Median / max latency (structured) |
|---|---|---|---|---|
| 27B | 10/10 schema-valid | 10/10 | 5/5 valid name and arguments | 2.0 s / 6.3 s |
| 4B (8K) | 10/10 schema-valid | 10/10 | 5/5 valid name and arguments | 4.5 s / 5.2 s |

The triage cases are deliberately unambiguous. This shows the transport is reliable,
not that the models are good at hard triage.

## 3. End-to-end engineering workflow (`scripts/run_evaluation.py --mode both --merge-local`)

Receipt: `evidence/evaluation/run-04-macos/`. The repositories are fresh real Git
repositories with seeded single-function defects (Python unittest, JavaScript
node:test). The publication backend is **local** (GitHub objects are simulated and
labelled). Each merge was explicitly authorised by the evaluation policy.

| Metric | Baseline (27B only) | Multimodel (4B investigate/review, 27B plan/implement) |
|---|---|---|
| Seeded defects detected / resolved / post-merge green | 2/2 / 2/2 / 2/2 | 2/2 / 2/2 / 2/2 |
| Clean-repository negative scans (false positives) | 0/2 | 0/2 |
| Mean task duration | 17.6 s | 35.8 s |
| Model calls schema-valid | 8/8 | 8/8 |
| Duplicate issues / PRs | 0 / 0 | 0 / 0 |
| Merge-policy violations | 0 | 0 |
| Approval-gate demonstrations | missing approval → `waiting_approval` (4/4); injected failed remote check → `blocked` (4/4); cancel and resume → returned to `waiting_approval` (4/4) |

Mean latency per call: 27B 4.6 s (12 calls), 4B 11.0 s (4 calls). As on 18 October,
the baseline profile is faster for these small tasks, so it remains the default.

## 4. Natural-language tickets against real repositories

Receipts: `evidence/2026-10-20/acceptance-tickets.json`,
`evidence/2026-10-20/GE-*-events.json`, `evidence/2026-10-20/live-*.png`.
These ran through the real scheduler (`graph-engineer dispatch`) with real models.
Both repositories were registered read-only, and no repository code was executed.

| | GE-05145CDA | GE-EABBDB96 |
|---|---|---|
| Repository | General_AI_Agent (JS + Python, local) | google/uuid (Go, public clone at `2d3c2a9`) |
| Report | "The agent sometimes gets stuck during tool execution. Investigate what is causing it." | Parse accepts braces and urn forms; is that inconsistent with Validate? |
| Graph path | intake → 4B triage → search ⇄ 27B investigation (1 additional search loop) → enhancement → validation → finalisation | same, plus a validation retry. Re-investigation was requested **through MCP** (`add_clarification`, operate profile). |
| Model calls | 4/4 valid (4B 12.2 s; 27B 25.2, 19.7, 17.6 s) | 6/6 valid (4B 8.8 s; 27B 19–24 s). One more call was killed by the crash test. |
| Cited references verified by the deterministic validator | 3/3 | Before the drift fix: 3/6 (three quotes cited one line off). After: **6/6**, with 2 recorded line-offset corrections. |
| Outcome | Needs Information: three concrete clarification questions | Needs Information |
| **Manual accuracy check against the source** | Hypothesis supported: `backend/agent-loop.js:85` awaits `execute()` with no timeout or abort. **Partial**: it missed that the planner call `choose()` (line 58) is also unbounded, and that one tool already uses `AbortSignal.timeout(10000)` (`tools.js:58`). | Verified findings correct (Parse documents and implements the non-standard forms). **Incorrect hedged claim**: "Validate (which likely only accepts standard RFC 9562 forms)". `Validate` (`uuid.go:223-253`) accepts the same forms. Validate was not in the retrieved excerpts, and the ticket labels the statement a hypothesis. |

Investigation accuracy, by manual grading: 2 of 2 tickets cite correct, verified
evidence. 1 of 2 hypotheses is supported but incomplete, and 1 of 2 contains an
unsupported speculative sub-claim. That claim is correctly marked as a hypothesis
and correctly held for human review. Validation rules out invented evidence; it
cannot rule out wrong reasoning, which is why the human review gate exists.

## 5. Recovery

| Scenario | Result |
|---|---|
| `kill -9` of the scheduler **and** its worker process during a 27B call (GE-EABBDB96, `problem_investigation`) | After restart: the orphaned lease was fenced (`worker_recovered`), the run returned to `ready`, the node re-ran from its checkpoint, the investigation completed, and there were **0 duplicate revisions** (2 revisions → 4, one per pass, as expected) |
| Unit-level failure injection (existing and new) | `test_control_recovery`, `test_effect_recovery`, `test_scheduler` and the intake outage tests all pass |
| Workflow recovery rate (live) | 1/1 |

## 6. MCP

| Check | Result |
|---|---|
| Official SDK client: in-process, stdio subprocess through the CLI, HTTP | pass; negotiated `2026-07-28` and accepted an older `2025-06-18` initialize |
| HTTP without token, with a wrong token, with a foreign Host | 401, 401, 401 |
| Read profile lists write tools | no (0) |
| Operate profile can approve, accept, start development or register | no; refused or not listed |
| `doctor` handshake | 14 read tools listed |
| OpenCode 2.0.32 as a client | `✓ graph-engineering connected` |
| Qwen 27B *invoking* an MCP tool through `opencode run` | **Not achieved.** In 3 attempts (standalone, service mode, explicit permissions) the model replied in text without calling a tool. Root cause not established. |

## 7. Portability

| Repository | Ecosystems detected | Registration | Investigation | Execution |
|---|---|---|---|---|
| General_AI_Agent | javascript, python | ✓ (source unchanged: `git status` clean before and after) | ✓ live | not trusted, not run |
| google/uuid | go | ✓ | ✓ live | not run (Go is not installed) |
| Seeded Python and JS fixtures | python / javascript | ✓ | ✓ | ✓ live through merge |
| Rust, Maven, Gradle, .NET, Ruby, generic | detected in unit tests | ✓ (tests) | retrieval is language-neutral | not run (toolchains absent) |

No Graph Engineering source was changed to move between these repositories.
