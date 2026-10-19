# ADR 002: tickets, scheduling and an event-driven network

Status: accepted for Phase 2 implementation.

The original graph is sequential within one ticket but branches for validation,
repair, approval and clean completion. It has no queue or concurrent ticket
controller. A drawing cannot honestly add concurrency. Introduce a persisted
scheduler that leases tickets and executes a single existing graph node in a
separate process. Concurrent tickets use independent processes and repository
locks; resource limits and dependencies gate dispatch. Durable events are the
only trigger for moving ticket markers.

SQLite tables are additive to schema version 1; old run/event/effect records and
merge gates remain authoritative. Tickets reference runs, never duplicate their
execution cursor. Queue metadata records admission, leases, delays and operator
commands. OS ownership locks reconcile crashed scheduler/worker processes without
assuming that a timed-out lease means a worker died. Existing effect intents still
govern publication recovery. No controller can reset an uncertain external effect.

Graph library evaluation (official sources inspected):

| Candidate | Fit | Decision |
|---|---|---|
| [React Flow / XYFlow](https://reactflow.dev/learn/layouting/layouting) | Rich React nodes, custom edges, subflows; requires a React component/build migration and separate layout library | Defer; current local UI uses DOM modules |
| [ELK.js](https://github.com/kieler/elkjs) | Strong layered layout, ports and compound nodes; layout only, needs a renderer | Defer initial auto-layout; layered output alone encourages a pipeline |
| [Cytoscape.js](https://js.cytoscape.org/) | Existing DOM integration, canvas, compound graphs, curved edges, pan/zoom, drag, animation and layouts | Adopt a locked local build, no CDN |
| Handwritten SVG/canvas | Existing SVG works for 20 fixed steps; compound layout, pan and instance aggregation would duplicate mature library behavior | Keep Phase 1 as legacy; replace for control centre |

Use clustered, manually seeded positions with optional compound force layout,
saved node positions and collapsed clusters. Graph edges combine executable node
contracts, scheduler admission/resource states and observed worker child events.
Multiple tickets provide genuine parallel lanes. No speculative refactoring,
test-writing or security worker is drawn as running when it does not exist.

[SSE](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
provides resumable event delivery with IDs. A fresh snapshot establishes the
cursor; subsequent events invalidate/refetch the authoritative snapshot and animate
only matching, observed transitions. Historical replay uses the same records with
an explicit replay banner. Animation time never drives ticket status.

Control is opt-in. A loopback server rejects foreign Host/Origin, requires a
per-boot CSRF header and idempotency key for writes, validates bounded commands and
does not accept arbitrary shell commands. New repository registration authorizes
metadata reads only; operator trust and publication settings remain explicit.
