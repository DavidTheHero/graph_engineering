"""Development prompts fit the model budget instead of blocking (GE-30D05C40, 2026-10-21: agent.js is 24.5 KB)."""
import unittest

from graph_engineering.investigation import _size, prompt_budget
from graph_engineering.models import default_models
from graph_engineering.workflow import HUNK_SCHEMA, PLAN_SCHEMA, fit_implementation_request, fit_planning_request

CONFIG = {"models": default_models(), "routing": "baseline"}


def planning(big):
    return {"task": "t" * 700, "candidate": {"actual": "a" * 4000},
            "evidence": {"files": [{"path": "backend/agent.js", "content": "x" * big, "complete": True},
                                   {"path": "backend/llm.js", "content": "y" * 5000, "complete": True}]},
            "additional_information": [], "regression_test": {"path": "t.test.js", "content": "z" * 600},
            "previous_attempt": {"files": ["backend/llm.js"], "failure_output": "f" * 3000}}


class PromptFittingTests(unittest.TestCase):
    def test_planning_prompt_is_shortened_to_the_budget_with_paths_kept(self):
        request = planning(40000)
        self.assertGreater(_size(request), prompt_budget(CONFIG, "planning", PLAN_SCHEMA))
        fitted = fit_planning_request(CONFIG, request)
        self.assertLessEqual(_size(fitted), prompt_budget(CONFIG, "planning", PLAN_SCHEMA))
        self.assertEqual([f["path"] for f in fitted["evidence"]["files"]], ["backend/agent.js", "backend/llm.js"])
        agent = fitted["evidence"]["files"][0]
        self.assertEqual((agent["complete"], agent["shortened_from"]), (False, 40000))
        self.assertTrue(fitted["shortened_for_budget"])
        self.assertEqual(fit_planning_request(CONFIG, planning(2000)), planning(2000))  # Fits: unchanged.

    def test_implementation_prompt_keeps_planned_files_complete_or_fails_clearly(self):
        evidence = {"files": [{"path": "backend/agent.js", "content": "x" * 30000, "complete": True}]}
        request = {"task": "t", "plan": {}, "candidate": {"actual": "a" * 4000}, "evidence": evidence,
                   "previous_failure": "f" * 4000, "review_feedback": {}, "additional_information": ["i" * 800] * 3}
        fitted = fit_implementation_request(CONFIG, request)
        self.assertLessEqual(_size(fitted), prompt_budget(CONFIG, "implementation", HUNK_SCHEMA))
        self.assertEqual(fitted["evidence"], evidence)  # Edits must match the exact complete text.
        too_big = request | {"evidence": {"files": [{"path": "big.js", "content": "x" * 45000, "complete": True}]}}
        with self.assertRaisesRegex(ValueError, "split the task"):
            fit_implementation_request(CONFIG, too_big)

    def test_log_inside_evidence_and_review_feedback_are_shortened_before_giving_up(self):
        """Review of PR #7: a 31 KB planned file with a 6 KB bundle log must fit, not raise."""
        evidence = {"files": [{"path": "a.js", "content": "x" * 31000, "complete": True}], "failure_output": "l" * 6000}
        request = {"task": "t", "plan": {}, "candidate": {"actual": "a" * 500}, "evidence": evidence,
                   "previous_failure": "f" * 4000, "review_feedback": {"approved": False, "rationale": "r" * 3000, "raw": "z" * 2000},
                   "additional_information": []}
        fitted = fit_implementation_request(CONFIG, request)
        self.assertLessEqual(_size(fitted), prompt_budget(CONFIG, "implementation", HUNK_SCHEMA))
        self.assertEqual(fitted["evidence"]["files"], evidence["files"])
        self.assertLessEqual(len(fitted["evidence"]["failure_output"]), 1500)

    def test_budget_follows_the_model_that_is_routed_first_not_the_strictest_fallback(self):
        """Review of PR #7: a stricter fallback model must not shrink prompts the preferred model accepts."""
        models = default_models() | {"c": default_models()["a"] | {"context": 32000, "label": "small-context large"}}
        config = {"models": models, "routing": "assigned", "role_routes": {"implementation": ["a", "c"], "planning": ["a", "c"]}}
        self.assertEqual(prompt_budget(config, "implementation", HUNK_SCHEMA), prompt_budget(CONFIG, "implementation", HUNK_SCHEMA))
        reversed_routes = config | {"role_routes": {"implementation": ["c", "a"]}}
        self.assertLess(prompt_budget(reversed_routes, "implementation", HUNK_SCHEMA), prompt_budget(CONFIG, "implementation", HUNK_SCHEMA))


if __name__ == "__main__":
    unittest.main()
