"""Mine eval tasks from a repository's history.

A past commit that changed both tests and source is a task with a known answer: start from its parent, give the
commit's intent (subject, body, linked issue) as the ticket, and grade with the commit's own tests (hidden from the
agent). A task is kept only when that reference solution validates: the hidden tests fail on the parent and pass on
the commit, so a 0% pass rate means the agent failed, not that the task is broken (Demystifying evals for AI agents).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from .gates import clone_cow
from .repos import Repo
from .sandbox import Sandbox, clip, git, prepare_workspace

LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock")
TRAILER = re.compile(r"^(Co-Authored-By|Signed-off-by|Reviewed-by):.*$", re.I | re.M)


def candidates(repo: Repo, ref: str, limit: int, max_files: int = 12, max_lines: int = 600) -> list[dict]:
    """Commits on ``ref`` (first-parent, no merges) that change tests and source, add no dependency, and stay small."""
    found = []
    for line in git(Path(repo.path), "log", "--no-merges", "--format=%H %P", ref).splitlines():
        sha, *parents = line.split()
        if not parents:
            continue  # a root commit has no base to start from
        rows = git(Path(repo.path), "show", "--format=", "--numstat", "--no-renames", sha).splitlines()
        files, size = [], 0
        for row in rows:
            added, removed, path = row.split("\t", 2)
            files.append(path)
            size += int(added) + int(removed) if added.isdigit() and removed.isdigit() else 0
        tests = [p for p in files if repo.is_test(p)]
        sources = [p for p in files if not repo.is_test(p)]
        if not tests or not sources or len(files) > max_files or size > max_lines:
            continue
        if any(Path(p).name in LOCKFILES for p in files) or any(repo.test_config(p) or repo.protected(p) for p in files):
            continue
        found.append({"sha": sha, "files": files, "tests": tests, "lines": size})
        if len(found) >= limit:
            break
    return found


def criteria_from(text: str) -> list[str]:
    """Bullets under an 'Acceptance' heading, else every top-level bullet."""
    section = re.search(r"(?ims)^#*\s*acceptance[^\n]*\n(.*?)(?=^#+\s|\Z)", text)
    source = section.group(1) if section else text
    bullets = [re.sub(r"\s+", " ", b).strip() for b in re.findall(r"(?m)^\s*[-*]\s+(.+(?:\n(?!\s*[-*]\s)\s+.+)*)", source)]
    if section and not bullets:
        sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", section.group(1).strip()) if s.strip()]
        return sentences[:6]
    return bullets[:8]


def linked_issue(repo: Repo, message: str) -> dict | None:
    if not repo.slug:
        return None
    match = re.search(r"(?:#|issue[s]?/|\(#)(\d+)\)?", message)
    if not match:
        return None
    result = subprocess.run(["gh", "issue", "view", match.group(1), "--repo", repo.slug, "--json", "title,body"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        return None
    data = json.loads(result.stdout)
    return data if "pull_request" not in data else None


def spec_for(repo: Repo, sha: str) -> dict:
    subject = git(Path(repo.path), "log", "-1", "--format=%s", sha).strip()
    body = TRAILER.sub("", git(Path(repo.path), "log", "-1", "--format=%b", sha)).strip()
    issue = linked_issue(repo, subject + "\n" + body)
    text = (issue["body"] if issue else body) or subject
    # A subject such as "Throttle sweep errors; keep block reasons intact" lists behaviours: one criterion each.
    criteria = criteria_from(text) or [c.strip().rstrip(".") for c in subject.split(";") if c.strip()]
    title = re.sub(r"\s*\(#\d+\)$", "", issue["title"] if issue else subject)
    kind = "fix" if re.match(r"(?i)fix|bug|repair|correct", title) else "feat"
    return {"title": title[:120], "kind": kind, "problem": clip(text, 3000), "acceptance_criteria": criteria}


def task_for(repo: Repo, candidate: dict) -> dict:
    sha = candidate["sha"]
    tests = [p for p in candidate["tests"]
             if git(Path(repo.path), "cat-file", "-t", f"{sha}:{p}", check=False).strip() == "blob"]
    runners = [repo.runner(p) for p in tests]
    command = " && ".join(dict.fromkeys(r for r in runners if r)) if all(runners) else repo.test_command
    return {"id": f"{repo.name}-{sha[:8]}", "source": f"{repo.name} commit {sha[:12]}", "repo": repo.name,
            "base": f"{sha}^", "reference": sha, "spec": spec_for(repo, sha),
            "hidden_tests": {"files": {p: f"@{p}@{sha}@" for p in tests}, "command": command}}


def validate(repo: Repo, task: dict, scratch: Path) -> dict:
    """The reference commit must pass the hidden tests and its parent must fail them."""
    if scratch.exists():
        shutil.rmtree(scratch)
    box = scratch / "box"
    results = {}
    deps = None
    for label, revision in (("reference", task["reference"]), ("base", task["base"])):
        work = box / label
        prepare_workspace(Path(repo.path), revision, work)
        sandbox = Sandbox(box, work, repo.sandbox_env(), trusted=repo.trusted)
        if deps is None:
            for command in repo.setup:
                code, out, _ = sandbox.run(repo.expand(command), timeout=1800, network=True)
                if code:
                    return {"valid": False, "reason": f"setup failed: {command}", "output": clip(out, 1500)}
            deps = [e for e in ("node_modules", ".venv") if (work / e).is_dir()]
            first = work
        else:
            for entry in deps:
                clone_cow(first / entry, work / entry)
        for path, ref in task["hidden_tests"]["files"].items():
            source, sha = ref[1:-1].rsplit("@", 1)
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_text(git(Path(repo.path), "show", f"{sha}:{source}"))
        code, out, _ = sandbox.run(task["hidden_tests"]["command"], cwd=work, timeout=repo.run_timeout * 3)
        results[label] = {"exit": code, "output": clip(out, 1500)}
    valid = results["reference"]["exit"] == 0 and results["base"]["exit"] != 0
    reason = "" if valid else ("the reference commit fails its own tests" if results["reference"]["exit"] else
                               "the hidden tests already pass on the parent")
    shutil.rmtree(scratch, ignore_errors=True)
    return {"valid": valid, "reason": reason, **results}


def mine(repo: Repo, ref: str, out: Path, limit: int, check: bool = True, log=print) -> list[Path]:
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for candidate in candidates(repo, ref, limit * 3 if check else limit):
        task = task_for(repo, candidate)
        if check:
            verdict = validate(repo, task, out / ".validate")
            log(f"{task['id']}: {'valid' if verdict['valid'] else 'skipped: ' + verdict['reason']}  {task['spec']['title'][:70]}")
            if not verdict["valid"]:
                continue
        path = out / f"{task['id']}.json"
        path.write_text(json.dumps(task, indent=2) + "\n")
        written.append(path)
        if len(written) >= limit:
            break
    return written
