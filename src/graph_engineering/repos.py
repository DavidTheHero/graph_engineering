"""Repository registration: where a repository lives and how to set it up, test it, build it and publish to it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import fnmatch
import json
from pathlib import Path
import re
import shlex
import subprocess

NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TEST_FILES = r"(^|/)(tests?|__tests__)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
TEST_CONFIG = ("pytest.ini", "conftest.py", "*/conftest.py", "tox.ini", "noxfile.py", "jest.config.*", "*/jest.config.*",
               "vitest.config.*", "*/vitest.config.*", "vitest.workspace.*", ".mocharc*", "karma.conf.*", "phpunit.xml*")
GENERATED = ("node_modules/*", "*/node_modules/*", ".venv/*", "dist/*", "*/dist/*", "*.db", "*.sqlite", "*.sqlite3", ".env", ".env.*")


@dataclass
class Repo:
    name: str
    path: str
    base: str = "main"
    slug: str | None = None                 # owner/name on GitHub; None keeps publication local
    test_command: str = ""
    build_command: str = ""
    setup: list = field(default_factory=list)          # shell commands run once per workspace, network allowed
    env: dict = field(default_factory=dict)            # extra sandbox environment; "{repo}" expands to the path
    test_file_pattern: str = TEST_FILES
    test_file_runners: list = field(default_factory=list)   # [{"pattern", "command" with {file}}]; else the full suite
    protected_paths: list = field(default_factory=lambda: [".github/*"])
    test_config_paths: list = field(default_factory=lambda: list(TEST_CONFIG))   # decide which tests run: guarded
    trusted: bool = False                   # allow running without an OS sandbox
    auto_accept: bool = False               # tickets the intake agent marks ready start development without a person
    auto_publish: bool = True               # open the PR as soon as gates and review pass (merge always needs a person)
    issue_required: bool = True             # create a GitHub issue and reference it with "Closes #N"
    branch_template: str = "{kind}/{issue}-{slug}"
    max_rounds: int = 3
    coding_steps: int = 80
    run_timeout: int = 300

    def __post_init__(self):
        if not NAME.fullmatch(self.name):
            raise ValueError("repository name must be 1-64 letters, digits, '.', '_' or '-'")
        if self.slug and not SLUG.fullmatch(self.slug):
            raise ValueError("slug must look like owner/name")
        if not self.test_command:
            raise ValueError("a test command is required")
        re.compile(self.test_file_pattern)

    def expand(self, value: str) -> str:
        return value.replace("{repo}", self.path)

    def sandbox_env(self) -> dict:
        return {k: self.expand(v) for k, v in self.env.items()}

    def is_test(self, path: str) -> bool:
        return bool(re.search(self.test_file_pattern, path))

    def runner(self, path: str) -> str | None:
        for item in self.test_file_runners:
            if re.search(item["pattern"], path):
                return item["command"].replace("{file}", shlex.quote(path))
        return None

    def test_config(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.test_config_paths)

    def protected(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.protected_paths)

    @staticmethod
    def generated(path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in GENERATED) and path != ".env.example"

    def public(self) -> dict:
        return asdict(self)


def detect(path: Path) -> dict:
    """Best-effort commands for a checkout; the person reviews them at registration."""
    path = Path(path)
    found: dict = {}
    if (path / "package.json").exists():
        scripts = json.loads((path / "package.json").read_text()).get("scripts", {})
        lock = (path / "package-lock.json").exists()
        found["setup"] = ["npm ci --ignore-scripts --no-audit --no-fund" if lock else "npm install --ignore-scripts --no-audit --no-fund"]
        found["test_command"] = "npm test" if "test" in scripts else ""
        found["build_command"] = "npm run build" if "build" in scripts else ""
        runners = []
        if "node --test" in " ".join(scripts.values()):
            runners.append({"pattern": r"\.test\.[cm]?js$", "command": "node --test {file}"})
        if "vitest" in " ".join(scripts.values()):
            config = re.search(r"vitest[^&|]*?--config\s+(\S+)", " ".join(scripts.values()))
            runners.append({"pattern": r"\.(test|spec)\.[jt]sx$" if runners else r"\.(test|spec)\.[cm]?[jt]sx?$",
                            "command": "npx vitest run " + (f"--config {config.group(1)} " if config else "") + "{file}"})
        found["test_file_runners"] = runners
    elif (path / "pyproject.toml").exists() or (path / "setup.py").exists():
        text = (path / "pyproject.toml").read_text() if (path / "pyproject.toml").exists() else ""
        if "pytest" in text:
            found["test_command"] = "python3 -m pytest -q"
            found["test_file_runners"] = [{"pattern": r"(^|/)test_[^/]*\.py$|_test\.py$", "command": "python3 -m pytest -q {file}"}]
        else:
            start = "tests" if (path / "tests").is_dir() else "."
            found["test_command"] = f"python3 -m unittest discover -s {start} -q"
    try:
        branch = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=path,
                                capture_output=True, text=True).stdout.strip()
        found["base"] = branch.split("/", 1)[1] if "/" in branch else "main"
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=path, capture_output=True, text=True).stdout.strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", remote)
        if match:
            found["slug"] = match.group(1)
    except OSError:
        pass
    return found


class Repos:
    def __init__(self, home: Path):
        self.dir = Path(home) / "repos"
        self.dir.mkdir(parents=True, exist_ok=True)

    def get(self, name: str) -> Repo:
        if not NAME.fullmatch(name or ""):
            raise KeyError(name)
        path = self.dir / f"{name}.json"
        if not path.exists():
            raise KeyError(name)
        return Repo(**json.loads(path.read_text()))

    def all(self) -> list[Repo]:
        return [Repo(**json.loads(p.read_text())) for p in sorted(self.dir.glob("*.json"))]

    def save(self, repo: Repo) -> Repo:
        if not (Path(repo.path) / ".git").exists():
            raise ValueError(f"{repo.path} is not a git checkout")
        (self.dir / f"{repo.name}.json").write_text(json.dumps(repo.public(), indent=2) + "\n")
        return repo
