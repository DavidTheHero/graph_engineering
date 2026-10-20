"""Repository ecosystem adapters. Detection reads tracked files only; it never runs repository code.

Each detector inspects committed metadata and returns the argv commands the
controller *may* run once the operator trusts local execution. A repository
without a recognised ecosystem still registers through the generic adapter:
read-only inspection and natural-language investigation need no test command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None


@dataclass(frozen=True)
class Adapter:
    name: str
    test_command: list[str] | None
    build_commands: list[list[str]]
    source_suffixes: tuple[str, ...]
    package_manager: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# Union of every adapter's editable source suffixes plus common languages without
# a dedicated adapter. Used for retrieval and as the generic adapter's scope.
CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".kts",
                 ".scala", ".cs", ".fs", ".rb", ".php", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".m",
                 ".lua", ".sh", ".ex", ".exs", ".erl", ".dart", ".vue", ".svelte", ".sql", ".zig")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".json", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".gradle", ".xml")
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testing", "e2e", "integration_tests", "testdata"}


def is_test(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    return (name.startswith(("test", "spec")) or bool(re.search(r"[._-](?:test|spec)s?[._-]", name))
            # JVM/.NET/Swift/PHP convention: FooTest.java, FooTests.cs, FooSpec.scala (case-sensitive, so
            # Latest.kt and Contest.php stay source files).
            or bool(re.search(r"[a-z0-9](?:Test|Tests|Spec)\.(?:java|kt|cs|scala|swift|php)$", p.name))
            or bool({part.lower() for part in p.parts[:-1]} & TEST_DIRS))


def _read(repo: Path, name: str, limit: int = 200_000) -> str:
    path = repo / name
    if path.is_symlink() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")[:limit]


def _npm_prefix(manager: str) -> list[str]:
    executable = shutil.which(manager)
    # Windows batch shims are not passed to a shell. Invoke npm's JS CLI with Node.
    if os.name == "nt" and manager == "npm" and executable:
        npm_js = Path(executable).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if npm_js.exists():
            return [shutil.which("node") or "node", str(npm_js)]
    return [manager]


def javascript(repo: Path, files: set[str]) -> Adapter | None:
    if "package.json" not in files:
        return None
    try:
        manifest = json.loads(_read(repo, "package.json") or "{}")
    except ValueError:
        manifest = {}
    scripts = manifest.get("scripts", {}) if isinstance(manifest.get("scripts"), dict) else {}
    manager = next((m for lock, m in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
                                      ("bun.lock", "bun")) if lock in files), "npm")
    prefix = _npm_prefix(manager)
    test = ["node", "--test"] if scripts.get("test") == "node --test" or "test" not in scripts else prefix + ["test"]
    builds = [prefix + ["run", name] for name in ("build", "lint", "typecheck") if name in scripts]
    return Adapter("javascript", test, builds, (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"), manager)


def python(repo: Path, files: set[str]) -> Adapter | None:
    if not any(f.endswith(".py") for f in files):
        return None
    pyproject = _read(repo, "pyproject.toml")
    try:
        configured = "pytest" in tomllib.loads(pyproject).get("tool", {}) if pyproject and tomllib else "[tool.pytest" in pyproject
    except ValueError:
        configured = False
    pytest = ("pytest.ini" in files or "conftest.py" in files or configured
              or any(is_test(f) and f.endswith(".py") and "import pytest" in _read(repo, f, 12000) for f in files))
    manager = "uv" if "uv.lock" in files else "poetry" if "poetry.lock" in files else "pip"
    # The controller interpreter runs tests; the operator provisions dependencies into it or overrides argv.
    if pytest:
        test = [sys.executable, "-m", "pytest", "-q"]
    elif any(f.startswith("tests/") and f.endswith(".py") for f in files):
        test = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    else:
        test = [sys.executable, "-m", "unittest", "discover", "-v"]
    return Adapter("python", test, [], (".py",), manager)


def go(repo: Path, files: set[str]) -> Adapter | None:
    if "go.mod" not in files:
        return None
    return Adapter("go", ["go", "test", "./..."], [["go", "vet", "./..."]], (".go",), "go")


def rust(repo: Path, files: set[str]) -> Adapter | None:
    if "Cargo.toml" not in files:
        return None
    return Adapter("rust", ["cargo", "test", "--locked"], [["cargo", "check", "--locked"]], (".rs",), "cargo")


def jvm(repo: Path, files: set[str]) -> Adapter | None:
    suffixes = (".java", ".kt", ".kts", ".scala")
    if "gradlew" in files:
        return Adapter("jvm-gradle", ["./gradlew", "test", "--no-daemon"], [], suffixes, "gradle")
    if "build.gradle" in files or "build.gradle.kts" in files:
        return Adapter("jvm-gradle", ["gradle", "test", "--no-daemon"], [], suffixes, "gradle")
    if "pom.xml" in files:
        return Adapter("jvm-maven", ["mvn", "-q", "-B", "test"], [], suffixes, "maven")
    return None


def dotnet(repo: Path, files: set[str]) -> Adapter | None:
    if not any(f.endswith((".sln", ".csproj", ".fsproj")) for f in files):
        return None
    return Adapter("dotnet", ["dotnet", "test"], [["dotnet", "build"]], (".cs", ".fs"), "dotnet")


def ruby(repo: Path, files: set[str]) -> Adapter | None:
    if "Gemfile" not in files:
        return None
    rspec = any(f.startswith("spec/") for f in files)
    return Adapter("ruby", ["bundle", "exec", "rspec"] if rspec else ["bundle", "exec", "rake", "test"], [], (".rb",), "bundler")


def make(repo: Path, files: set[str]) -> Adapter | None:
    if "Makefile" not in files or not re.search(r"^test\s*:", _read(repo, "Makefile"), re.M):
        return None
    return Adapter("make", ["make", "test"], [], CODE_SUFFIXES, "make",
                   ("Makefile test target; recipe is repository code and runs only when trusted",))


def generic(repo: Path, files: set[str]) -> Adapter:
    return Adapter("generic", None, [], CODE_SUFFIXES, None,
                   ("No recognised test runner; read-only investigation only until test_command is configured",))


# Ordered: the first match is the primary adapter; every match is reported.
DETECTORS = (javascript, python, go, rust, jvm, dotnet, ruby, make)


def detect(repo: Path, files: list[str]) -> tuple[Adapter, list[Adapter]]:
    """Return the primary adapter (its runner is the default test command) and every detected adapter.

    Polyglot repositories keep one default runner but their source scope is the
    union of all detected ecosystems, so investigation covers every language.
    """
    present = set(files)
    found = [adapter for detector in DETECTORS if (adapter := detector(Path(repo), present)) is not None]
    return (found[0] if found else generic(Path(repo), present)), found or [generic(Path(repo), present)]
