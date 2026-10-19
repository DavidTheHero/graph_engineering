# Phase 2 control centre

Keep the existing Workflow/Store as execution and audit authorities. Add SQLite
queue/ticket metadata, an OS-locked multi-process scheduler, read-only repository
registration, same-origin protected operator commands and an SSE event projection.
Workers execute one existing graph node per dispatch so scheduling and pause/cancel
apply at durable boundaries. A separate process owns each ticket attempt, never a
shared SQLite connection or mutable Workflow instance across threads.

Render a local bundled Cytoscape compound graph alongside a Kanban board and
worker/queue monitor. Runtime instances and ticket markers come from correlated
persisted events. Historical playback is explicitly marked. Save only layout and
theme in browser preferences. Preserve the Phase 1 UI at /legacy.

Default new repositories to read-only. Development and GitHub publication need
explicit operator configuration. Read-only analysis must not run repository code
or publish tickets remotely. For General_AI_agent's dirty checkout, inspect a
controller-owned committed snapshot; retain source and revision provenance.

## Verification sequence

Queue ordering/admission/recovery tests -> one real scheduled node -> operator API
and SSE tests -> board/network browser tests -> parallel and retry integration
tests -> actual local Qwen execution -> General_AI_agent demonstration -> package
and full regressions. Test doubles/failure injection are clearly labelled, never
presented as real model or GitHub outcomes.
