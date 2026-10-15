"""Plan D13: every message the harness writes opens with one tag."""
from __future__ import annotations

import pathlib
import re

from claude_hooks.agent_loop import runner

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_tagged_once():
    n = runner.harness_note("Continue.")
    assert n == {"role": "user", "content": "[SYSTEM MESSAGE] Continue.",
                 "_harness": True}
    assert runner.harness_note(n["content"])["content"] == n["content"]


def test_every_harness_message_is_built_by_the_helper():
    # A hand-built ``_harness`` dict is how an untagged note gets in.
    raw = re.compile(r'"_harness"\s*:\s*True')
    offenders = []
    for base in ("claude_hooks", "consultants"):
        for path in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if raw.search(line) and "def harness_note" not in line \
                        and 'return {"role": "user", "content": body' not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


def _tool():
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path": "a"}'}}]},
        "finish_reason": "tool_calls"}]}


def _say(text, done="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": done, "done_reason": done}]}


def test_loop_notes_are_tagged():
    # force-first retry, then a cut answer continued: both notes tagged.
    script = [_say("no tools"), _tool(), _say("half", done="length"),
              _say(" whole")]
    sent = []

    def chat_fn(p):
        sent.append([dict(m) for m in p["messages"]])
        return script.pop(0)

    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_answer_after=0),
        tool_specs=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}],
        chat_fn=chat_fn, tool_executor=lambda n, a, c: "body")
    notes = [m for msgs in sent for m in msgs if m.get("_harness")]
    assert len({m["content"] for m in notes}) >= 2
    assert all(m["content"].startswith("[SYSTEM MESSAGE]") for m in notes)
