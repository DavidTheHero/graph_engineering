"""The untrusted zone: every model action and every target-repository command runs here.

Writes are confined to the run directory, credentials are unreadable, the environment is minimal,
and the network is closed except for localhost (setup may open it to install dependencies).
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time

SECRETS = (".ssh", ".config/gh", ".aws", ".azure", ".config/gcloud", ".docker", ".kube", ".gnupg", ".netrc",
           ".git-credentials", ".npmrc", ".pypirc")
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TZ")


class SandboxUnavailable(RuntimeError):
    pass


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + f"\n[... {len(text) - limit} characters omitted ...]\n" + text[-(limit - head):]


def available() -> str | None:
    """The sandbox mechanism on this host, or None."""
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if platform.system() == "Linux" and shutil.which("bwrap"):
        return "bwrap"
    return None


class Sandbox:
    def __init__(self, run_dir: Path, work: Path, env: dict | None = None, trusted: bool = False):
        self.run_dir, self.work = Path(run_dir).resolve(), Path(work).resolve()
        self.home, self.tmp = self.run_dir / "home", self.run_dir / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.mechanism = available()
        if not self.mechanism and not trusted:
            raise SandboxUnavailable("no OS sandbox (sandbox-exec or bwrap) on this host; register the repository as "
                                     "trusted to run without one")
        self.env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        self.env.update({"HOME": str(self.home), "TMPDIR": str(self.tmp), "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0",
                         "npm_config_cache": str(self.tmp / "npm"), "npm_config_update_notifier": "false",
                         "PIP_CACHE_DIR": str(self.tmp / "pip"), "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0",
                         # HOME is empty, so tests that commit need an identity (a neutral one, never the person's).
                         "GIT_AUTHOR_NAME": "graph_engineering", "GIT_AUTHOR_EMAIL": "graph-engineering@localhost",
                         "GIT_COMMITTER_NAME": "graph_engineering", "GIT_COMMITTER_EMAIL": "graph-engineering@localhost"})
        self.env.update(env or {})

    def _profile(self, network: bool) -> Path:
        user = Path.home()
        denied = " ".join(f'(subpath "{user / s}")' for s in SECRETS)
        rules = ["(version 1)", "(allow default)",
                 f'(deny file-write* (subpath "{user}"))',
                 f'(allow file-write* (subpath "{self.run_dir}"))',
                 f"(deny file-read* {denied})"]
        if not network:
            rules += ["(deny network-outbound)", '(allow network-outbound (remote ip "localhost:*"))',
                      '(allow network-outbound (remote unix-socket))', '(allow network-bind (local ip "localhost:*"))']
        path = self.run_dir / ("sandbox-net.sb" if network else "sandbox.sb")
        path.write_text("\n".join(rules) + "\n")
        return path

    def command(self, command: str, network: bool = False) -> list[str]:
        shell = ["/bin/sh", "-c", command]
        if self.mechanism == "sandbox-exec":
            return ["sandbox-exec", "-f", str(self._profile(network))] + shell
        if self.mechanism == "bwrap":
            wrap = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                    "--bind", str(self.run_dir), str(self.run_dir), "--die-with-parent", "--new-session"]
            for secret in SECRETS:
                target = Path.home() / secret
                if target.is_dir():
                    wrap += ["--tmpfs", str(target)]
                elif target.exists():
                    wrap += ["--ro-bind", "/dev/null", str(target)]
            if not network:
                wrap.append("--unshare-net")
            return wrap + shell
        return shell

    def run(self, command: str, cwd: Path | None = None, timeout: int = 300, network: bool = False) -> tuple[int, str, float]:
        started = time.time()
        proc = subprocess.Popen(self.command(command, network), cwd=cwd or self.work, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            out += f"\n[killed after {timeout}s]".encode()
            code = 124
        return code, out.decode("utf-8", "replace"), time.time() - started


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-c", "advice.detachedHead=false", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def prepare_workspace(repo: Path, base: str, work: Path) -> str:
    """A private clone of ``repo`` at ``base`` with no remote; returns the base commit."""
    sha = git(repo, "rev-parse", "--verify", base + "^{commit}").strip()
    work.parent.mkdir(parents=True, exist_ok=True)
    git(work.parent, "clone", "-q", "--no-checkout", str(repo), str(work))
    git(work, "checkout", "-q", sha)
    git(work, "remote", "remove", "origin")
    git(work, "config", "user.name", "graph_engineering")
    git(work, "config", "user.email", "graph-engineering@localhost")
    return sha
