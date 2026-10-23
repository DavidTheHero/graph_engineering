"""The harness's own judgement of a workspace. Nothing here trusts what an agent said it did."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import re
import shutil
import subprocess

from .repos import Repo
from .sandbox import Sandbox, clip, git

LOG_CHARS = 3000


def check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def changes(work: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for every change against base, staged so new files count."""
    git(work, "add", "-A")
    fields = git(work, "diff", "--cached", "--name-status", "--no-renames", "-z", base).split("\0")
    return [(fields[i][0], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def snapshot(work: Path, base: str) -> str:
    """An unreferenced commit of exactly what is staged: what the gates test is what gets published."""
    git(work, "add", "-A")
    tree = git(work, "write-tree").strip()
    return git(work, "commit-tree", tree, "-p", base, "-m", "graph_engineering gate snapshot").strip()


def diff(work: Path, base: str, revision: str) -> str:
    return git(work, "diff", "--no-renames", base, revision)


def clone_cow(source: Path, target: Path) -> None:
    """Copy a directory copy-on-write where the filesystem allows it."""
    command = ["cp", "-cR"] if platform.system() == "Darwin" else ["cp", "-a", "--reflink=auto"]
    if subprocess.run(command + [str(source), str(target)], capture_output=True).returncode:
        shutil.copytree(source, target, symlinks=True)


def installed(work: Path) -> list[str]:
    """Top-level ignored directories, recorded right after setup: the installed dependencies (node_modules, .venv)."""
    entries = git(work, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory").splitlines()
    return sorted(e.rstrip("/") for e in entries if e.endswith("/") and "/" not in e.rstrip("/"))


def checkout(work: Path, revision: str, target: Path, dependencies: list[str] | None = None) -> Path:
    """A clean checkout of ``revision`` with only the installed dependency directories alongside, so nothing else
    untracked in the workspace can make a check pass."""
    if target.exists():
        shutil.rmtree(target)
    git(work.parent, "clone", "-q", "--no-checkout", str(work), str(target))
    git(target, "checkout", "-q", revision)
    for entry in installed(work) if dependencies is None else dependencies:
        if (work / entry).is_dir() and not (target / entry).exists():
            clone_cow(work / entry, target / entry)
    return target


def test_scripts(work: Path, revision: str) -> dict:
    """The package.json scripts that run tests (test, test:*, pretest...) at a revision."""
    try:
        scripts = json.loads(git(work, "show", f"{revision}:package.json")).get("scripts", {})
    except (RuntimeError, ValueError):
        return {}
    return {k: v for k, v in scripts.items() if "test" in k}


def test_configuration(repo: Repo, work: Path, base: str, commit: str, rows: list) -> list[str]:
    """Changes to what decides which tests run. The agent must not be able to make the gate easier."""
    changed = [p for s, p in rows if repo.test_config(p)]
    if any(p == "package.json" for _, p in rows) and test_scripts(work, base) != test_scripts(work, commit):
        changed.append("package.json test scripts")
    return changed


def run_gates(repo: Repo, work: Path, base: str, sandbox: Sandbox, scratch: Path, dependencies: list[str] | None = None,
              allow_test_config: bool = False) -> dict:
    """``dependencies``: the ignored directories setup installed; anything else untracked is left out of the checks.
    ``allow_test_config``: a person allowed this ticket to change test configuration."""
    dependencies = [] if dependencies is None else dependencies
    rows = changes(work, base)
    commit = snapshot(work, base)
    result = checkout(work, commit, scratch / "result", dependencies)
    tests = [(s, p) for s, p in rows if repo.is_test(p)]
    sources = [p for s, p in rows if not repo.is_test(p)]
    checks = [
        check("changes tests", bool(tests), "" if tests else "no test file was added or changed"),
        check("changes source", bool(sources), "" if sources else "no non-test file was changed"),
        check("keeps existing tests", all(s != "D" for s, _ in tests),
              ", ".join(p for s, p in tests if s == "D") and "deleted: " + ", ".join(p for s, p in tests if s == "D")),
    ]
    protected = [p for _, p in rows if repo.protected(p)]
    checks.append(check("leaves protected paths alone", not protected, ", ".join(protected)))
    generated = [p for s, p in rows if s != "D" and repo.generated(p)]
    checks.append(check("adds no generated or local files", not generated, ", ".join(generated)))
    config = test_configuration(repo, work, base, commit, rows)
    checks.append(check("leaves test configuration alone", allow_test_config or not config,
                        ("changed: " + ", ".join(config) + ". Test configuration decides what the gates run; change the "
                         "tests and source instead, or ask a person to allow it on the ticket") if config else ""))

    live = [p for s, p in tests if s != "D"]
    if live:
        before = checkout(work, base, scratch / "baseline", dependencies)
        for path in live:
            (before / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result / path, before / path)
        whole_suite = [p for p in live if repo.runner(p) is None]
        for path in live:
            command = repo.runner(path)
            if not command:
                continue
            code_before, out_before, _ = sandbox.run(command, cwd=before, timeout=repo.run_timeout)
            code_after, out_after, _ = sandbox.run(command, cwd=result, timeout=repo.run_timeout)
            detail = (f"on base: exit {code_before}\n{clip(out_before, LOG_CHARS)}\n\non result: exit {code_after}\n"
                      f"{clip(out_after, LOG_CHARS)}")
            checks.append(check(f"{path} fails on base, passes on result", code_before != 0 and code_after == 0, detail))
        if whole_suite and any(not _helper(p) for p in whole_suite):
            # The failure must come from the new tests: the untouched base passes, base plus new tests fails.
            pure = checkout(work, base, scratch / "base", dependencies)
            code_pure, out_pure, _ = sandbox.run(repo.test_command, cwd=pure, timeout=repo.run_timeout * 3)
            code_before, out_before, _ = sandbox.run(repo.test_command, cwd=before, timeout=repo.run_timeout * 3)
            added = (failed_tests(out_before) or set()) - (failed_tests(out_pure) or set())
            checks.append(check("new tests fail on base", code_before != 0 and (code_pure == 0 or bool(added)),
                                f"base alone: exit {code_pure}\n{clip(out_pure, LOG_CHARS // 2)}\n\n{', '.join(whole_suite)} on the base "
                                f"source: exit {code_before}\n{clip(out_before, LOG_CHARS)}"))
            shutil.rmtree(pure, ignore_errors=True)
    code, out, seconds = sandbox.run(repo.test_command, cwd=result, timeout=repo.run_timeout * 3)
    detail = f"{repo.test_command}: exit {code} in {seconds:.0f}s\n{clip(out, LOG_CHARS)}"
    ok = code == 0
    if not ok and code != 124:
        # A base that already fails would block every ticket: pass only when every failing test already failed on the
        # base. When the failing tests cannot be named, the gate stays strict.
        pure = checkout(work, base, scratch / "base", dependencies)
        code_base, out_base, _ = sandbox.run(repo.test_command, cwd=pure, timeout=repo.run_timeout * 3)
        shutil.rmtree(pure, ignore_errors=True)
        now, then = failed_tests(out), failed_tests(out_base)
        if code_base != 0 and now and then is not None and now <= then:
            ok = True
            detail = (f"{repo.test_command}: exit {code}; every failing test already failed on the base, so none is new:\n"
                      + "\n".join(sorted(now)))
        elif code_base != 0 and now and then is not None:
            detail += "\n\nNew failures (not failing on the base):\n" + "\n".join(sorted(now - then))
    checks.append(check("test suite passes", ok, detail))
    if repo.build_command:
        code, out, seconds = sandbox.run(repo.build_command, cwd=result, timeout=repo.run_timeout * 2)
        checks.append(check("build passes", code == 0, f"{repo.build_command}: exit {code} in {seconds:.0f}s\n{clip(out, LOG_CHARS)}"))
    patch = git(work, "diff", "--no-renames", base, commit)
    (scratch / "change.diff").write_text(patch)
    for directory in ("baseline", "result"):
        shutil.rmtree(scratch / directory, ignore_errors=True)
    return {"base": base, "commit": commit, "tree": git(work, "rev-parse", f"{commit}^{{tree}}").strip(), "changed": rows,
            "tests": [p for _, p in tests], "sources": sources, "checks": checks, "passed": all(c["ok"] for c in checks)}


FAILED_TEST = [
    re.compile(r"^(?:FAIL|ERROR): \S+ \(([\w.]+)\)", re.M),                 # unittest
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.M),                        # pytest -rA / short summary
    re.compile(r"^\s*[✖✕×] (.+?)(?: \([\d.]+ ?m?s\))?$", re.M),               # node --test, jest, vitest
]


def failed_tests(output: str) -> set[str] | None:
    """The failing tests a run names, or None when it names none (the caller then stays strict)."""
    found = {m.strip() for pattern in FAILED_TEST for m in pattern.findall(output)}
    found -= {"failing tests:"}
    return found or None


def _helper(path: str) -> bool:
    """Fixtures and data under a tests directory are not tests in themselves."""
    return Path(path).suffix in (".json", ".txt", ".csv", ".yaml", ".yml", ".snap", ".md", ".html")


def summary(report: dict) -> str:
    return "\n".join(f"- {'PASS' if c['ok'] else 'FAIL'} {c['name']}" for c in report["checks"])


def failures(report: dict) -> str:
    return "\n\n".join(f"FAILED: {c['name']}\n{c['detail']}" for c in report["checks"] if not c["ok"])
