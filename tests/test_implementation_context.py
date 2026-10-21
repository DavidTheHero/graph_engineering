"""Planned files reach the implementer whole even when one line is enormous (GE-30D05C40, 2026-10-21).

backend/llm.js is 18 KB of which 7 lines (embedded prompts) are 12 KB; with backend/agent.js (24.5 KB)
the two planned files could never be shown complete within the 27B's budget, so implementation stopped
with "exceeds complete implementation context" and every recovery round burnt on it."""
from pathlib import Path
import tempfile
import unittest

from graph_engineering.accepted_development import materialize_hunks
from graph_engineering.models import default_models
from graph_engineering.repository import TRUNCATED_LINE, source_bundle
from graph_engineering.workflow import implementation_bundle_limit

CONFIG = {"models": default_models(), "routing": "baseline"}


class ImplementationContextTests(unittest.TestCase):
    def test_long_lines_are_shortened_so_the_file_stays_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            body = ["export function createModel() {", "  return new ChatOllama({", '    model: process.env.OLLAMA_MODEL || "x",',
                    "    system: '" + "p" * 12000 + "',", "  });", "}"]
            (repo / "llm.js").write_text("\n".join(body) + "\n")
            whole = source_bundle(repo, ["llm.js"], "", 30000)
            self.assertTrue(whole["files"][0]["complete"] and "truncated_lines" not in whole["files"][0])
            short = source_bundle(repo, ["llm.js"], "", 30000, max_line=300)
            [item] = short["files"]
            self.assertTrue(item["complete"])
            self.assertEqual(item["truncated_lines"], 1)
            self.assertLess(len(item["content"]), 1000)
            self.assertIn('    model: process.env.OLLAMA_MODEL || "x",', item["content"])  # Ordinary lines untouched.
            self.assertTrue(item["content"].splitlines()[3].endswith(TRUNCATED_LINE))
            # With a small budget the shortened file would still be cut, and that is reported as incomplete.
            self.assertFalse(source_bundle(repo, ["llm.js"], "", 400, max_line=300)["files"][0]["complete"])

    def test_limit_follows_the_model_budget_not_a_constant(self):
        self.assertGreater(implementation_bundle_limit(CONFIG), 30000)
        tiny = {"models": {"a": default_models()["a"] | {"input_budget_bytes": 20000}}, "routing": "baseline"}
        self.assertEqual(implementation_bundle_limit(tiny), 30000)  # Never below the historical floor.

    def test_edits_on_shortened_lines_keep_the_hidden_remainder_or_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            tail = "p" * 500 + "';"
            (repo / "a.js").write_text("const a = 1;\nimport { one, two, " + "x" * 300 + " } from './m.js';\nconst prompt = '" + tail + "\nconst b = 2;\n")
            shown = "import { one, two, " + "x" * 101  # The first 120 characters of the long line, as a 120-character view shows it.
            # Both old and new end with the marker: the visible prefix changes, the hidden remainder stays.
            kept = materialize_hunks(repo, {"edits": [{"path": "a.js", "old": shown + TRUNCATED_LINE, "new": "import { one, two, three, " + "x" * 101 + TRUNCATED_LINE}]}, ["a.js"])
            self.assertIn("import { one, two, three, " + "x" * 300 + " } from './m.js';", kept[0]["content"])
            # Review of PR #9: the visible prefix without the marker would splice the hidden remainder after the edit.
            with self.assertRaisesRegex(ValueError, "ends inside a long line"):
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": shown, "new": "import { one } from './m.js';"}]}, ["a.js"])
            # A replacement without the marker would be guessing the hidden text.
            with self.assertRaisesRegex(ValueError, "keep its hidden remainder"):
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": shown + TRUNCATED_LINE, "new": "import { one } from './m.js';"}]}, ["a.js"])
            # The marker is never source text.
            with self.assertRaisesRegex(ValueError, "never source text"):
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": "const b = 2;", "new": "const b = 2; /* " + TRUNCATED_LINE.strip() + " */"}]}, ["a.js"])
            # Ordinary edits, including mid-line edits of short lines, are unaffected.
            fine = materialize_hunks(repo, {"edits": [{"path": "a.js", "old": "b = 2", "new": "b = 3"}]}, ["a.js"])
            self.assertIn("const b = 3;", fine[0]["content"])

    def test_guard_covers_the_shortest_view_and_tails_follow_their_own_line(self):
        """Second review of PR #9/#10: F1 (80-character view) and F2 (swapped marker lines)."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            keys = "const KEYS = [" + ", ".join(f"'k{i}'" for i in range(12)) + "];"  # 88 characters: shortened at the 80 level.
            template = "const TEMPLATE = `" + "t" * 300 + "`;"
            imports = "import { " + ", ".join(f"a{i}" for i in range(40)) + " } from './a';"
            (repo / "a.js").write_text("x\n" + keys + "\n" + imports + "\n" + template + "\n")
            with self.assertRaisesRegex(ValueError, "ends inside a long line"):  # The 80-character prefix without the marker.
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": "x\n" + keys[:80], "new": "x\nconst KEYS = ['model'"}]}, ["a.js"])
            swapped = [{"path": "a.js", "old": imports[:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE,
                        "new": template[:80] + TRUNCATED_LINE + "\n" + imports[:80] + TRUNCATED_LINE}]
            out = materialize_hunks(repo, {"edits": swapped}, ["a.js"])[0]["content"]
            self.assertIn(template + "\n" + imports, out)  # A reorder keeps each line's own remainder, never swaps them.
            with self.assertRaisesRegex(ValueError, "begin like the shortened old line"):  # An unrelated line cannot claim a remainder.
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": imports[:80] + TRUNCATED_LINE, "new": "const OTHER = 1; " + TRUNCATED_LINE}]}, ["a.js"])
            reordered_properly = [{"path": "a.js", "old": imports[:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE,
                                   "new": "import { zz, " + imports[9:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE}]
            out = materialize_hunks(repo, {"edits": reordered_properly}, ["a.js"])[0]["content"]
            self.assertIn("import { zz, " + imports[9:], out)  # The import keeps its own remainder...
            self.assertIn(template, out)                       # ...and the template keeps its own.


if __name__ == "__main__":
    unittest.main()
