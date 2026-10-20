import subprocess
import tempfile
import unittest
from pathlib import Path

from graph_engineering.adapters import detect, is_test
from graph_engineering.investigation import retrieve
from graph_engineering.repository import discover, run_check
from graph_engineering.tickets import Tickets
from graph_engineering.control_repositories import register, inspect


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@localhost", "commit", "-qm", "init")
    return repo


class AdapterDetectionTests(unittest.TestCase):
    def detected(self, files):
        primary, found = detect(Path("/nonexistent"), list(files))
        return primary.name, [a.name for a in found]

    def test_each_ecosystem_is_detected_from_manifests_only(self):
        cases = {
            "go": ["go.mod", "main.go"],
            "rust": ["Cargo.toml", "src/lib.rs"],
            "jvm-maven": ["pom.xml", "src/main/java/A.java"],
            "jvm-gradle": ["gradlew", "build.gradle"],
            "dotnet": ["App.csproj", "Program.cs"],
            "ruby": ["Gemfile", "lib/a.rb"],
            "generic": ["README.md", "main.c"],
        }
        for expected, files in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(self.detected(files)[0], expected)

    def test_generic_adapter_has_no_runner(self):
        primary, _ = detect(Path("/nonexistent"), ["main.c"])
        self.assertIsNone(primary.test_command)
        self.assertIn(".c", primary.source_suffixes)

    def test_multilanguage_test_naming(self):
        for name in ("pkg/add_test.go", "src/test/java/AddTest.java", "spec/a_spec.rb", "tests/unit.rs", "FooTests.cs"):
            with self.subTest(name=name):
                self.assertTrue(is_test(name))
        for name in ("pkg/add.go", "src/main/java/Add.java", "lib/latest.rb", "contest.py"):
            with self.subTest(name=name):
                self.assertFalse(is_test(name))


class PortableRegistrationTests(unittest.TestCase):
    def test_go_repository_registers_and_is_searchable_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td), {"go.mod": "module x\n", "add.go": "package x\nfunc Add(a, b int) int { return a - b }\n",
                                        "add_test.go": "package x\n"})
            info = discover(repo)
            self.assertEqual((info["adapter"], info["source_files"], info["test_files"]), ("go", ["add.go"], ["add_test.go"]))
            self.assertIn("go.mod", info["manifests"])
            found = retrieve(repo, info["sha"], ["Add"], "Add returns the wrong value")
            self.assertEqual([e["file"] for e in found["excerpts"] if e["file"].endswith(".go")][0], "add.go")
            tickets = Tickets(Path(td) / "home")
            try:
                registered = register(tickets, str(repo))
                self.assertFalse(registered["trusted"])
                self.assertEqual(inspect(tickets, registered["id"])["test_command"], ["go", "test", "./..."])
            finally:
                tickets.store.close()

    def test_polyglot_source_scope_is_the_union(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td), {"package.json": '{"scripts":{"test":"node --test"}}', "app.js": "", "tool/x.py": ""})
            info = discover(repo)
            self.assertEqual(info["ecosystems"], ["javascript", "python"])
            self.assertEqual(sorted(info["source_files"]), ["app.js", "tool/x.py"])

    def test_generic_repository_cannot_execute_without_explicit_command(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td), {"main.c": "int main(){return 0;}\n"})
            self.assertIsNone(discover(repo)["test_command"])
            with self.assertRaisesRegex(ValueError, "No test command"):
                run_check(repo, None, trusted=True)


if __name__ == "__main__":
    unittest.main()
