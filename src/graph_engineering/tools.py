"""The agent-computer interface: the tools a model uses on a workspace.

Few tools, each doing one obvious thing, with errors that say how to recover. Paths are relative to
the workspace root and cannot leave it. Output is clipped so one call cannot flood the context.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shlex
import subprocess

from .sandbox import Sandbox, clip

OUTPUT_CHARS = 8000
READ_LINES = 400
READ_CHARS = 24000

SPECS = {
    "list_files": {"description": "List files under a directory (tracked and new files; gitignored files are skipped).",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory, default '.'"}}}},
    "read_file": {"description": f"Read a file with line numbers, at most {READ_LINES} lines per call; use start_line to page. "
                                 "The line numbers are not part of the file.",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                                                 "end_line": {"type": "integer"}}, "required": ["path"]}},
    "search": {"description": "Search file contents with an extended regular expression. Returns path:line:text.",
               "parameters": {"type": "object", "properties": {"pattern": {"type": "string"},
                                                              "path": {"type": "string", "description": "Limit to this directory or file"}},
                              "required": ["pattern"]}},
    "write_file": {"description": "Create a file or replace its entire content.",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                  "required": ["path", "content"]}},
    "edit_file": {"description": "Replace one exact occurrence of old_text with new_text. old_text must match the file exactly, "
                                 "including indentation, and occur once; include surrounding lines to make it unique.",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                                 "new_text": {"type": "string"}},
                                 "required": ["path", "old_text", "new_text"]}},
    "run": {"description": "Run a shell command at the repository root in a sandbox without network access. "
                           "Returns the exit code and output. Do not pipe test commands through tail or grep: you lose the exit code.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
}
READ_ONLY = ("list_files", "read_file", "search")
READ_WRITE = READ_ONLY + ("write_file", "edit_file", "run")


class Workspace:
    def __init__(self, root: Path, sandbox: Sandbox | None = None, run_timeout: int = 300):
        self.root, self.sandbox, self.run_timeout = Path(root).resolve(), sandbox, run_timeout

    def specs(self, names) -> list[dict]:
        return [{"name": name, **SPECS[name]} for name in names]

    def call(self, name: str, args: dict, allowed) -> str:
        if name not in allowed:
            return f"Error: there is no tool named {name}. Available: {', '.join(allowed)}."
        try:
            return getattr(self, name)(**args)
        except TypeError as exc:
            return f"Error: bad arguments for {name}: {exc}"
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return f"Error: {exc}"

    def path(self, value: str | None) -> Path:
        target = (self.root / (value or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"{value} is outside the repository")
        if ".git" in target.relative_to(self.root).parts:
            raise ValueError(".git is not accessible")
        return target

    def relative(self, value: str | None) -> str:
        return self.path(value).relative_to(self.root).as_posix()  # "." for the root: git rejects an empty pathspec

    def list_files(self, path: str = ".") -> str:
        result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", self.relative(path)],
                                cwd=self.root, capture_output=True, text=True)
        files = [f for f in result.stdout.splitlines() if (self.root / f).exists()]
        if not files:
            return f"No files under {path}"
        more = f"\n[{len(files) - 400} more; list a subdirectory]" if len(files) > 400 else ""
        return "\n".join(files[:400]) + more

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target = self.path(path)
        if target.is_dir():
            return f"Error: {path} is a directory; use list_files."
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + READ_LINES - 1, start + READ_LINES - 1)
        rows, size = [], 0
        for n in range(start, end + 1):
            row = f"{n:5}| {lines[n - 1][:2000]}"
            if size + len(row) > READ_CHARS and rows:
                end = n - 1
                break
            rows.append(row)
            size += len(row) + 1
        body = "\n".join(rows)
        more = f"\n[lines {end + 1}-{len(lines)} not shown]" if end < len(lines) else ""
        return f"{path} ({len(lines)} lines)\n{body}{more}"

    def search(self, pattern: str, path: str = ".") -> str:
        result = subprocess.run(["git", "grep", "-n", "-I", "-E", "--untracked", "-e", pattern, "--", self.relative(path)],
                                cwd=self.root, capture_output=True, text=True)
        if result.returncode == 1:
            return "No matches"
        if result.returncode > 1:
            return "Error: search failed: " + result.stderr.strip()[:300]
        lines = [line[:300] for line in result.stdout.splitlines()]
        more = f"\n[{len(lines) - 80} more matches; narrow the pattern or path]" if len(lines) > 80 else ""
        return "\n".join(lines[:80]) + more

    def syntax_error(self, target: Path) -> str | None:
        """SWE-agent's lint-on-edit: an edit that breaks the file's syntax is not applied (removing it cost 3 points)."""
        suffix = target.suffix
        if suffix == ".py":
            try:
                ast.parse(target.read_text(), filename=target.name)
            except SyntaxError as exc:
                return f"line {exc.lineno}: {exc.msg}"
        elif suffix == ".json":
            try:
                json.loads(target.read_text())
            except ValueError as exc:
                return str(exc)
        elif suffix in (".js", ".mjs", ".cjs") and self.sandbox is not None:
            # Node 25's module detection makes a plain `node --check file.js` miss errors; parse explicitly instead.
            modes = {".mjs": ["module"], ".cjs": ["commonjs"]}.get(suffix, ["module", "commonjs"])
            source = shlex.quote(str(target.relative_to(self.root)))
            errors = []
            for mode in modes:
                code, out, _ = self.sandbox.run(f"node --input-type={mode} --check < {source}", timeout=30)
                if code == 0 or code == 127:  # parses (127: no node here, nothing to say)
                    return None
                errors.append(out.strip())
            return clip(errors[0], 1200)
        return None

    def checked(self, path: str, target: Path, previous: str | None, message: str) -> str:
        problem = self.syntax_error(target)
        if not problem:
            return message
        if previous is None:
            target.unlink()
        else:
            target.write_text(previous)
        return (f"Error: the change was not applied because it breaks the syntax of {path}: {problem}\n"
                "The file is unchanged. Fix the change and try again.")

    def write_file(self, path: str, content: str) -> str:
        target = self.path(path)
        previous = target.read_text() if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return self.checked(path, target, previous,
                            f"{'Replaced' if previous is not None else 'Created'} {path} ({content.count(chr(10)) + 1} lines)")

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        target = self.path(path)
        text = target.read_text()
        count = text.count(old_text) if old_text else 0
        if count == 1:
            target.write_text(text.replace(old_text, new_text, 1))
            return self.checked(path, target, text, f"Edited {path}")
        if count > 1:
            return f"Error: old_text occurs {count} times in {path}; include more surrounding lines so it is unique."
        squash = lambda s: re.sub(r"\s+", " ", s).strip()
        hint = "The text matches if whitespace is ignored: check indentation." if old_text and squash(old_text) in squash(text) \
            else "Read the file again; it may differ from what you expect."
        return f"Error: old_text not found in {path}. {hint}"

    def run(self, command: str) -> str:
        if self.sandbox is None:
            return "Error: commands are not available in this session."
        code, out, seconds = self.sandbox.run(command, timeout=self.run_timeout)
        return f"exit code {code} ({seconds:.0f}s)\n{clip(out, OUTPUT_CHARS)}"
