import json
import tempfile
import unittest
from pathlib import Path

from graph_engineering import routing
from graph_engineering.models import ModelError, default_models, load_models


def config(profile="multimodel", **extra):
    return {"models": default_models(), "routing": profile} | extra


class RoutingTests(unittest.TestCase):
    def test_baseline_profile_routes_every_role_to_the_large_tier(self):
        for role in set(routing.ROLE_TIERS) - routing.SMALL_FIRST_ROLES:
            with self.subTest(role=role):
                self.assertEqual(routing.select(config("baseline"), role)[0], "a")

    def test_question_answering_is_small_first_in_every_profile_but_assigned_order_wins(self):
        for profile in ("baseline", "multimodel"):
            self.assertEqual(routing.candidates(config(profile), "question_answering"), ["b", "a"])
        assigned = config("assigned", role_routes={"question_answering": ["a"]})
        self.assertEqual(routing.candidates(assigned, "question_answering"), ["a"])

    def test_multimodel_profile_uses_role_tiers(self):
        self.assertEqual(routing.select(config(), "classification")[0], "b")
        self.assertEqual(routing.select(config(), "planning")[0], "a")

    def test_bounded_role_escalates_when_small_model_is_down(self):
        chosen, decision = routing.select(config(), "classification", health={"b": False})
        self.assertEqual(chosen, "a")
        self.assertTrue(decision["degraded"])
        self.assertEqual(decision["skipped_unavailable"], ["b"])

    def test_complex_role_never_silently_uses_small_model(self):
        with self.assertRaisesRegex(routing.RoutingError, "observed unavailable"):
            routing.select(config(), "implementation", health={"a": False})

    def test_operator_may_explicitly_permit_small_fallback_for_a_complex_role(self):
        chosen, decision = routing.select(config(small_fallback_roles=["root_investigation"]),
                                          "root_investigation", health={"a": False})
        self.assertEqual((chosen, decision["degraded"]), ("b", True))
        with self.assertRaises(routing.RoutingError):
            routing.select(config(small_fallback_roles=["root_investigation"]), "planning", health={"a": False})

    def test_role_allow_list_and_missing_identifier_exclude_models(self):
        models = default_models()
        models["b"]["roles"] = ["review"]
        self.assertEqual(routing.candidates({"models": models, "routing": "multimodel"}, "classification"), ["a"])
        models["a"]["model_id"] = None
        with self.assertRaisesRegex(routing.RoutingError, "No configured model"):
            routing.select({"models": models, "routing": "multimodel"}, "planning")

    def test_override_must_name_a_usable_model(self):
        self.assertEqual(routing.select(config(model_overrides={"review": "a"}), "review")[0], "a")
        with self.assertRaises(routing.RoutingError):
            routing.select(config(model_overrides={"review": "zzz"}), "review")

    def test_arbitrary_provider_names_route_by_tier(self):
        models = {"remote-large": {"endpoint": "https://example.invalid/v1", "model_id": "big", "tier": "large"},
                  "local-small": {"endpoint": "http://127.0.0.1:1/v1", "model_id": "tiny", "tier": "small"}}
        cfg = {"models": models, "routing": "multimodel"}
        self.assertEqual(routing.select(cfg, "classification")[0], "local-small")
        self.assertEqual(routing.select(cfg, "implementation")[0], "remote-large")

    def test_legacy_models_without_tier_infer_from_context(self):
        models = default_models()
        for m in models.values():
            m.pop("tier")
        self.assertEqual(routing.tier_of(models, "b"), routing.SMALL)
        self.assertEqual(routing.tier_of(models, "a"), routing.LARGE)


class ModelRegistryTests(unittest.TestCase):
    def test_home_registry_overrides_defaults_and_rejects_inline_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertEqual(load_models(home), default_models())
            (home / "models.json").write_text(json.dumps({"x": {"endpoint": "http://h/v1", "model_id": "m", "tier": "small"}}))
            self.assertEqual(list(load_models(home)), ["x"])
            (home / "models.json").write_text(json.dumps({"x": {"endpoint": "http://h/v1", "api_key": "s"}}))
            with self.assertRaises(ModelError):
                load_models(home)
            (home / "models.json").write_text(json.dumps({"x": {"endpoint": "http://h/v1", "typo_field": 1}}))
            with self.assertRaisesRegex(ModelError, "Unknown model configuration"):
                load_models(home)


if __name__ == "__main__":
    unittest.main()
