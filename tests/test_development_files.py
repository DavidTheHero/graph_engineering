"""Development evidence covers where a feature lives, not only the cited lines (GE-30D05C40, 2026-10-21)."""
from pathlib import Path
import tempfile
import unittest

from graph_engineering.accepted_development import development_files
from graph_engineering.repository import git


class DevelopmentFilesTests(unittest.TestCase):
    def test_acceptance_criteria_bring_in_the_api_and_chat_bar_files(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@localhost")
            (repo / "backend").mkdir()
            (repo / "frontend").mkdir()
            (repo / "backend" / "llm.js").write_text('export const createModel = () => ({ model: process.env.OLLAMA_MODEL || "x" });\n')
            (repo / "backend" / "agent.js").write_text('import { createModel } from "./llm.js";\nexport const createAgent = () => createModel();\n')
            (repo / "backend" / "app.js").write_text('import { createAgent } from "./agent.js";\napp.post("/api/query", (req, res) => res.json({ answer }));\n')
            (repo / "frontend" / "ChatBar.jsx").write_text('export const ChatBar = () => <button>Send message</button>;\n')
            (repo / "backend" / "unrelated.js").write_text('export const add = (a, b) => a + b;\n')
            git(repo, "add", ".")
            git(repo, "commit", "-m", "Chat app")
            sha = git(repo, "rev-parse", "HEAD").strip()
            state = {"config": {"source_repo": str(repo)}, "base_sha": sha, "enhanced": {
                "read_files": ["backend/llm.js", "README.md"],
                "acceptance_criteria": ["A GET /api/model endpoint returns the current model name as JSON",
                                        "The chat bar displays the model name next to the Send message button"]}}
            sources = ["backend/llm.js", "backend/agent.js", "backend/app.js", "frontend/ChatBar.jsx", "backend/unrelated.js"]
            files = development_files(state, sources, ["backend/llm.js"])
            self.assertEqual(files[0], "backend/llm.js")  # Cited findings first.
            self.assertEqual(files[1:3], ["backend/agent.js", "backend/app.js"])  # The import chain up to the API app.
            self.assertIn("frontend/ChatBar.jsx", files)  # Where the chat bar lives.
            self.assertNotIn("README.md", files)          # Only committed source files.
            self.assertNotIn("backend/unrelated.js", files)  # Neither imported nor matching the criteria.

    def make(self, folder, files):
        repo = Path(folder)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@localhost")
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        git(repo, "add", ".")
        git(repo, "commit", "-m", "files")
        return repo, git(repo, "rev-parse", "HEAD").strip()

    def test_every_cited_file_is_kept_and_first_in_the_bundle(self):
        """Review of PR #6: neither the limit nor the bundle's ordering may drop a validated finding file."""
        from graph_engineering.repository import source_bundle
        with tempfile.TemporaryDirectory() as folder:
            files = {f"f{i}.js": f"export const v{i} = {i};\n" for i in range(12)}
            files.update({"z/llm.js": "export const llm = 1;\n" + "// pad\n" * 2000,
                          **{f"{d}/{n}.js": 'import { llm } from "../z/llm.js";\n' + "// pad\n" * 2000 for d, n in (("a", "app"), ("b", "agent"), ("c", "ui"))}})
            repo, sha = self.make(folder, files)
            state = {"config": {"source_repo": str(repo)}, "base_sha": sha, "enhanced": {"acceptance_criteria": []}}
            cited = [f"f{i}.js" for i in range(12)]
            self.assertEqual(development_files(state, list(files), cited)[:12], cited)  # All 12, beyond the limit.
            names = development_files(state, list(files), ["z/llm.js"])
            self.assertEqual(names[0], "z/llm.js")
            self.assertEqual(sorted(names[1:]), ["a/app.js", "b/agent.js", "c/ui.js"])  # Real importers.
            bundle = source_bundle(repo, names, "", 30000, first=["z/llm.js"])
            self.assertEqual(bundle["files"][0]["path"], "z/llm.js")
            self.assertTrue(bundle["files"][0]["complete"])

    def test_import_chain_needs_a_real_import_not_a_substring(self):
        with tempfile.TemporaryDirectory() as folder:
            repo, sha = self.make(folder, {"src/a.js": "export const a = 1;\n",
                                           "x/one.js": 'const data = {}; data.a = 1;\n',
                                           "x/two.js": 'fetch("/api/a/b");\n',
                                           "x/real.js": 'import { a } from "../src/a.js";\n',
                                           "py/user.py": "from pkg.a import thing\n",
                                           "py/other.py": "import alpha\n"})
            state = {"config": {"source_repo": str(repo)}, "base_sha": sha, "enhanced": {"acceptance_criteria": []}}
            names = development_files(state, ["src/a.js", "x/one.js", "x/two.js", "x/real.js", "py/user.py", "py/other.py"], ["src/a.js"])
            self.assertEqual(sorted(names[1:]), ["py/user.py", "x/real.js"])


if __name__ == "__main__":
    unittest.main()
