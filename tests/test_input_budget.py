"""A large model's input budget follows its registry entry and context, not a 40 000-byte constant (GE-30D05C40, 2026-10-21).

The 27B serves a 150 000-token context; capped at 40 000 bytes it could not be shown agent.js, llm.js and the
adapted agent.test.js together, so the widened plan stopped with "Planned source ... exceeds" after the last
recovery round. Measured: a 94 500-byte prompt is 21 665 tokens and prefills in 21 s on that host."""
from pathlib import Path
import tempfile
import unittest

from graph_engineering.investigation import prompt_budget
from graph_engineering.model_assignment import entry
from graph_engineering.models import LARGE_INPUT_CEILING, OpenCodeWorker, ModelError, default_input_budget, default_models, tier_input_ceiling
from graph_engineering.workflow import implementation_bundle_limit


def config(**large):
    return {"models": {"a": default_models()["a"] | large, "b": default_models()["b"]}, "routing": "baseline"}


class InputBudgetTests(unittest.TestCase):
    def test_default_budget_scales_with_context_within_bounds(self):
        self.assertEqual([default_input_budget(c) for c in (150000, 262144, 32768, 16384, None)], [90000, 120000, 40000, 12288, 3072])
        self.assertEqual(entry({"endpoint": "http://x", "model_id": "m", "context": 150000})["input_budget_bytes"], 90000)
        self.assertEqual(entry({"endpoint": "http://x", "model_id": "m", "context": 150000}, {"input_budget_bytes": 50000})["input_budget_bytes"], 50000)  # Operator-tuned stays.

    def test_prompt_budget_honours_a_large_entry_above_the_old_cap(self):
        self.assertGreater(prompt_budget(config(), "implementation"), 80000)
        self.assertGreater(implementation_bundle_limit(config()), 80000)
        self.assertLess(prompt_budget(config(input_budget_bytes=10**7), "implementation"), LARGE_INPUT_CEILING)  # Never above the ceiling.
        self.assertLess(prompt_budget(config(input_budget_bytes=10**7, context=30000), "implementation"), 30000)  # Never above the context.
        self.assertEqual((tier_input_ceiling(True), tier_input_ceiling(False)), (12000, LARGE_INPUT_CEILING))  # The small tier keeps its own ceiling.

    def test_worker_accepts_a_prompt_the_registry_allows(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = OpenCodeWorker(Path(folder), config(input_budget_bytes=90000)["models"])
            with self.assertRaisesRegex(ModelError, "Input exceeds"):
                worker.invoke("implementation", "x" * 95000, {}, "a", "run1")
            try:  # Under the registry's limit: the budget check passes and the call goes on to the (absent) OpenCode binary.
                worker.invoke("implementation", "x" * 60000, {}, "a", "run1")
            except ModelError as exc:
                self.assertNotIn("Input exceeds", str(exc))
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
