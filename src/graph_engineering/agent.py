"""The agent loop: a model calls tools on a workspace until it calls its finish tool.

The loop owns the stopping conditions (step budget, stalls, repeated calls, cancellation) and the
context, following Anthropic's context-engineering practice: keep a small working set of high-signal
tokens. In order of cost:

1. Tool-result clearing: past the working budget, old tool output is elided (the latest results stay).
2. Notes: an ``update_notes`` tool keeps the plan and findings in a file outside the context; it is
   shown at the start of every session, so later rounds start from what earlier ones learned.
3. Compaction: when clearing is not enough, the model summarises its progress into the notes and the
   conversation restarts from the task and the notes.
4. Sub-agents: an ``explore`` tool answers a question in a fresh read-only context and returns only
   the answer.

Reasoning cut off by the output limit is handed back as notes too, since chat templates drop earlier reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
import time
from typing import Callable

from .llm import LLMError, Model, chat
from .sandbox import clip
from .tools import Workspace

CHARS_PER_TOKEN = 3.5      # measured 3.71 on the 27B with this code (JSON-escaped messages); a little margin
LOW_WATER = 0.6            # a clearing frees down to this share of the budget, so the next one comes much later
RESULT_SHARE = 0.25        # one tool result takes at most this share of the budget; the 9B's two file reads overflowed it
WORKING_TOKENS = 105000    # about 99K real tokens of a 150K window; at 80K x 3 characters the coder on task #20 cleared
                           # every few steps and re-read agent.js ten times (40K made it re-read instead of edit)
NOTES_CHARS = 6000
FAILED_OUTPUT = re.compile(r"Traceback \(most recent call last\)|^(?:FAIL|ERROR)[:\s]|^\s*[✖✕×] |AssertionError|"
                           r"\b\d+ (?:failed|failing)\b|^not ok ", re.M)
SAME_OUTPUT = "[the same output as this command gave"

NOTES_TOOL = {"name": "update_notes",
              "description": "Replace your working notes: the plan, what is done, what you learned about the code (file paths, "
                             "functions, conventions), and what is next. Notes survive when old messages are cleared and are "
                             "shown to you in later sessions on this task. Keep them under 6000 characters.",
              "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}
EXPLORE_TOOL = {"name": "explore",
                "description": "Ask a helper to investigate a question about the codebase in its own context (for example: "
                               "'how are API routes tested?' or 'where is the model name configured?'). It reads and searches, "
                               "then returns a short answer with file paths. Use it for broad questions; read files yourself "
                               "for narrow ones.",
                "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}
COMPACT = ("Your context is full. Write your working notes now so that you can continue from them alone: the plan, what is "
           "done (files changed and why), what you learned about the code (paths, functions, test conventions), the current "
           "problem and the next steps. Reply with the notes only, under 6000 characters.")


@dataclass
class Outcome:
    status: str                         # done | stalled | looping | out_of_steps | cancelled | error
    result: dict | None = None          # the finish tool's arguments when status is done
    steps: int = 0
    seconds: float = 0
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    error: str | None = None

    def asdict(self) -> dict:
        return {"status": self.status, "result": self.result, "steps": self.steps, "seconds": round(self.seconds),
                "usage": self.usage, "error": self.error}


def validate(arguments: dict, schema: dict) -> str | None:
    """A small JSON-schema subset: required keys, basic types, enums. Returns an error or None."""
    types = {"string": str, "array": list, "object": dict, "integer": int, "boolean": bool, "number": (int, float)}
    for key in schema.get("required", []):
        if key not in arguments:
            return f"missing required field {key}"
    for key, spec in schema.get("properties", {}).items():
        if key not in arguments:
            continue
        value = arguments[key]
        expected = types.get(spec.get("type"))
        if expected and not isinstance(value, expected):
            return f"{key} must be of type {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"{key} must be one of {spec['enum']}"
        if spec.get("type") == "array" and "items" in spec:
            for item in value:
                problem = validate(item, spec["items"]) if isinstance(item, dict) else None
                if spec["items"].get("type") == "object" and not isinstance(item, dict):
                    problem = f"items of {key} must be objects"
                if problem:
                    return f"{key}: {problem}"
    return None


class Agent:
    def __init__(self, model: Model, workspace: Workspace, tools: tuple, finish: dict, system: str, *,
                 max_steps: int = 60, nudge_after: int | None = None, transcript: Path | None = None,
                 on_step: Callable[[dict], None] | None = None, cancelled: Callable[[], bool] = lambda: False,
                 notes: Path | None = None, explorer: Callable[[str], str] | None = None, working_tokens: int = WORKING_TOKENS,
                 evidence: bool = False, verify: str | None = None, thinking_budget: int | None = None):
        self.model, self.workspace, self.tools, self.finish, self.system = model, workspace, tools, finish, system
        self.max_steps, self.nudge_after, self.on_step, self.cancelled = max_steps, nudge_after, on_step, cancelled
        self.transcript = Path(transcript) if transcript else None
        self.notes, self.explorer = (Path(notes) if notes else None), explorer
        # The working budget, never more than the window minus the reply and the template's own framing.
        window = (model.context - model.max_output - 2000) * CHARS_PER_TOKEN * 0.8
        self.history_chars = int(min(window, working_tokens * CHARS_PER_TOKEN))
        self.result_chars = max(2000, int(self.history_chars * RESULT_SHARE))
        self.compactions = 0
        self.evidence = evidence          # finish must cite commands actually run after the last file change
        self.verify = verify              # the test command the harness runs itself when evidence is missing or stale
        self.thinking_budget = thinking_budget   # per-agent reasoning cap; None uses the model's default
        self.commands: list[tuple[int, str, int]] = []   # (step, command, exit code) of every run call
        self.last_change = 0
        self.reads: dict[str, tuple] = {}     # tool call id -> (path, first line, last line, step)
        self.edits: dict[str, int] = {}       # path -> step of its latest edit
        self.failed_at = 0                    # step of the latest command that failed, by exit code or by its output
        self.outputs: dict[str, int] = {}     # command output -> step it was last seen

    def log(self, entry: dict) -> None:
        if self.transcript:
            with self.transcript.open("a") as handle:
                handle.write(json.dumps({"at": time.time(), **entry}) + "\n")

    def emit(self, step: int, kind: str, text: str) -> None:
        if self.on_step:
            self.on_step({"step": step, "kind": kind, "text": text[:400]})

    def compact(self, messages: list, seen: dict | None = None) -> None:
        """Only once history passes the budget: clear superseded reads, then the oldest tool results (keeping the
        latest ten, then the latest three), down to LOW_WATER of the budget.

        Rewriting an earlier message invalidates the model server's prefix cache from that point, so the history is
        append-only between these rare, batched clearings. A cleared result is forgotten by the repeat guard too."""
        if self.size(messages) <= self.history_chars:
            return
        cleared = self.supersede(messages)
        size = self.size(messages)
        target = int(self.history_chars * LOW_WATER)
        for keep in (10, 3):
            for message in [m for m in messages if m["role"] == "tool"][:-keep]:
                if size <= target:
                    break
                if not message["content"].startswith("[elided"):
                    size -= len(json.dumps(message["content"])) - 70
                    message["content"] = "[elided to save context; repeat the call if you still need it]"
                    cleared.add(message.get("tool_call_id"))
        if seen is not None and cleared:
            for signature in [s for s, (_, call_id) in seen.items() if call_id in cleared]:
                del seen[signature]

    def supersede(self, messages: list) -> set:
        """Clear reads that a newer read of the same lines, or an edit of the file, has made redundant.
        One current copy of each file stays in context, so re-reading never pushes other files out."""
        cleared = set()
        latest: dict[str, list] = {}
        for message in reversed([m for m in messages if m["role"] == "tool"]):
            read = self.reads.get(message.get("tool_call_id"))
            if not read or message["content"].startswith("["):
                continue
            path, start, end, step = read
            edited = self.edits.get(path, 0)
            newer = latest.setdefault(path, [])
            if step < edited:
                note = f"[an older read of {path}, from before your edit at step {edited}; read it again if needed]"
            elif any(s <= start and end <= e for s, e in newer):
                note = f"[an older read of {path}; a newer read of the same lines is further down]"
            else:
                newer.append((start, end))
                continue
            message["content"] = note
            cleared.add(message.get("tool_call_id"))
        return cleared

    def read_notes(self) -> str:
        return self.notes.read_text()[:NOTES_CHARS] if self.notes and self.notes.exists() else ""

    def opening(self, task: str) -> list:
        notes = self.read_notes()
        text = task + (f"\n\nYour notes from earlier work on this task:\n<notes>\n{notes}\n</notes>" if notes else "")
        return [{"role": "system", "content": self.system}, {"role": "user", "content": text}]

    def summarise(self, messages: list, specs: list, task: str, step: int) -> list:
        """Compaction: the model writes its notes, and the conversation restarts from the task and those notes."""
        reply = chat(self.model, messages + [{"role": "user", "content": COMPACT}], specs, tool_choice="none")
        summary = (reply.content or reply.reasoning).strip()[:NOTES_CHARS]
        if self.notes:
            self.notes.write_text(summary)
        self.compactions += 1
        self.emit(step, "compact", f"context compacted ({self.compactions}); notes {len(summary)} characters")
        fresh = self.opening(task)
        if not self.notes:
            fresh[1]["content"] += f"\n\nYour notes from earlier in this session:\n<notes>\n{summary}\n</notes>"
        fresh[1]["content"] += "\n\nThe conversation was compacted. Continue from your notes; the workspace holds your changes."
        for message in fresh:
            self.log({**message, "compaction": self.compactions})
        return fresh

    def size(self, messages: list) -> int:
        return sum(len(json.dumps(m)) for m in messages)

    def run(self, task: str) -> Outcome:
        started = time.time()
        outcome = Outcome("out_of_steps")
        messages = self.opening(task)
        for message in messages:
            self.log(message)
        extra = ([NOTES_TOOL] if self.notes else []) + ([EXPLORE_TOOL] if self.explorer else [])
        specs = self.workspace.specs(self.tools) + extra + [self.finish]
        allowed = self.tools
        idle = repeats = since_change = 0
        force_action = False   # after reasoning ran out without acting: next turn without thinking, a tool call required
        compacted_at = -1
        restarted = restart = False   # one fresh start from the notes when the agent loops
        seen: dict[str, tuple[int, str]] = {}   # call signature -> (step, tool call id) of results still in context
        try:
            for step in range(1, self.max_steps + 1):
                outcome.steps = step
                if self.cancelled():
                    outcome.status = "cancelled"
                    break
                self.compact(messages, seen)
                stuck = False
                if self.size(messages) > self.history_chars:  # clearing was not enough
                    # Compacting on consecutive steps means one step's reads no longer fit: the explorer re-read the same
                    # two files after every compaction until it ran out of steps. Answer from the notes instead.
                    stuck = compacted_at == step - 1
                    messages = self.summarise(messages, specs, task, step)
                    compacted_at = step
                    seen.clear()
                if step == self.max_steps or stuck:
                    # The last step can only end the session: a warning alone did not stop a reviewer from exploring.
                    if stuck:
                        self.emit(step, "note", "compacted on consecutive steps; asking for the final answer")
                    messages.append({"role": "user", "content": f"This is your last step. Call {self.finish['name']} now."})
                    self.log(messages[-1])
                    reply = chat(self.model, messages, [self.finish],
                                 tool_choice={"type": "function", "function": {"name": self.finish["name"]}})
                elif force_action:
                    # Eval: the 27B hit the output limit thinking four turns running and stalled; asking it to act
                    # did not help. Without thinking and with a required tool call, it acts.
                    reply = chat(self.model, messages, specs, tool_choice="required", think=False)
                    force_action = False
                else:
                    budget, think = self.thinking(step)
                    reply = chat(self.model, messages, specs, budget=budget, think=think)
                for key in outcome.usage:
                    outcome.usage[key] += reply.usage.get(key, 0)
                message = {"role": "assistant", "content": reply.content}
                if reply.tool_calls:
                    message["tool_calls"] = [{"id": c["id"], "type": "function",
                                              "function": {"name": c["name"], "arguments": c["arguments"]}} for c in reply.tool_calls]
                messages.append(message)
                self.log({**message, "reasoning": reply.reasoning, "finish_reason": reply.finish_reason, "step": step})
                if reply.content.strip():
                    self.emit(step, "say", reply.content.strip())
                if not reply.tool_calls:
                    idle += 1
                    if reply.finish_reason == "length":
                        message["content"] = ("My analysis was cut off at the output limit. Notes so far:\n"
                                              + (reply.reasoning or reply.content)[-6000:])
                        nudge = ("Your thinking hit the output limit. Do not redo the analysis: act on your notes now with a "
                                 "single tool call, and think further after you see the result.")
                        self.emit(step, "note", "thinking cut off; next turn acts without thinking")
                        force_action = True
                    else:
                        nudge = f"Continue with a tool call, or call {self.finish['name']} if you are done."
                    if idle == 4:
                        outcome.status = "stalled"
                        break
                    messages.append({"role": "user", "content": nudge})
                    self.log(messages[-1])
                    continue
                idle = 0
                since_change += 1
                for call in reply.tool_calls:
                    result = self.dispatch(call, step, seen, allowed)
                    if isinstance(result, Outcome):
                        result.steps, result.usage = step, outcome.usage
                        outcome = result
                        break
                    if call["name"] in ("write_file", "edit_file") and not result.startswith("Error"):
                        seen.clear()  # files changed, so earlier reads and runs may now differ
                        since_change = 0
                    repeats = repeats + 1 if result.startswith(("You already made this exact call", SAME_OUTPUT)) else 0
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                    self.log(messages[-1])
                    if repeats >= 3 and not restarted:
                        # Run 13: the coder re-read the same lines in circles ("I've been overthinking this") and the
                        # round was lost. A repeated action predicts failure (SWE-smith); a fresh start from the
                        # notes breaks the circle. A second loop ends the session.
                        restarted, repeats, restart = True, 0, True   # after this turn's remaining calls answer
                    elif repeats >= 3:
                        outcome = Outcome("looping", steps=step, usage=outcome.usage)
                        break
                if restart:
                    restart = False
                    self.emit(step, "note", "repeated the same call three times; restarting from notes")
                    messages = self.summarise(messages, specs, task, step)
                    compacted_at = step
                    seen.clear()
                    continue
                if outcome.status != "out_of_steps":
                    break
                if step == self.max_steps - 3:
                    # Live eval: a reviewer read until its budget ran out and never gave a verdict, which cost whole rounds.
                    messages.append({"role": "user", "content": f"You have 3 steps left. Call {self.finish['name']} now with "
                                     "what you have found; say in it what you could not check."})
                    self.log(messages[-1])
                    self.emit(step, "note", "3 steps left: asked to finish")
                if self.nudge_after and since_change and since_change % self.nudge_after == 0:
                    messages.append({"role": "user", "content": f"You have explored for {self.nudge_after} steps without changing "
                                     "a file. Stop exploring: state your plan in a short paragraph, then make the first change."})
                    self.log(messages[-1])
        except LLMError as exc:
            outcome.status, outcome.error = "error", str(exc)
        outcome.seconds = time.time() - started
        self.log({"role": "outcome", **outcome.asdict()})
        self.emit(outcome.steps, "end", outcome.status + (f": {outcome.error}" if outcome.error else ""))
        return outcome

    def check_evidence(self, args: dict) -> str | None:
        """Evidence, not assertions: every cited command must have run, after the last file change, with that exit code."""
        items = args.get("evidence") or []
        if not items:
            return ("finish needs evidence: run the test command after your last change, then cite it with its exit code "
                    "in the evidence field")
        for item in items:
            command = str(item.get("command", "")).strip()
            runs = [code for step, ran, code in self.commands if ran == command and step >= self.last_change]
            if not runs:
                return (f"evidence `{command}` was not run after your last file change (step {self.last_change}); "
                        "run it now and cite the result")
            if runs[-1] != item.get("exit_code"):
                return f"evidence `{command}` last exited {runs[-1]}, not {item.get('exit_code')}"
        if not (args.get("unresolved") or "").strip() and all(item.get("exit_code") != 0 for item in items):
            return "no cited command passed; fix the failures, or say in unresolved what is left"
        return None

    def thinking(self, step: int) -> tuple[int | None, bool | None]:
        """(budget, think) for this turn. 0 turns thinking off (Aider: Qwen3 edits as well or better without it). A
        negative budget is adaptive: think, up to that many tokens, on the first step and right after a command
        failed, and act without thinking otherwise. Run 15: with thinking off simple tickets took half the time, but
        a debugging-heavy one ran out of steps three rounds running."""
        budget = self.thinking_budget
        if budget is None or budget > 0:
            return budget, None
        if budget == 0:
            return None, False
        return (-budget, None) if step == 1 or self.failed_at == step - 1 else (None, False)

    def fit(self, result: str) -> tuple[str, int | None]:
        """Cut a result to the per-result share of the budget at a line boundary, saying where it stopped."""
        head = result[:self.result_chars]
        head = head[:head.rfind("\n")] if "\n" in head else head
        numbers = re.findall(r"(?m)^\s*(\d+)\|", head)
        if numbers:
            last = int(numbers[-1])
            return (head + f"\n[cut at line {last} to fit your context; read from start_line={last + 1} for more]", last)
        return head + f"\n[cut to {len(head)} characters to fit your context; narrow the request for the rest]", None

    def dispatch(self, call: dict, step: int, seen: dict, allowed) -> str | Outcome:
        name = call["name"]
        try:
            args = json.loads(call["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            return f"Error: arguments are not a valid JSON object ({exc}). Call the tool again with valid JSON."
        if name == self.finish["name"]:
            problem = validate(args, self.finish["parameters"]) or (self.check_evidence(args) if self.evidence else None)
            if problem and self.verify and self.workspace.sandbox and not validate(args, self.finish["parameters"]) \
                    and ("needs evidence" in problem or "was not run after" in problem):
                # Run the check for the agent instead of spending a turn asking it to.
                result = self.workspace.run(self.verify)
                match = re.match(r"exit code (-?\d+)", result)
                code = int(match.group(1)) if match else -1
                self.commands.append((step, self.verify, code))
                self.emit(step, "verify", f"harness ran {self.verify}: exit {code}")
                if code == 0:
                    args = {**args, "evidence": [{"command": self.verify, "exit_code": 0, "run_by": "harness"}]}
                    problem = None
                else:
                    return (f"Error: your finish had no fresh evidence, so the harness ran `{self.verify}` for you:\n{result}\n"
                            "Fix the failures and run it again, or call finish saying in unresolved what is left.")
            if problem:
                return f"Error: {problem}. Call {name} again with every required field."
            self.emit(step, "finish", json.dumps(args)[:400])
            return Outcome("done", result=args)
        signature = name + json.dumps(args, sort_keys=True)
        if signature in seen:
            self.emit(step, "repeat", name)
            return (f"You already made this exact call at step {seen[signature][0]} and nothing has changed since, so the result "
                    "is the same. Use what you have and take the next step.")
        seen[signature] = (step, call["id"])
        if name == "update_notes" and self.notes:
            self.notes.write_text(str(args.get("content", ""))[:NOTES_CHARS])
            self.emit(step, "notes", str(args.get("content", ""))[:400])
            return f"Notes saved ({min(len(str(args.get('content', ''))), NOTES_CHARS)} characters)."
        if name == "explore" and self.explorer:
            self.emit(step, "explore", str(args.get("question", "")))
            answer = self.explorer(str(args.get("question", "")))
            self.emit(step, "explored", answer[:400])
            return answer
        result = self.workspace.call(name, args, allowed)
        shown = None
        if len(result) > self.result_chars:
            if name == "run":   # a command's verdict is at the end: keep the exit code line and the tail
                first, _, rest = result.partition("\n")
                result = first + "\n" + clip(rest, self.result_chars - len(first) - 80)
            else:
                result, shown = self.fit(result)
        if name == "read_file" and not result.startswith("Error"):
            start = int(args.get("start_line") or 1)
            total = re.match(r".*? \((\d+) lines\)", result)
            end = min(int(args.get("end_line") or start + 399), int(total.group(1)) if total else 10 ** 9)
            end = min(end, shown or end)
            self.reads[call["id"]] = (str(args.get("path")), start, end, step)
        if name == "run":
            match = re.match(r"exit code (-?\d+)", result)
            code = int(match.group(1)) if match else -1
            self.commands.append((step, str(args.get("command", "")).strip(), code))
            # Run 16: `test | head; echo EXIT=$?` exits 0 while the test errors; the output says it failed.
            if code != 0 or FAILED_OUTPUT.search(result):
                self.failed_at = step
            # Run 16: the coder ran one test 77 times changing only `head -260` to `-280`, `-300`: the same output
            # with nothing changed is no new information, whatever the command's spelling.
            key = re.sub(r"^exit code (-?\d+) \(\d+s\)", r"exit code \1", result)
            seen_at = self.outputs.get(key)
            self.outputs[key] = step
            if seen_at is not None and seen_at >= self.last_change and seen_at < step:
                result = (f"{SAME_OUTPUT} at step {seen_at}, and no file has changed since. Change the code, or take a "
                          f"different step.]\n" + result)
        elif name in ("write_file", "edit_file") and not result.startswith("Error"):
            self.last_change = step
            self.edits[str(args.get("path"))] = step
        brief = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in args.items() if k not in ("content", "new_text", "old_text"))
        self.emit(step, "tool", f"{name}({brief}) -> {result.splitlines()[0][:160] if result else ''}")
        return result
