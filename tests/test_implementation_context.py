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
from graph_engineering.workflow import complete_view, implementation_bundle_limit, planned_complete

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
            # Re-review F-B: a lone remaining remainder is assigned even when the start changes (`const` -> `let`).
            lone = materialize_hunks(repo, {"edits": [{"path": "a.js", "old": template[:80] + TRUNCATED_LINE, "new": "let " + template[6:80] + TRUNCATED_LINE}]}, ["a.js"])
            self.assertIn("let " + template[6:], lone[0]["content"])
            with self.assertRaisesRegex(ValueError, "begin like the shortened old line"):  # Among several, an unrelated line cannot claim one.
                materialize_hunks(repo, {"edits": [{"path": "a.js", "old": imports[:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE,
                                                   "new": "const OTHER = 1; " + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE}]}, ["a.js"])
            reordered_properly = [{"path": "a.js", "old": imports[:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE,
                                   "new": "import { zz, " + imports[9:80] + TRUNCATED_LINE + "\n" + template[:80] + TRUNCATED_LINE}]
            out = materialize_hunks(repo, {"edits": reordered_properly}, ["a.js"])[0]["content"]
            self.assertIn("import { zz, " + imports[9:], out)  # The import keeps its own remainder...
            self.assertIn(template, out)                       # ...and the template keeps its own.

    def test_the_first_view_where_every_planned_file_is_complete_is_used(self):
        """GE-30D05C40 (third stop): agent.js + agent.test.js + the regression were incomplete at 300 but complete at 120."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / "a.js").write_text("\n".join("const a%d = '%s';" % (i, "x" * 250) for i in range(60)) + "\n")  # 15.5 KB, 60 long lines
            (repo / "b.js").write_text("\n".join("const b%d = '%s';" % (i, "x" * 250) for i in range(60)) + "\n")
            bundle = lambda max_line: source_bundle(repo, ["a.js", "b.js"], "", 20000, max_line=max_line)
            evidence, start = complete_view(bundle, ["a.js", "b.js"], (300, 200, 120), 20000)
            self.assertEqual(start, 2)  # Incomplete at 300 and 200 (31 KB), complete at 120 (about 17 KB).
            self.assertTrue(all(f["complete"] for f in evidence["files"]))
            with self.assertRaisesRegex(ValueError, "at the shortest view"):
                complete_view(bundle, ["a.js", "b.js"], (300, 200), 20000)
            self.assertEqual(complete_view(bundle, ["a.js"], (None,), 20000)[1], 0)  # Whole-file mode: no views.

    def test_a_planned_file_dropped_for_lack_of_room_is_incomplete(self):
        """Review of PR #11: a bundle with no room left omits the file entirely, which must not count as complete."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / "a.js").write_text("\n".join("const a%d = '%s';" % (i, "x" * 250) for i in range(60)) + "\n")
            (repo / "b.js").write_text("const b = 1;\n")
            bundle = lambda max_line: source_bundle(repo, ["a.js", "b.js"], "", 16300, max_line=max_line)
            dropped = bundle(300)
            self.assertEqual([f["path"] for f in dropped["files"]], ["a.js"])  # No room for b.js after a.js.
            self.assertFalse(planned_complete(dropped, ["a.js", "b.js"]))
            self.assertTrue(planned_complete(dropped, ["a.js"]))
            evidence, start = complete_view(bundle, ["a.js", "b.js"], (300, 200, 120), 16300)
            self.assertEqual((start, [f["path"] for f in evidence["files"]]), (1, ["a.js", "b.js"]))

    def test_each_view_is_checked_because_shortening_can_grow_a_line(self):
        """Review of PR #11: a line just over the limit grows by the marker, so completeness is not monotonic."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / "a.js").write_text("\n".join("const a%d = '%s';" % (i, "x" * 108) for i in range(60)) + "\n")  # 124-character lines
            bundle = lambda max_line: source_bundle(repo, ["a.js"], "", 7600, max_line=max_line)
            self.assertEqual([planned_complete(bundle(m), ["a.js"]) for m in (300, 200, 120, 80)], [True, True, False, True])
            self.assertEqual(complete_view(bundle, ["a.js"], (120, 80), 7600)[1], 1)


if __name__ == "__main__":
    unittest.main()
