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
            (repo / "backend" / "app.js").write_text('app.post("/api/query", (req, res) => res.json({ answer }));\n')
            (repo / "frontend" / "ChatBar.jsx").write_text('export const ChatBar = () => <button>Send message</button>;\n')
            (repo / "backend" / "unrelated.js").write_text('export const add = (a, b) => a + b;\n')
            git(repo, "add", ".")
            git(repo, "commit", "-m", "Chat app")
            sha = git(repo, "rev-parse", "HEAD").strip()
            state = {"config": {"source_repo": str(repo)}, "base_sha": sha, "enhanced": {
                "read_files": ["backend/llm.js", "README.md"],
                "acceptance_criteria": ["A GET /api/model endpoint returns the current model name as JSON",
                                        "The chat bar displays the model name next to the Send message button"]}}
            sources = ["backend/llm.js", "backend/app.js", "frontend/ChatBar.jsx", "backend/unrelated.js"]
            files = development_files(state, sources, ["backend/llm.js"])
            self.assertEqual(files[0], "backend/llm.js")  # Cited findings first.
            self.assertIn("backend/app.js", files)        # Where the API response lives.
            self.assertIn("frontend/ChatBar.jsx", files)  # Where the chat bar lives.
            self.assertNotIn("README.md", files)          # Only committed source files.


if __name__ == "__main__":
    unittest.main()
