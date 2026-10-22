"""A repository map for the opening brief: the files that matter for a ticket, each with its main symbols.

After Aider's repo map: symbols per file, files ranked over the import graph with a bias toward the ticket's
words, trimmed to a token budget. The agent then reads only what it needs. Standard library only, so symbols
come from regular expressions for Python and JavaScript/TypeScript rather than a parser.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path, PurePosixPath
import re
import subprocess

SOURCE = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
MAX_FILE_BYTES = 200_000
PY_SYMBOL = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.M)
JS_SYMBOL = re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)
PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import|import\s+([\w.]+))", re.M)
JS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\s*\(\s*|^import\s+)["'](\.{1,2}/[^"']+)["']""", re.M)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
STOP = {"the", "and", "for", "that", "this", "with", "from", "are", "was", "not", "but", "its", "has", "have", "when",
        "into", "each", "can", "all", "any", "should", "must", "which", "their", "returns", "return", "name", "new",
        "use", "uses", "used", "file", "files", "code", "test", "tests", "function", "value", "true", "false", "null"}


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if PurePosixPath(p).suffix in SOURCE and "node_modules/" not in p]


def symbols(text: str, suffix: str) -> list[str]:
    pattern = PY_SYMBOL if suffix == ".py" else JS_SYMBOL
    seen = []
    for name in pattern.findall(text):
        if name not in seen and not name.startswith("_"):
            seen.append(name)
    return seen


def resolve(source: str, spec: str, files: set[str]) -> str | None:
    """A relative JS import or a Python module name, as a tracked file."""
    if spec.startswith("."):
        if "/" in spec or spec.startswith("./") or spec.startswith("../"):
            base = PurePosixPath(source).parent / spec
            candidates = [str(base)] + [f"{base}{s}" for s in SOURCE] + [f"{base}/index{s}" for s in SOURCE]
        else:  # Python relative import: "." is the package, ".." its parent
            depth = len(spec) - len(spec.lstrip("."))
            base = PurePosixPath(source).parent
            for _ in range(depth - 1):
                base = base.parent
            rest = spec.lstrip(".").replace(".", "/")
            candidates = [f"{base}/{rest}.py", f"{base}/{rest}/__init__.py"] if rest else [f"{base}/__init__.py"]
    else:
        path = spec.replace(".", "/")
        candidates = [f"{path}.py", f"{path}/__init__.py", f"src/{path}.py", f"src/{path}/__init__.py"]
    for candidate in candidates:
        normal = str(PurePosixPath(*[p for p in PurePosixPath(candidate).parts])).replace("/./", "/")
        parts = []
        for part in PurePosixPath(normal).parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        candidate = "/".join(parts)
        if candidate in files:
            return candidate
    return None


def build(root: Path, query: str = "", budget_chars: int = 4500) -> str:
    root = Path(root)
    files = tracked(root)
    if not files:
        return ""
    present = set(files)
    words = {w.lower() for w in WORD.findall(query)} - STOP
    relevance: dict[str, int] = {}
    syms: dict[str, list[str]] = {}
    edges: dict[str, set[str]] = defaultdict(set)
    for path in files:
        target = root / path
        try:
            if target.is_symlink() or target.stat().st_size > MAX_FILE_BYTES:
                continue
            text = target.read_text(errors="replace")
        except OSError:
            continue
        suffix = PurePosixPath(path).suffix
        syms[path] = symbols(text, suffix)
        relevance[path] = len(words & {w.lower() for w in WORD.findall(text)}) if words else 0
        imports = [a or b for a, b in PY_IMPORT.findall(text)] if suffix == ".py" else JS_IMPORT.findall(text)
        for spec in imports:
            found = resolve(path, spec, present)
            if found and found != path:
                edges[path].add(found)
    rank = pagerank(list(syms), edges, personal(syms, query))
    top = max(rank.values()) or 1.0
    # The ticket's words, found in the file, decide first; the import graph breaks ties and lifts central files.
    score = {p: (1 + relevance.get(p, 0)) ** 2 * (0.5 + rank[p] / top) for p in syms}
    lines, size = [], 0
    for path in sorted(syms, key=lambda p: -score[p]):
        names = syms[path][:12]
        line = f"{path}: {', '.join(names)}" if names else path
        if size + len(line) > budget_chars:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def personal(syms: dict[str, list[str]], query: str) -> dict[str, float]:
    """Bias toward files whose path or symbols share words with the ticket; tests count a little less."""
    words = {w.lower() for w in WORD.findall(query)}
    weights = {}
    for path, names in syms.items():
        tokens = {w.lower() for w in WORD.findall(path + " " + " ".join(names))}
        tokens |= {t.lower() for n in names for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", n) if len(t) > 2}
        score = 1.0 + 5.0 * len(words & tokens)
        if re.search(r"(^|/)(tests?|__tests__)/|\.test\.|\.spec\.|(^|/)test_", path):
            score *= 0.5
        weights[path] = score
    return weights


def pagerank(nodes: list[str], edges: dict[str, set[str]], personalization: dict[str, float],
             damping: float = 0.85, rounds: int = 30) -> dict[str, float]:
    """A file imported by important files is important; teleports follow the ticket's words."""
    total = sum(personalization.get(n, 1.0) for n in nodes) or 1.0
    teleport = {n: personalization.get(n, 1.0) / total for n in nodes}
    rank = dict(teleport)
    for _ in range(rounds):
        nxt = {n: (1 - damping) * teleport[n] for n in nodes}
        for source in nodes:
            targets = [t for t in edges.get(source, ()) if t in nxt]
            share = rank[source] * damping
            if targets:
                for target in targets:
                    nxt[target] += share / len(targets)
            else:
                for n in nodes:
                    nxt[n] += share * teleport[n]
        rank = nxt
    return rank
