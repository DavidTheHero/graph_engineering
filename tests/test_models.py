import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from graph_engineering.models import (
    ModelError, OpenCodeWorker, default_models, generate_opencode_config,
    parse_opencode_output, sanitized_environment, validate_artifact, detect_opencode,
    ModelClient, _run_opencode,
)


class ModelBoundaryTests(unittest.TestCase):
    def test_long_source_transport_grammar_keeps_local_length_validation(self):
        schema={'type':'object','properties':{'content':{'type':'string','minLength':20,'maxLength':6000}},'required':['content'],'additionalProperties':False}
        config=generate_opencode_config(default_models()['b'],'regression_test',schema)
        transport=config['providers']['local']['models'][default_models()['b']['model_id']]['body']['response_format']['json_schema']['schema']
        self.assertNotIn('maxLength',transport['properties']['content'])
        self.assertEqual(schema['properties']['content']['maxLength'],6000)
        with self.assertRaises(ModelError):validate_artifact({'content':'x'*6001},schema)

    def test_unavailable_retries_bounded_and_invalid_models(self):
        with patch("graph_engineering.models._request", return_value={"error": "unavailable"}) as req:
            result = ModelClient(default_models()["b"]).health(retries=99)
            self.assertFalse(result["available"])
            self.assertEqual(req.call_count, 3)
        with patch("graph_engineering.models._request", return_value={"status": 200, "body": []}):
            self.assertFalse(ModelClient(default_models()["a"]).health()["available"])

    def test_cancellation_terminates_actual_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            with self.assertRaises(ModelError):
                _run_opencode([sys.executable, "-c", "import time; time.sleep(30)"], Path(tmp), dict(os.environ), 30, cancelled=lambda: True)
            self.assertLess(time.monotonic() - start, 5)

    def test_actual_version_prefix(self):
        with patch("graph_engineering.models.shutil.which", return_value="opencode"), patch("graph_engineering.models.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "opencode v2.0.32\n"
            self.assertEqual(detect_opencode()[1], "2.0.32")

    def test_malformed_or_tools_are_rejected(self):
        with self.assertRaises(ModelError):
            parse_opencode_output('{"type":"text","part":{"text":"not JSON"}}')
        with self.assertRaises(ModelError):
            parse_opencode_output('{"type":"tool_use","part":{"tool":"shell"}}')
        with self.assertRaises(ModelError):
            parse_opencode_output('{"type":"error","error":{"message":"offline"}}')

    def test_actual_event_shape_parses_and_validates(self):
        text = json.dumps({"type": "text", "sessionID": "ses_test", "part": {"text": '{"ok":true}'}})
        content, meta = parse_opencode_output(text)
        self.assertEqual(content, {"ok": True})
        self.assertEqual(meta["session_ids"], ["ses_test"])
        validate_artifact(content, {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False})
        with self.assertRaises(ModelError):
            validate_artifact({"ok": "true"}, {"type": "object", "properties": {"ok": {"type": "boolean"}}})
        with self.assertRaises(ModelError):
            validate_artifact({"ok": True, "cmd": "danger"}, {"type": "object", "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False})
        with self.assertRaises(ModelError):
            validate_artifact(float("nan"), {"type": "number"})

    def test_v2_config_and_secrets(self):
        cfg = generate_opencode_config(default_models()["a"], "implementation", {"type": "object"})
        self.assertIn("providers", cfg)
        self.assertIn("agents", cfg)
        self.assertNotIn("provider", cfg)
        self.assertEqual(cfg["providers"]["local"]["package"], "@opencode/ai/providers/openai-compatible")
        self.assertEqual(cfg["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GITHUB_TOKEN": "github-secret", "OPENAI_API_KEY": "other-secret", "CUSTOM_QWEN_KEY": "model-secret"}):
                env = sanitized_environment(Path(tmp), "CUSTOM_QWEN_KEY")
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["GRAPH_ENGINEERING_MODEL_API_KEY"], "model-secret")
            self.assertEqual(env["OPENCODE_CONFIG_PROJECT_DISABLE"], "true")

    def test_context_limit_before_process_and_unknown_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = OpenCodeWorker(Path(tmp), default_models())
            with self.assertRaises(ModelError):
                worker.invoke("discovery", "x" * 40001, {"type": "object"}, "a", "run")
            with self.assertRaises(ModelError):
                worker.invoke("discovery", "classify", {"type": "object"}, "missing", "run")
            with self.assertRaises(ModelError):
                worker.invoke("discovery", "x" * 12001, {"type": "object"}, "b", "run")
            with self.assertRaises(ModelError):
                worker.invoke("discovery", "classify", {"type": "object"}, "a", "../escape")

    def test_invalid_response_is_recorded_and_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = OpenCodeWorker(Path(tmp), default_models())
            with patch("graph_engineering.models._run_opencode", return_value=(0, '{"type":"text","part":{"text":"bad"}}', "")), patch("graph_engineering.models.detect_opencode", return_value=("opencode", "2.0.32")):
                with self.assertRaises(ModelError):
                    worker.invoke("review", "review diff", {"type": "object"}, "a", "run")
            self.assertFalse(worker.metrics[-1]["success"])
            self.assertTrue(Path(worker.metrics[-1]["raw_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
