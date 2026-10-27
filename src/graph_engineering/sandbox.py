"""The untrusted zone: every model action and every target-repository command runs here.

Writes are confined to the run directory, credentials are unreadable, the environment is minimal,
and the network is closed except for localhost (setup may open it to install dependencies).
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile
import threading
import time

SECRETS = (".ssh", ".config/gh", ".aws", ".azure", ".config/gcloud", ".docker", ".kube", ".gnupg", ".netrc",
           ".git-credentials", ".npmrc", ".pypirc")
# Under the person's home only these (when present), the PATH entries and a repository's `readable` paths are
# readable: everything else there (keychains, shell history, other projects' .env files) is not.
TOOLCHAINS = (".nvm", ".pyenv", ".rbenv", ".nodenv", ".goenv", ".jenv", ".asdf", ".volta", ".bun", ".deno", ".cargo",
              ".rustup", "go", ".npm-global", ".local", ".sdkman", ".m2", ".gradle", ".ghcup", ".cabal", ".opam",
              ".dotnet", ".conda", "miniconda3", "anaconda3", ".pub-cache", ".nuget")
DNS_SOCKET = "/private/var/run/mDNSResponder"   # name resolution on macOS: the one unix socket a networked command needs
CONTAINER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock", "/run/user/{uid}/podman/podman.sock")
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TZ")


class SandboxUnavailable(RuntimeError):
    pass


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + f"\n[... {len(text) - limit} characters omitted ...]\n" + text[-(limit - head):]


@functools.cache
def available() -> str | None:
    """The sandbox mechanism on this host, or None. It is tried once, not just found: inside another sandbox (an
    eval of this project's own history runs its tests in one) sandbox-exec exists but cannot apply a profile."""
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        probe, mechanism = ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"], "sandbox-exec"
    elif platform.system() == "Linux" and shutil.which("bwrap"):
        probe, mechanism = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "/bin/true"], "bwrap"
    else:
        return None
    try:
        return mechanism if subprocess.run(probe, capture_output=True, timeout=20).returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


CLOSED_PORTS: set[int] = set()   # loopback ports the sandbox must not reach (the control centre registers its own)


def temp_roots() -> list[Path]:
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    if platform.system() == "Darwin":
        roots.add(Path("/private/var/folders"))
    return sorted(roots)


