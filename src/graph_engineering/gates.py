"""The harness's own judgement of a workspace. Nothing here trusts what an agent said it did."""
from __future__ import annotations

from pathlib import Path
import platform
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
    rows = []
    for line in git(work, "diff", "--cached", "--name-status", "--no-renames", base).splitlines():
        status, path = line.split("\t", 1)
        rows.append((status[0], path))
    return rows


def diff(work: Path, base: str) -> str:
    git(work, "add", "-A")
    return git(work, "diff", "--cached", "--no-renames", base)


def clone_cow(source: Path, target: Path) -> None:
    """Copy a directory copy-on-write where the filesystem allows it."""
    command = ["cp", "-cR"] if platform.system() == "Darwin" else ["cp", "-a", "--reflink=auto"]
    if subprocess.run(command + [str(source), str(target)], capture_output=True).returncode:
        shutil.copytree(source, target, symlinks=True)


def baseline(work: Path, base: str, target: Path) -> Path:
    """The base revision with the workspace's installed, ignored directories (node_modules, .venv) alongside."""
    if target.exists():
        shutil.rmtree(target)
    git(work.parent, "clone", "-q", "--no-checkout", str(work), str(target))
    git(target, "checkout", "-q", base)
    ignored = git(work, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory").splitlines()
    for entry in ignored:
        entry = entry.rstrip("/")
        if entry and "/" not in entry and (work / entry).is_dir() and not (target / entry).exists():
            clone_cow(work / entry, target / entry)
    return target


def run_gates(repo: Repo, work: Path, base: str, sandbox: Sandbox, scratch: Path) -> dict:
    rows = changes(work, base)
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

    live = [p for s, p in tests if s != "D"]
    if live:
        before = baseline(work, base, scratch / "baseline")
        for path in live:
            (before / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / path, before / path)
        whole_suite = [p for p in live if repo.runner(p) is None]
        for path in live:
            command = repo.runner(path)
            if not command:
                continue
            code_before, out_before, _ = sandbox.run(command, cwd=before, timeout=repo.run_timeout)
            code_after, out_after, _ = sandbox.run(command, cwd=work, timeout=repo.run_timeout)
            detail = (f"on base: exit {code_before}\n{clip(out_before, LOG_CHARS)}\n\non result: exit {code_after}\n"
                      f"{clip(out_after, LOG_CHARS)}")
            checks.append(check(f"{path} fails on base, passes on result", code_before != 0 and code_after == 0, detail))
        if whole_suite and any(not _helper(p) for p in whole_suite):
            code_before, out_before, _ = sandbox.run(repo.test_command, cwd=before, timeout=repo.run_timeout * 3)
            checks.append(check("new tests fail on base", code_before != 0,
                                f"{', '.join(whole_suite)} with the base source: exit {code_before}\n{clip(out_before, LOG_CHARS)}"))
    code, out, seconds = sandbox.run(repo.test_command, timeout=repo.run_timeout * 3)
    checks.append(check("test suite passes", code == 0, f"{repo.test_command}: exit {code} in {seconds:.0f}s\n{clip(out, LOG_CHARS)}"))
    if repo.build_command:
        code, out, seconds = sandbox.run(repo.build_command, timeout=repo.run_timeout * 2)
        checks.append(check("build passes", code == 0, f"{repo.build_command}: exit {code} in {seconds:.0f}s\n{clip(out, LOG_CHARS)}"))
    return {"base": base, "changed": rows, "tests": [p for _, p in tests], "sources": sources,
            "checks": checks, "passed": all(c["ok"] for c in checks)}


def _helper(path: str) -> bool:
    """Fixtures and data under a tests directory are not tests in themselves."""
    return Path(path).suffix in (".json", ".txt", ".csv", ".yaml", ".yml", ".snap", ".md", ".html")


def summary(report: dict) -> str:
    return "\n".join(f"- {'PASS' if c['ok'] else 'FAIL'} {c['name']}" for c in report["checks"])


def failures(report: dict) -> str:
    return "\n\n".join(f"FAILED: {c['name']}\n{c['detail']}" for c in report["checks"] if not c["ok"])
