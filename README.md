# graph_engineering

A local controller for agentic software engineering. You describe a change; a local model turns it into a
ticket, implements it in a sandbox, the harness checks the result itself, a second model session reviews it,
and a pull request is opened for **you** to merge.

```
request ─► intake agent ─► ticket ─► you accept ─► coding agent ─► gates ─► review agent ─► PR ─► you approve merge
```

The design follows Anthropic's *Building effective agents* and *Effective context engineering for AI agents*:
a fixed workflow outside, tool-using agents inside, ground truth from tests, a small working context.
See [docs/DESIGN.md](docs/DESIGN.md).

## Requirements

- Python 3.10+ and Git. No Python dependencies (the MCP server is an optional extra).
- An OpenAI-compatible model server with tool calling, such as vLLM with `--enable-auto-tool-choice`.
- macOS (`sandbox-exec`) or Linux with `bwrap` for the sandbox. Elsewhere, only repositories registered as
  `trusted` can run, without a sandbox.
- `gh`, signed in, for GitHub publication.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export GE_HOME=$PWD/.runtime
graph-engineer init                         # writes $GE_HOME/models.json; edit endpoints and roles
graph-engineer models                       # checks every endpoint
graph-engineer repo add myapp ~/code/myapp  # detects setup, test, build and the GitHub slug; review the output
graph-engineer serve                        # workers + control centre at http://127.0.0.1:8765
```

File a ticket in the control centre or with `graph-engineer ticket new myapp "what you want"`.

| You do | Command |
|---|---|
| Accept a proposed ticket | `ticket accept ID` (or set `auto_accept`) |
| Answer the intake agent's questions | `ticket answer ID "..."` |
| Publish a ready change (if `auto_publish` is off) | `ticket approve-publish ID` |
| Approve the merge of the PR's current head | `ticket approve-merge ID` |
| Send it back with comments | `ticket request-changes ID "..."` |
| Retry after a failure / cancel | `ticket retry ID` / `ticket cancel ID` |

## Models

`models.json` lists endpoints and assigns them to roles: `intake`, `coding`, `review` and `explore`.
A role lists models in order of preference. When `review` lists more than one model, the reviewer is a different
model from the coder.

## What the harness checks

After each coding round, without trusting the agent's account: some test and some source file changed, no test was
deleted, no protected or generated file changed, each changed test **fails on the base revision and passes on the
result**, and the full test command and build pass. Then the review agent reads the diff with read-only tools.
Failures become the next round's feedback (default three rounds).

A merge needs, at one exact commit: your approval, passing required checks, satisfied reviews, a mergeable PR and
readable branch protection. The controller never uses admin rights. Approvals are not available over MCP.

## Evals

```bash
graph-engineer eval evals/tasks/*.json --repeat 3
```

Each task is a real past ticket pinned to its base commit; some carry the maintainers' own tests as a hidden,
stricter check. Use evals to measure every change to prompts, tools or models.

## Tests

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
```

The tests use a scripted model server and real Git repositories; sandbox tests run the real OS sandbox.
They prove the harness logic, not a model's ability; that is what the evals are for.
