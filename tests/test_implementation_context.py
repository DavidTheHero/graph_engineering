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

    def test_edit_inside_a_shortened_line_is_refused_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / "a.js").write_text("const a = 1;\nconst prompt = '" + "p" * 500 + "';\nconst b = 2;\n")
            edits = [{"path": "a.js", "old": "const prompt = '" + "p" * 300 + TRUNCATED_LINE, "new": "const prompt = 'q';"}]
            with self.assertRaisesRegex(ValueError, "shortened line"):
                materialize_hunks(repo, {"edits": edits}, ["a.js"])
            fine = materialize_hunks(repo, {"edits": [{"path": "a.js", "old": "const b = 2;", "new": "const b = 3;"}]}, ["a.js"])
            self.assertIn("const b = 3;", fine[0]["content"])


if __name__ == "__main__":
    unittest.main()