class Sandbox:
    """``run_dir`` is the only directory the sandbox may write (plus system temp, since not everything honours
    TMPDIR). The person's home is unreadable but for the run directory, system temp, toolchains and ``readable``
    paths; ``private`` directories are neither readable nor writable, except the run directory when it lies
    inside one (the state directory holds every ticket's box). ``profiles`` holds the sandbox profiles and must
    not be writable from inside, or a command could loosen the profile of the next. ``work/.git`` is read-only
    inside the sandbox: the controller runs git in the workspace, and hooks or configuration planted there
    would run outside the sandbox (with sandbox-exec only the index may change, so `git checkout -- file` still
    works; under bwrap the whole of .git is read-only). Unix sockets are reachable only inside the run directory
    and temp: a container daemon's socket would mount the whole disk."""

    def __init__(self, run_dir: Path, work: Path, env: dict | None = None, trusted: bool = False,
                 profiles: Path | None = None, private: tuple = (), readable: tuple = ()):
        self.run_dir, self.work = Path(run_dir).resolve(), Path(work).resolve()
        self.home, self.tmp = self.run_dir / "home", self.run_dir / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.profiles = Path(profiles).resolve() if profiles else self.run_dir.parent / "profiles"
        if self.profiles == self.run_dir or self.run_dir in self.profiles.parents:
            raise ValueError("sandbox profiles must live outside the run directory")
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.private = [Path(p).resolve() for p in private]
        self.extra = [Path(p).resolve() for p in readable]
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

    def readable(self) -> list[Path]:
        """What a command may read under the person's home besides the run directory: the PATH entries there (with
        the directory holding a `bin`, where their links lead), the toolchains that exist, the repository's
        ``readable`` paths and system temp. Never the home itself."""
        user = Path.home().resolve()
        roots = set(self.extra)
        for entry in self.env.get("PATH", "").split(os.pathsep):
            path = Path(entry)
            if path.is_absolute() and path.resolve().is_relative_to(user):
                roots.add(path.resolve())
                if path.name == "bin":
                    roots.add(path.resolve().parent)
        roots.update(user / name for name in TOOLCHAINS if (user / name).exists())
        roots.update(temp_roots())
        return sorted(p for p in roots if p != user)

    def _profile(self, network: bool) -> Path:
        """A sandbox-exec profile. The last matching rule wins, so the order is: nothing is writable; system temp
        and the device files are; the person's home is unreadable; the toolchains and readable paths are readable;
        private directories are neither readable nor writable; the run directory is both; the workspace's .git is
        not writable except its index; the secrets are unreadable. Outbound unix sockets only inside the run
        directory and temp (plus the resolver when the network is open); loopback ports the controller owns are
        closed (the control centre takes merge approvals: the sandbox must not reach it)."""
        user = Path.home()
        subpaths = lambda paths: " ".join(f'(subpath "{p}")' for p in paths)
        enclosing = [p for p in self.private if p in self.run_dir.parents]
        apart = [p for p in self.private if p != self.run_dir and p not in enclosing]
        rules = ["(version 1)", "(allow default)",
                 "(deny file-write*)",
                 "(allow file-write* " + subpaths(temp_roots())
                 + ' (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
                 f'(deny file-read* (subpath "{user}"))',
                 "(allow file-read* " + subpaths(self.readable()) + ")"]
        if enclosing or apart:
            rules.append("(deny file-read* file-write* " + subpaths(enclosing + apart) + ")")
        # Programs resolve real paths by stat-ing every ancestor (node, git): the ancestors of what is readable
        # may be stat-ed, which tells their names and dates, not their contents or listings.
        ancestors = {a for p in [self.run_dir, *self.readable()] for a in p.parents}
        rules += [f'(allow file-read* file-write* (subpath "{self.run_dir}"))',
                  "(allow file-read-metadata " + " ".join(f'(literal "{a}")' for a in sorted(ancestors)) + ")",
                  f'(deny file-write* (subpath "{self.work / ".git"}"))',
                  f'(allow file-write* (literal "{self.work / ".git" / "index"}") (literal "{self.work / ".git" / "index.lock"}"))',
                  "(deny file-read* " + subpaths(user / s for s in SECRETS) + ")"]
        if not network:
            rules += ["(deny network-outbound)", '(allow network-outbound (remote ip "localhost:*"))',
                      '(allow network-bind (local ip "localhost:*"))']
        else:
            rules += ["(deny network-outbound (remote unix-socket))", f'(allow network-outbound (literal "{DNS_SOCKET}"))']
        rules += ["(allow network-outbound " + subpaths([self.run_dir, *temp_roots()]) + ")"]
        rules += [f'(deny network-outbound (remote ip "localhost:{port}"))' for port in sorted(CLOSED_PORTS)]
        path = self.profiles / (f"{self.run_dir.name}-net.sb" if network else f"{self.run_dir.name}.sb")
        path.write_text("\n".join(rules) + "\n")
        return path

    def command(self, command: str, network: bool = False) -> list[str]:
        # pipefail: `npm test 2>&1 | tail -25` otherwise reports tail's exit code, and a failing suite reads as passing.
        shell = ["/bin/bash", "-o", "pipefail", "-c", command] if Path("/bin/bash").exists() else ["/bin/sh", "-c", command]
        if self.mechanism == "sandbox-exec":
            return ["sandbox-exec", "-f", str(self._profile(network))] + shell
        if self.mechanism == "bwrap":
            # The same policy as the profile, as mounts in order: the disk read-only, the home empty, the readable
            # paths back, private directories empty, the run directory writable, .git read-only, sockets masked.
            user = Path.home().resolve()
            wrap = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(user)]
            wrap += [arg for p in self.readable() if p.exists() and user in p.parents for arg in ("--ro-bind", str(p), str(p))]
            wrap += [arg for p in self.private if p.exists() and p != self.run_dir for arg in ("--tmpfs", str(p))]
            wrap += ["--bind", str(self.run_dir), str(self.run_dir)]
            if (self.work / ".git").exists():
                wrap += ["--ro-bind", str(self.work / ".git"), str(self.work / ".git")]
            for secret in SECRETS:
                target = Path.home() / secret
                if target.is_dir():
                    wrap += ["--tmpfs", str(target)]
                elif target.exists():
                    wrap += ["--ro-bind", "/dev/null", str(target)]
            for socket in CONTAINER_SOCKETS:
                if Path(socket.format(uid=os.getuid())).exists():
                    wrap += ["--ro-bind", "/dev/null", socket.format(uid=os.getuid())]
            wrap += ["--die-with-parent", "--new-session"]
            if not network:
                wrap.append("--unshare-net")
            return wrap + shell
        return shell

    def run(self, command: str, cwd: Path | None = None, timeout: int = 300, network: bool = False) -> tuple[int, str, float]:
        """Run ``command`` and return (exit code, output, seconds). The command ends when its shell exits: anything it
        left running in the background is stopped then, so `npm start &` does not hold the output open until the
        timeout (run 9: four 138-second timeouts on one ticket) or keep a port busy for later commands."""
        started = time.time()
        proc = subprocess.Popen(self.command(command, network), cwd=cwd or self.work, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: proc.stdout.read1(65536), b"")), daemon=True)
        reader.start()
        note = b""
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code, note = 124, f"\n[killed after {timeout}s]".encode()
        reader.join(timeout=1)
        try:
            os.killpg(proc.pid, 0)            # raises when nothing of this command's session is left
            os.killpg(proc.pid, signal.SIGKILL)
            if code != 124:
                note = (b"\n[background processes this command started were stopped when it ended; start and use a "
                        b"server within one command]")
        except (ProcessLookupError, PermissionError):
            pass
        reader.join(timeout=5)
        proc.wait()
        return code, (b"".join(chunks) + note).decode("utf-8", "replace"), time.time() - started


GIT = shutil.which("git") or "git"   # resolved once, at import, before any sandboxed command runs
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "advice.detachedHead=false")


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """The controller's git: no hooks, no fsmonitor, no system config."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run([GIT, *SAFE_GIT, *args], cwd=cwd, capture_output=True, text=True, env=env)
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
