"""Evals: fixed tasks, run repeatedly through the real development round, scored by the harness.

A task file:
  {"id": "...", "repo": "<registered repository name>", "base": "<commit or ref>",
   "spec": {"title", "kind", "problem", "acceptance_criteria": [...]},
   "hidden_tests": {"files": {"path": "<content or @path/in/repo@ref>"}, "command": "..."}}   (optional)

Each run uses its own home so runs cannot share state. A run passes when the round reaches `ready`
(gates and review passed); hidden tests, when given, are the maintainers' own tests for the same change,
run on the result as a second, stricter score.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import statistics
import time

from .llm import Registry
from .repos import Repos
from .sandbox import Sandbox, clip, git
from .store import Store
from .workflow import Context, accept, develop


def run_task(task: dict, repos: Repos, registry: Registry, out: Path, index: int) -> dict:
    home = (out / f"{task['id']}-{index}").resolve()
    if home.exists():
        shutil.rmtree(home)
    repo = repos.get(task["repo"])
    repo = replace(repo, auto_accept=False, auto_publish=False,
                   max_rounds=task.get("max_rounds", repo.max_rounds))
    local = Repos(home)
    (local.dir / f"{repo.name}.json").write_text(json.dumps(repo.public()))
    store = Store(home / "ge.db")
    ticket = store.create(repo.name, task["spec"]["title"], "eval", max_rounds=repo.max_rounds, spec=task["spec"], state="proposed")
    if task.get("base"):
        store.merge_data(ticket["id"], base_ref=task["base"])
    accept(store, local, ticket["id"], "eval")
    ctx = Context(home, store, local, registry, owner="eval")
    started = time.time()
    while True:
        ticket = store.claim("eval", 3600, states=("accepted",))
        if ticket is None:
            break
        try:
            develop(ctx, ticket)
        except Exception as exc:  # an infrastructure error ends this run; it is reported, not retried
            store.release(ticket["id"], "eval", error=f"{type(exc).__name__}: {exc}")
            break
    ticket = store.get(ticket["id"] if ticket else store.list()[0]["id"])
    runs = store.runs(ticket["id"])
    result = {"task": task["id"], "run": index, "state": ticket["state"], "passed": ticket["state"] == "ready",
              "rounds": ticket["round"], "seconds": round(time.time() - started), "error": ticket["error"],
              "prompt_tokens": sum(r["usage"].get("prompt_tokens", 0) for r in runs),
              "completion_tokens": sum(r["usage"].get("completion_tokens", 0) for r in runs),
              "agent_runs": {role: sum(1 for r in runs if r["role"] == role) for role in {r["role"] for r in runs}},
              "home": str(home)}
    if task.get("hidden_tests") and result["passed"]:
        result["hidden"] = hidden(task["hidden_tests"], repo, home / "work" / ticket["id"])
    return result


def hidden(spec: dict, repo, root: Path) -> dict:
    """Copy the maintainers' tests into the result and run them."""
    work = root / "work"
    for path, content in spec["files"].items():
        if content.startswith("@") and content.endswith("@"):
            source, ref = content[1:-1].rsplit("@", 1)
            content = git(Path(repo.path), "show", f"{ref}:{source}")
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    sandbox = Sandbox(root, work, repo.sandbox_env(), trusted=repo.trusted)
    code, out, _ = sandbox.run(spec["command"], timeout=repo.run_timeout * 3)
    return {"passed": code == 0, "output": clip(out, 3000)}


def run(tasks: list[Path], repos: Repos, registry: Registry, out: Path, repeat: int = 1, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for path in tasks:
        task = json.loads(Path(path).read_text())
        for index in range(1, repeat + 1):
            log(f"{task['id']} run {index}/{repeat} ...")
            result = run_task(task, repos, registry, out, index)
            results.append(result)
            log(f"  {'PASS' if result['passed'] else 'FAIL'} {result['state']} rounds={result['rounds']} "
                f"{result['seconds']}s prompt={result['prompt_tokens']:,} completion={result['completion_tokens']:,}"
                + (f" hidden={'pass' if result['hidden']['passed'] else 'FAIL'}" if "hidden" in result else "")
                + (f" error={result['error']}" if result["error"] and not result["passed"] else ""))
            (out / "results.json").write_text(json.dumps(results, indent=1))
    summary = {"runs": len(results), "pass_rate": round(sum(r["passed"] for r in results) / max(1, len(results)), 3),
               "median_seconds": statistics.median([r["seconds"] for r in results]) if results else 0,
               "median_prompt_tokens": statistics.median([r["prompt_tokens"] for r in results]) if results else 0,
               "by_task": {t: f"{sum(r['passed'] for r in results if r['task'] == t)}/{sum(1 for r in results if r['task'] == t)}"
                           for t in dict.fromkeys(r["task"] for r in results)}}
    (out / "summary.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    return summary
