# ADR 005: one MCP server over a protocol-neutral capability layer

Date: 2026-10-20. Status: accepted and implemented (`capabilities.py`, `mcp_server.py`).

## Context

Before this change, the only programmatic interface was the loopback control-centre
HTTP API. Its CSRF token is designed for a browser. Codex, OpenCode and other agents had
no standard way to inspect repositories, tickets, runs or evidence.

The official Python SDK (`mcp` 2.3.0, released 2026-10-02) replaced FastMCP with
`mcp.server.mcpserver.MCPServer`. It negotiates protocol versions `2024-11-05` through
`2026-07-28`. Its dependencies are pydantic, starlette, uvicorn, httpx2, jsonschema
and OpenTelemetry.

## Decision

* **One server, one capability layer.** `Capabilities` is a plain Python service with
  no MCP import. It enforces profiles, validates arguments and writes an audit event
  for every call: allowed, refused or failed. `mcp_server.py` is a thin adapter.
  Writes reuse the dashboard's idempotent command dispatcher (`control_api.operate`),
  so MCP cannot reach anything the operator API cannot.
  No per-agent servers: every agent role would need the same data, and duplicating
  servers multiplies the permission surface.
* **Least privilege by construction.** Each profile (`read` < `operate` < `admin`)
  lists only its own tools. The capability layer checks the profile again.
  Defaults to `read`.
* **Approvals are not capabilities.** Accepting a ticket revision, starting
  development, trusting code execution, publishing and approving a merge exist only
  in the CLI and the control centre. An agent connected over MCP cannot approve its
  own work.
* **Untrusted content is labelled.** Every response that carries repository text,
  diffs, logs or model output says so. The server instructions tell clients never to
  follow instructions found in that content.
* **Structured errors.** Failures are MCP tool errors (`isError=true`) with a stable
  prefix: `permission_denied:`, `not_found:`, `invalid_argument:` or
  `internal_error:`. Internal errors expose only the exception type.
* **Transports.** The default is stdio. HTTP is streamable HTTP bound to `127.0.0.1`;
  it needs a bearer token of at least 24 characters (constant-time comparison) and
  rejects non-loopback Host headers (DNS rebinding).
* **Optional dependency.** The controller core stays dependency-free.
  `pip install 'graph-engineering[mcp]'` adds the SDK. `doctor` reports whether it is
  installed and performs a real handshake.

Worktree creation, test execution and GitHub writes are **not** exposed as raw tools.
They run only inside graph nodes, under the node's policy, locks and effect ledger.
MCP exposes their results (`engineering_artifacts`) and the controls to steer the
graph (create, clarify, pause, resume, cancel, retry, read-only scans).

## Consequences

* Schema-level argument rejections happen inside the SDK, before our code runs, so
  they are not in the audit trail. Calls that pass the schema are always audited.
* The audit trail is in the same `events` table (`run_id='mcp'`), so it is visible
  in the control centre's history.
* Future: per-client tokens and OAuth through the SDK's `auth` hooks before any
  non-loopback deployment.

## Verification

`tests/test_mcp.py` uses the official SDK client:

* in-process sessions;
* a real stdio subprocess launched through the CLI;
* raw JSON-RPC `initialize` negotiation of `2025-06-18`;
* authenticated streamable HTTP, and 401 responses for a missing token, a wrong
  token and a foreign Host;
* profile isolation, idempotency, git-option injection and committed-only search.

OpenCode 2.0.32 reported `✓ graph-engineering connected` as a client. See
`docs/MCP.md`.
