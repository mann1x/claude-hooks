"""Plan Milestone C4: thinking condensation and reasoning on the wire."""
from __future__ import annotations

import pytest

from claude_hooks import capped_thinking, reasoning_replay
from claude_hooks.agent_loop import runner
from consultants.engine import compaction, council


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    reasoning_replay.clear_cache()
    yield
    reasoning_replay.clear_cache()


# --- modes ---------------------------------------------------------------- #

def test_apply_modes():
    msg = {"role": "assistant", "content": "answer", "thinking": "because x"}
    assert "thinking" not in reasoning_replay.apply(msg, "none")
    assert reasoning_replay.apply(msg, "none")["content"] == "answer"
    assert reasoning_replay.apply(msg, "all")["thinking"] == "because x"
    inline = reasoning_replay.apply(msg, "inline")
    assert inline["content"] == "<think>\nbecause x\n</think>\n\nanswer"
    assert "thinking" not in inline


class _Probe:
    base_url = "http://h"

    def __init__(self, delta):
        self.delta = delta
        self.calls = 0

    def chat(self, payload):
        self.calls += 1
        carried = any(m.get("thinking") for m in payload["messages"])
        return {"usage": {"prompt_tokens": 40 + (self.delta if carried else 0)}}


def test_all_is_kept_where_the_field_reaches_the_prompt_else_inline():
    assert reasoning_replay.resolve("all", _Probe(120), "m") == "all"
    reasoning_replay.clear_cache()
    assert reasoning_replay.resolve("all", _Probe(0), "m") == "inline"
    assert reasoning_replay.resolve("none", _Probe(0), "m") == "none"


def test_the_probe_is_cached_per_server_and_model():
    probe = _Probe(120)
    reasoning_replay.resolve("all", probe, "m")
    reasoning_replay.resolve("all", probe, "m")
    assert probe.calls == 2      # one pair, once


def test_none_asks_nothing_of_the_server():
    probe = _Probe(0)
    knobs = council.budget.Knobs()
    assert council._reasoning_replay(probe, "m", knobs) == "none"
    assert probe.calls == 0


def test_the_loop_keeps_reasoning_only_when_asked():
    turns = iter([
        {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "thinking": "reading a first",
            "tool_calls": [{"id": "t", "function": {
                "name": "read_file", "arguments": "{}"}}]}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]},
    ])
    sent = []

    def chat_fn(payload):
        sent.append([dict(m) for m in payload["messages"]])
        return next(turns)

    cfg = runner.LoopConfig(force_answer_after=0, capped_thinking_enabled=False,
                            reasoning_replay="inline")
    runner.run_loop({"messages": [{"role": "user", "content": "q"}]}, "",
                    config=cfg, tool_specs=[], chat_fn=chat_fn,
                    tool_executor=lambda n, a, c: "body")
    replayed = [m for m in sent[1] if m.get("role") == "assistant"][0]
    assert replayed["content"].startswith("<think>\nreading a first")


def test_default_loop_drops_reasoning_as_before():
    assert runner.LoopConfig().reasoning_replay == "none"


# --- the discarded-turn path ---------------------------------------------- #

def _capped_turn(text="partial", thinking="I am weighing the options " * 400):
    return {"choices": [{"finish_reason": "length",
                         "message": {"content": text, "thinking": thinking}}],
            "done_reason": "length",
            # The server's enforced allowance (plan B3) is what is compared.
            "timings": {"think_budget_tokens": 3000},
            "usage": {"prompt_tokens": 10, "completion_tokens": 4000}}


def test_a_cut_answer_continues_with_its_condensed_reasoning(monkeypatch):
    monkeypatch.setattr(
        capped_thinking, "condense",
        lambda capped, **kw: ("I have settled that the guard is wrong.", ""))
    sent = []
    answers = iter([_capped_turn(),
                    {"choices": [{"finish_reason": "stop",
                                  "message": {"content": " rest"}}]}])

    def chat_fn(payload):
        sent.append(payload)
        return next(answers)

    cfg = runner.LoopConfig(tools_available=False, thinking_budget_tokens=4000)
    out = runner.run_loop({"messages": [{"role": "user", "content": "q"},
                                        ], "options": {"num_predict": 4000}},
                          "", config=cfg, tool_specs=[], chat_fn=chat_fn,
                          tool_executor=lambda *a: "")
    assert out["choices"][0]["message"]["content"] == "partial rest"
    ask = sent[1]["messages"][-1]
    assert ask["_harness"] is True
    assert capped_thinking.REASONING_NOT_CARRIED in ask["content"]
    assert "I have settled that the guard is wrong." in ask["content"]


def test_single_shot_condenses_a_capped_think_into_the_continuation(monkeypatch):
    monkeypatch.setattr(
        capped_thinking, "condense",
        lambda capped, **kw: ("The caller passes None; that is the bug.", ""))

    class Client:
        def __init__(self):
            self.sent = []
            self.answers = iter([_capped_turn(), {
                "choices": [{"finish_reason": "stop",
                             "message": {"content": " done"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}])

        def context_length(self, model):
            return 262_144

        def chat(self, payload):
            self.sent.append(payload)
            return next(self.answers)

    client = Client()
    from types import SimpleNamespace
    monkeypatch.setattr(council.truncation, "classify",
                        lambda r, **k: SimpleNamespace(detail="cut")
                        if r.get("done_reason") == "length" else None)
    text, _, _ = council._single_shot(client, "m",
                                      [{"role": "user", "content": "q"}],
                                      role="synthesizer")
    assert text == "partial done"
    ask = client.sent[1]["messages"][-1]
    assert "The caller passes None; that is the bug." in ask["content"]
    assert capped_thinking.REASONING_NOT_CARRIED in ask["content"]


def test_a_turn_within_its_allowance_gets_no_note():
    resp = {"choices": [{"message": {"content": "x", "thinking": "short"}}],
            "usage": {"completion_tokens": 20}}
    assert council._single_shot_capped_note(
        object(), "m", resp, {"options": {"num_predict": 8000}}) is None


# --- telling the model ---------------------------------------------------- #

def test_harness_notes_quoting_reasoning_say_it_is_not_carried():
    block = capped_thinking.as_thinking_block("note")
    framed = capped_thinking.as_discarded_turn_note(block)
    assert framed.startswith(capped_thinking.REASONING_NOT_CARRIED)
    from consultants.engine import retrospective
    marker = retrospective.render_marker(
        retrospective.Digest(retrospective="Read callers first."), 3)
    assert "not carried into this request" in marker["content"]


def test_writer_input_carries_reasoning_cut_to_two_thousand_chars():
    text = compaction.serialize([{"role": "assistant", "content": "a",
                                  "thinking": "t" * 5_000}])
    line = next(ln for ln in text.splitlines() if ln.startswith("[Bot thinking]"))
    assert len(line) < 2_100 and "more chars" in line
