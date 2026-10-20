# MCP server

Graph Engineering serves one Model Context Protocol server over the same capability
layer as the control centre. The design is in [ADR 005](ADR-005-mcp-capability-layer.md).

## Install and check

```bash
pip install -e '.[mcp]'          # adds the official SDK (mcp 2.3.x); the core stays dependency-free
graph-engineer doctor            # "mcp": {"available": true, "protocol_version": "2026-07-28", ...}
```

## Run

```bash
# stdio (what Codex/OpenCode launch); read-only by default
graph-engineer --home ~/.graph-engineering mcp serve --profile read

# loopback streamable HTTP at http://127.0.0.1:8787/mcp; a token is mandatory
export GRAPH_ENGINEERING_MCP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
graph-engineer mcp serve --transport http --port 8787 --profile operate
# clients send: Authorization: Bearer $GRAPH_ENGINEERING_MCP_TOKEN
```

`--home` selects the controller whose repositories, tickets and runs are served.
`--client` is a label recorded in the audit trail.

## Client configuration

Generate a snippet with absolute paths for the current installation:

```bash
graph-engineer --home ~/.graph-engineering mcp config --client codex     # ~/.codex/config.toml
graph-engineer --home ~/.graph-engineering mcp config --client opencode  # opencode.json
graph-engineer --home ~/.graph-engineering mcp config --client json      # generic mcpServers JSON
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.graph-engineering]
command = "/abs/path/.venv/bin/graph-engineer"
args = ["--home", "/abs/controller-home", "mcp", "serve", "--profile", "read", "--client", "codex"]
startup_timeout_sec = 20
tool_timeout_sec = 120
```

OpenCode 2.0.32 (`opencode.json` in the project). You can also run
`opencode mcp add graph-engineering -- <command…>`, which writes the `mcp.servers` form:

```json
{"mcp": {"servers": {"graph-engineering": {"type": "local",
  "command": ["/abs/path/.venv/bin/graph-engineer", "--home", "/abs/controller-home", "mcp", "serve", "--client", "opencode"]}}}}
```

OpenCode v2 loads configuration through a background service. After you change
`opencode.json`, `opencode mcp list` can show "No MCP servers configured" for a few
seconds; run it again. Verified on 2026-10-20: `✓ graph-engineering connected`.

Codex is not installed on the verification host. Its snippet follows the documented
`[mcp_servers]` schema but has not been exercised here.

## Profiles

| Profile | Adds | Never available |
|---|---|---|
| `read` (default) | 14 read tools, 6 resources | — |
| `operate` | `create_ticket`, `add_clarification`, `control_ticket` (pause/resume/cancel/retry), `control_scheduler`, `start_read_only_scan` | accepting tickets, starting development, trusting code execution, publishing, approving merges |
| `admin` | `register_repository` (metadata access only) | same as above |

Tools outside the active profile are not listed at all, and the capability layer
checks the profile again on every call.

## Tools

| Tool | Purpose |
|---|---|
| `list_repositories` / `inspect_repository` | Registered repositories; detected ecosystems, commands, CI and policy files. No code is executed. |
| `search_repository` | Literal `git grep` over **committed** blobs at a revision; returns line-numbered excerpts |
| `git_history` / `read_diff` | Commit log (optionally one path); changed files, diffstat and patch between two revisions |
| `list_tickets` / `get_ticket` / `ticket_timeline` | Board state; original report, revisions, evidence and validation; persisted transitions |
| `get_run` / `run_events` / `engineering_artifacts` | Graph cursor and attempts; paged event log; test receipts, patch, diff, review, PR, gates |
| `workflow_graph` | Nodes and the exact transitions the engine enforces |
| `model_health` / `scheduler_status` | Registry, tiers, observed health (`refresh=true` probes and verifies IDs); scheduler state |
| `create_ticket` … `start_read_only_scan` | Writes (operate profile); each needs an `idempotency_key`, and retrying with the same key is safe |

Resources: `graph-engineering://graph`, `graph-engineering://models`,
`graph-engineering://repositories`, `graph-engineering://tickets/{ticket_id}`,
`graph-engineering://tickets/{ticket_id}/timeline`, `graph-engineering://runs/{run_id}/events`.

## Errors and audit

Failures return `isError: true` with a stable prefix: `permission_denied:`,
`not_found:`, `invalid_argument:` or `internal_error:` (exception type only, no
stack). Every call that reaches the capability layer, including refused calls, is
stored as an `mcp_tool_call` event (`run_id = 'mcp'`). The event records the tool,
profile, client, outcome, bounded arguments, an argument hash and the duration.

## Prompt-injection boundary

Every response carrying repository text, diffs, logs or model output includes a
`trust` notice, and the server instructions tell clients to treat such content as
data. The search tool never reads uncommitted files. No MCP tool executes repository
code, writes to GitHub or creates worktrees. Those happen only inside graph nodes,
under policy and approvals.
