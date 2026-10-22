import unittest

from graph_engineering import repomap
from graph_engineering.prompts import project_brief

from helpers import git, temp_dir


class RepoMap(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self) / "r"
        files = {
            "src/util.js": "export function helper() {}\nexport const shared = 1;\n",
            "src/chat.jsx": "import { helper } from './util';\nexport function ChatBar() { return 'Send model selector' }\n",
            "src/report.js": "import { helper } from './util.js';\nexport function renderReport() {}\n",
            "src/other.js": "import { shared } from './util';\nexport class Ledger {}\n",
            "pkg/__init__.py": "",
            "pkg/models.py": "from .base import Base\nclass ModelRegistry(Base):\n    pass\ndef _private():\n    pass\n",
            "pkg/base.py": "class Base:\n    pass\n",
        }
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.root.parent, "init", "-q", str(self.root))
        git(self.root, "add", "-A")

    def test_symbols_and_imports(self):
        self.assertEqual(repomap.symbols("export const a = 1\nexport default function B() {}\nclass C {}\n", ".js"),
                         ["a", "B", "C"])
        self.assertEqual(repomap.symbols("def _hidden():\n    pass\nasync def go():\n    pass\n", ".py"), ["go"])
        files = {"src/util.js", "pkg/base.py", "pkg/models.py"}
        self.assertEqual(repomap.resolve("src/chat.jsx", "./util", files), "src/util.js")
        self.assertEqual(repomap.resolve("pkg/models.py", ".base", files), "pkg/base.py")

    def test_the_tickets_words_beat_a_central_but_unrelated_file(self):
        lines = repomap.build(self.root, "Show the selected model in the chat bar next to Send").splitlines()
        self.assertTrue(lines[0].startswith("src/chat.jsx: ChatBar"), lines)
        self.assertLess(lines.index(next(l for l in lines if l.startswith("src/util.js"))),
                        lines.index(next(l for l in lines if l.startswith("src/other.js"))))  # imported by three files

    def test_budget_and_brief(self):
        self.assertLessEqual(len(repomap.build(self.root, "model", budget_chars=60)), 60)
        brief = project_brief(self.root, query="chat bar model selector")
        self.assertIn("Files most related to this task", brief)
        self.assertIn("src/chat.jsx: ChatBar", brief)
        self.assertNotIn("Files most related", project_brief(self.root))


if __name__ == "__main__":
    unittest.main()
