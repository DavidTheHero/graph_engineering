# ADR 008: classify infrastructure faults; watchdog, backoff and a separate retry allowance

Date: 2026-10-20. Status: accepted and implemented.

## Incident

Ticket GE-30D05C40 ("add feature to see which AI model is connected to the current
chat") blocked at `problem_investigation`. Both 27B attempts ran for exactly 120.0 s
and produced no output. Each worker's private OpenCode log stopped after "database
schema bootstrap completed": OpenCode 2.0.32 stalled during startup, and the model
was never contacted. The identical request replayed directly to the 27B returned
valid JSON in 15.9 s, and twelve consecutive worker startups each booted in about
2.5 s. The stall is intermittent; its cause inside OpenCode is unknown.

The workflow turned that transient fault into a permanent block:

* nothing detects a silent worker before the 120 s call timeout;
* the second attempt runs 2 s after the first, with no health check;
* infrastructure faults spend the node's 2-attempt budget, which exists to bound
  invalid *model output*;
* a blocked ticket never retries by itself;
* the error ("OpenCode exceeded invocation timeout") does not say where it stalled;
* the node deadline (120 s) equals the model timeout, so the deadline can fire first.

A second `ui --control` against the same home also failed to start its scheduler
(`BusyError` in `scheduler.log` only), and the dashboard did not say so.

## Decision

1. **Liveness watchdog** (`models._run_opencode`). Output goes to files and is
   polled. OpenCode emits `step_start` once its session is up and before the model
   streams (measured at 2–5 s). If there is no byte of output within
   `startup_timeout` (default 45 s, configurable per model), the process tree is
   killed and `WorkerStalled` is raised, with `phase="startup"`. The worker's OpenCode
   log tail is saved in `result.json`.
2. **Fault classification.** `ModelError(transient=True, phase=…)` marks
   infrastructure faults:
   * startup stall;
   * generation timeout;
   * nonzero OpenCode exit or provider error event;
   * model unhealthy or busy;
   * routing finding no healthy model;
   * process or OS errors, including the node deadline.

   Invalid JSON, schema violations, tool use and policy refusals are not transient.
3. **Separate allowance with backoff** (`Workflow.infrastructure_retry`). A transient
   fault does **not** count against the step's attempts. Each node visit has its own
   allowance (`max_infrastructure_retries`, default 4, range 0–10). Backoff is
   15 s, 30 s, 60 s, 120 s (capped at 600 s). The run stays `ready` with
   `retry_not_before`. The scheduler holds the ticket in the **Retry** queue and the
   queue explains why: "Automatic infrastructure retry n/N at HH:MM:SS (phase):
   message". Faults other than startup stalls probe the model's health immediately,
   so an unavailable server parks work under *Waiting for resource* rather than
   failing it.
4. **Dead letter only when the allowance is exhausted.** The error gets the category
   `infrastructure_exhausted`, the ticket shows a specific cause in the Dead Letter
   queue, and a manual Retry resets the allowance.
5. **Deadlines.** Nodes that call models have a deadline of 300 s, so the worker
   watchdog and call timeout, not the node deadline, decide the failure and report it.
6. **One scheduler per home.** `ui --control` / `centre` attaches to a scheduler with a
   fresh heartbeat and a live PID. If its own scheduler exits during startup, it says
   so on the console.

Not adopted (yet): bypassing OpenCode with a direct API call for proposal-only roles.
It would remove this failure class, but it departs from the OpenCode worker decision
(ADR 001) and should be an explicit, opt-in policy.

## Verification

* `tests/test_robustness.py` (10 tests):
  * real subprocesses: a silent worker is killed as a stall in under 5 s; a slow
    worker that has produced output is a generation timeout; a 300 KB stdin is fed
    without deadlock;
  * the real intake graph: 3 stalls give 15/30/60 s backoff, the investigation then
    completes, and the step budget records only the successful attempt; exhaustion
    dead-letters with its cause; manual Retry resets the allowance; non-startup
    faults probe health;
  * the real scheduler dispatch holds a ticket in Retry until `not_before`;
  * controller detection; model-node deadlines.
* Live, with real OpenCode and Qwen on 2026-10-20 (scratch controller; 27B stalls
  forced with a 1 s startup window and the allowance set to 2):
  * each stall was detected in 1.1 s instead of 120 s;
  * the real scheduler waited 15 s, then 30 s;
  * the step budget stayed at 0;
  * the ticket dead-lettered with "Infrastructure retries exhausted (2) … OpenCode
    produced no output within 1s";
  * after the startup window was restored and Retry pressed, the same report
    completed to Ready for Review with validated references (27B calls: 18.8 s and
    22.4 s);
  * a second `centre` attached to the running scheduler (PID 86207) instead of
    starting another.
