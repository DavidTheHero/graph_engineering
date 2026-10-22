"""OpenAI-compatible chat with tool calls, and the registry that routes roles to models."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROLES = ("intake", "coding", "review", "explore")


class LLMError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class Model:
    name: str
    endpoint: str
    model_id: str
    context: int = 32768            # tokens
    max_output: int = 8192          # tokens per turn, thinking included
    think: bool = True              # Qwen-style thinking via chat_template_kwargs
    timeout: int = 900              # seconds per model call
    api_key_env: str | None = None
    sampling: dict = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, data: dict) -> "Model":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "name"}
        return cls(name=name, **known)

    def body(self, messages: list, tools: list | None, tool_choice: str | dict = "auto") -> dict:
        # Qwen's recommended sampling; near-greedy decoding makes it repeat itself.
        sampling = {"temperature": 0.6, "top_p": 0.95, "top_k": 20} if self.think else \
                   {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.0}
        body = {"model": self.model_id, "messages": messages, "max_tokens": self.max_output,
                "chat_template_kwargs": {"enable_thinking": self.think}, **sampling, **self.sampling}
        if tools:
            body["tools"] = [{"type": "function", "function": tool} for tool in tools]
            body["tool_choice"] = tool_choice
        return body


@dataclass
class Reply:
    content: str
    reasoning: str
    tool_calls: list            # [{"id", "name", "arguments"(str)}]
    finish_reason: str
    usage: dict


def chat(model: Model, messages: list, tools: list | None = None, attempts: int = 4, tool_choice: str | dict = "auto") -> Reply:
    headers = {"Content-Type": "application/json"}
    if model.api_key_env:
        key = os.environ.get(model.api_key_env)
        if not key:
            raise LLMError(f"environment variable {model.api_key_env} is not set")
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(model.body(messages, tools, tool_choice)).encode()
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(model.endpoint.rstrip("/") + "/chat/completions", data, headers)
            with urllib.request.urlopen(request, timeout=model.timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if exc.code < 500 and exc.code != 429:
                raise LLMError(f"{model.name}: HTTP {exc.code}: {detail}") from exc
            error = LLMError(f"{model.name}: HTTP {exc.code}: {detail}", transient=True)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            error = LLMError(f"{model.name}: {type(exc).__name__}: {exc}", transient=True)
        if attempt == attempts - 1:
            raise error
        time.sleep(min(60, 5 * 2 ** attempt))
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{model.name}: malformed response") from exc
    calls = [{"id": c.get("id") or f"call_{i}", "name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}
             for i, c in enumerate(message.get("tool_calls") or [])]
    return Reply(content=message.get("content") or "", reasoning=message.get("reasoning") or message.get("reasoning_content") or "",
                 tool_calls=calls, finish_reason=choice.get("finish_reason") or "", usage=payload.get("usage") or {})


class Registry:
    """``models.json``: {"models": {name: {endpoint, model_id, ...}}, "roles": {role: [name, ...]}}.

    A role lists models in order of preference; the first reachable one is used."""

    def __init__(self, models: dict[str, Model], roles: dict[str, list[str]]):
        self.models, self.roles = models, roles
        self._probes: dict[str, tuple[float, bool]] = {}
        for role, names in roles.items():
            for name in names:
                if name not in models:
                    raise ValueError(f"role {role} names unknown model {name}")

    @classmethod
    def load(cls, path: Path) -> "Registry":
        data = json.loads(Path(path).read_text())
        models = {name: Model.from_mapping(name, spec) for name, spec in data.get("models", {}).items()}
        roles = {role: ([names] if isinstance(names, str) else list(names)) for role, names in data.get("roles", {}).items()}
        return cls(models, roles)

    def for_role(self, role: str, exclude: set[str] = frozenset(), prompt_chars: int = 0) -> Model:
        """Routing: the first model listed for the role that is reachable and whose window fits the opening prompt
        (with room to work). ``exclude`` is a preference (decorrelating a reviewer from the coder), not a requirement."""
        listed = self.roles.get(role) or list(self.models)
        if not listed:
            raise LLMError(f"no model is assigned to role {role}")
        for names in ([n for n in listed if n not in exclude], listed):
            for name in names:
                model = self.models[name]
                fits = prompt_chars / 3.0 < (model.context - model.max_output) * 0.5
                if fits and self.reachable(model):
                    return model
        return self.models[listed[-1]]  # nothing fits or answers: the last resort, so the error names a real model

    def reachable(self, model: Model) -> bool:
        cached = self._probes.get(model.name)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        ok = probe(model, timeout=5).get("ok", False)
        self._probes[model.name] = (time.time(), ok)
        return ok

    def describe(self) -> dict:
        return {"models": {n: {"endpoint": m.endpoint, "model_id": m.model_id, "context": m.context} for n, m in self.models.items()},
                "roles": self.roles}


def probe(model: Model, timeout: int = 10) -> dict:
    """Reachability and the served model ids, for `graph-engineer models`."""
    try:
        with urllib.request.urlopen(model.endpoint.rstrip("/") + "/models", timeout=timeout) as response:
            ids = [item.get("id") for item in json.load(response).get("data", [])]
        return {"ok": model.model_id in ids, "served": ids}
    except Exception as exc:  # report, never raise: this is a diagnostic
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
