"""Plan D1: the reasoning-loop guard — signals, stream cut, loop handling."""
from __future__ import annotations

import json

import pytest

from claude_hooks import capped_thinking
from claude_hooks import reasoning_loop_guard as rlg
from claude_hooks.agent_loop import runner
from claude_hooks.get_advice import chat_client as cc


# ---- the three signals ---------------------------------------------- #

def _feed(text: str, chunk: int = 37):
    g = rlg.ReasoningLoopGuard()
    for i in range(0, len(text), chunk):
        v = g.push(text[i:i + chunk])
        if v is not None:
            return v
    return None


def _healthy(n: int = 3000) -> str:
    return "".join(f"Step {i}: file_{i}.py defines f{i * 7}, which calls "
                   f"g{i * 3} — check its {i % 11} callers next.\n"
                   for i in range(n))


def test_healthy_long_reasoning_never_trips():
    # Length is never the signal: ~230k characters of varied reasoning.
    text = _healthy()
    assert len(text) > 200_000
    assert _feed(text) is None


def test_checklist_flip_trips_low_uniqueness():
    text = "".join(("- [ ] verify the retry window\n",
                    "- [x] verify the retry window\n",
                    "- [ ] verify the backoff ceiling\n")[i % 3]
                   for i in range(400))
    v = _feed(text)
    assert v is not None and v.kind == "low-uniqueness"
    assert v.distinct == 3


def test_exact_block_repeat_trips_line_cycle():
    # As in the fork: with a 60-line window only a short period reaches
    # min_cycles, and it fires while the window is still filling (long
    # lines carry min_chars first); a full window goes to low-uniqueness.
    block = ["So the cache key must include the mtime, otherwise a stale "
             "read is served after the edit and the lane never notices it.",
             "But the mtime alone is not enough on a coarse filesystem clock, "
             "so the size goes into the key as well, which brings us back."]
    text = "\n".join(block * 40) + "\n"
    v = _feed(text)
    assert v is not None and v.kind == "line-cycle"
    assert v.period == 2 and v.cycles >= 20


def test_unbroken_phrase_repeat_trips_phrase_cycle():
    text = "Wait, let me reconsider the order. " * 300
    v = _feed(text)
    assert v is not None and v.kind == "phrase-cycle"
    assert "reconsider" in v.sample


def test_short_block_never_trips():
    # Under min_chars nothing is judged, however repetitive.
    assert _feed("same line\n" * 300) is None


def test_scan_matches_live_guard():
    text = "Wait, let me reconsider the order. " * 300
    assert rlg.scan(text).kind == "phrase-cycle"
    assert rlg.scan(_healthy(500)) is None


# ---- the stream readers stop reading ------------------------------- #

class _CountingResp:
    def __init__(self, lines):
        self._lines = lines
        self.read_lines = 0

    def __iter__(self):
        for line in self._lines:
            self.read_lines += 1
            yield line


def _ollama_lines(reasoning_chunks, tail_content="never read"):
    lines = [(json.dumps({"message": {"role": "assistant", "content": "",
                                      "thinking": c}, "done": False}) + "\n")
             .encode() for c in reasoning_chunks]
    lines.append((json.dumps({"message": {"role": "assistant",
                                          "content": tail_content},
                              "done": True, "done_reason": "stop",
                              "eval_count": 9}) + "\n").encode())
    return lines


def test_ollama_reader_cuts_the_stream_on_a_loop():
    chunks = ["Wait, let me reconsider the order. "] * 2000
    resp = _CountingResp(_ollama_lines(chunks))
    client = cc.ChatClient("http://stub")
    data = client._consume_ndjson(resp, on_token=None, cancel_check=None)
    assert resp.read_lines < 300          # stopped long before the end
    assert data["done_reason"] == rlg.DONE_REASON
    assert data["reasoning_loop"]["kind"] == "phrase-cycle"
    assert data["message"]["content"] == ""
    # Carried verbatim through the OpenAI translation.
    out = client._from_ollama(data)
    assert out["choices"][0]["done_reason"] == rlg.DONE_REASON
    assert out["choices"][0]["finish_reason"] == rlg.DONE_REASON


def test_ollama_reader_reads_a_healthy_stream_to_the_end():
    chunks = [line + "\n" for line in _healthy(400).splitlines()]
    resp = _CountingResp(_ollama_lines(chunks, tail_content="done"))
    data = cc.ChatClient("http://stub")._consume_ndjson(
        resp, on_token=None, cancel_check=None)
    assert resp.read_lines == len(chunks) + 1
    assert data["done_reason"] == "stop"
    assert "reasoning_loop" not in data


def test_openai_reader_cuts_the_stream_on_a_loop():
    lines = [("data: " + json.dumps({"choices": [{"delta": {
        "reasoning_content": "Wait, let me reconsider the order. "}}]}) + "\n")
        .encode() for _ in range(2000)]
    lines.append(b"data: [DONE]\n")
    resp = _CountingResp(lines)
    client = cc.OpenAICompatChatClient("http://oc:8240/v1")
    data = client._consume_ndjson(resp, on_token=None, cancel_check=None)
    assert resp.read_lines < 300
    assert data["done_reason"] == rlg.DONE_REASON
    assert data["reasoning_loop"]["kind"] == "phrase-cycle"


# ---- run_loop ------------------------------------------------------- #

LOOPED = {"kind": "phrase-cycle", "chars": 4100, "period": 35,
          "cycles": 117, "sample": "Wait, let me reconsider the order."}


def _resp(content="", tool_calls=None, thinking="", loop=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if thinking:
        msg["thinking"] = thinking
    out = {"choices": [{"message": msg,
                        "finish_reason": "tool_calls" if tool_calls else (
                            rlg.DONE_REASON if loop else "stop"),
                        "done_reason": rlg.DONE_REASON if loop else "stop"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    if loop:
        out["reasoning_loop"] = loop
    return out


def _call(name="read_file", args='{"path": "a.py"}', cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


class _Script:
    """Conversation calls pop the script; condensation calls answer a note."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.condensations = 0

    def __call__(self, payload):
        msgs = payload.get("messages") or []
        if msgs and msgs[0].get("content") == capped_thinking.CAPPED_THINKING_SYSTEM_PROMPT:
            self.condensations += 1
            return _resp(content="Settled: the retry window is in deciseconds.")
        # The loop extends the same payload later: keep it as sent.
        self.calls.append({**payload, "messages": list(msgs)})
        return self.script.pop(0)


SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def _run(script, **cfg):
    events = []
    fn = _Script(script)
    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "audit"}]},
        "/tmp", config=runner.LoopConfig(force_first_tool_call=False,
                                          on_guard=events.append, **cfg),
        tool_specs=SPEC, chat_fn=fn,
        tool_executor=lambda name, args, cwd: "file body")
    return out, fn, events


def test_one_trip_keeps_partial_content_and_continues():
    pre = "".join(f"Reading module {i} of the ledger package.\n" for i in range(40))
    thinking = pre + "Wait, let me reconsider the order. " * 200
    out, fn, events = _run([
        _resp(content="Partial finding.", thinking=thinking, loop=LOOPED),
        _resp(content="Final answer."),
    ])
    assert out["choices"][0]["message"]["content"] == "Final answer."
    assert "_stop_reason" not in out
    sent = fn.calls[1]["messages"]
    assert sent[-2] == {"role": "assistant", "content": "Partial finding."}
    note = sent[-1]
    assert note["_harness"] is True
    assert note["content"].startswith("[SYSTEM MESSAGE]")
    assert "35-character phrase" in note["content"]
    assert "deciseconds" in note["content"]      # the condensed pre-loop
    assert fn.condensations == 1
    assert events == [{"guard": "reasoning_loop", "action": "cut",
                       "iteration": 0, "trips": 1, "limit": 3, **LOOPED}]


def test_the_event_carries_the_realized_sampler():
    r = _resp(loop=LOOPED)
    r["timings"] = {"sampler": {"seed": 1234, "temperature": 0.7}}
    out, fn, events = _run([r, _resp(content="ok")])
    assert events[0]["sampler"] == {"seed": 1234, "temperature": 0.7}


def test_three_trips_in_a_row_force_one_toolless_report():
    out, fn, events = _run([
        _resp(loop=LOOPED), _resp(loop=LOOPED), _resp(loop=LOOPED),
        _resp(content="Report: what I found."),
    ])
    assert out["_stop_reason"] == "reasoning_loop"
    assert out["_failure_class"] == "reasoning_loop"
    assert out["choices"][0]["message"]["content"] == "Report: what I found."
    last = fn.calls[-1]
    assert "tools" not in last and last["think"] is False
    assert "looped 3 turns in a row" in last["messages"][-1]["content"]
    assert [e["action"] for e in events] == ["cut", "cut", "stop"]
    assert len(fn.calls) == 4


def test_a_clean_turn_resets_the_streak():
    out, fn, events = _run([
        _resp(loop=LOOPED), _resp(loop=LOOPED),
        _resp(tool_calls=[_call()]),
        _resp(loop=LOOPED), _resp(loop=LOOPED),
        _resp(content="Done."),
    ])
    assert "_stop_reason" not in out
    assert out["choices"][0]["message"]["content"] == "Done."
    assert [e["trips"] for e in events] == [1, 2, 1, 2]


def test_unstreamed_reply_whose_reasoning_ate_the_turn_counts():
    thinking = "Wait, let me reconsider the order. " * 300
    out, fn, events = _run([
        _resp(thinking=thinking), _resp(content="Answer."),
    ])
    assert events and events[0]["kind"] == "phrase-cycle"
    assert out["choices"][0]["message"]["content"] == "Answer."


def test_unstreamed_reply_with_an_answer_is_left_alone():
    thinking = "Wait, let me reconsider the order. " * 300
    out, fn, events = _run([_resp(content="Answer.", thinking=thinking)])
    assert events == []
    assert out["choices"][0]["message"]["content"] == "Answer."


def test_a_cut_turn_is_never_force_first_retried():
    # With force_first on, a tool-less turn would be re-asked with the
    # force-first message; a cut turn gets the loop note instead.
    events2 = []
    fn2 = _Script([_resp(loop=LOOPED), _resp(tool_calls=[_call()]),
                   _resp(content="ok")])
    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "audit"}]},
        "/tmp", config=runner.LoopConfig(force_first_tool_call=True,
                                          on_guard=events2.append),
        tool_specs=SPEC, chat_fn=fn2,
        tool_executor=lambda name, args, cwd: "file body")
    second = fn2.calls[1]["messages"][-1]["content"]
    assert second.startswith("[SYSTEM MESSAGE]")
    assert runner.LoopConfig().force_first_retry_message not in second


@pytest.mark.parametrize("limit", [1, 2])
def test_trip_limit_is_configurable(limit):
    script = [_resp(loop=LOOPED)] * limit + [_resp(content="Report.")]
    out, fn, events = _run(script, reasoning_loop_max_trips=limit)
    assert out["_stop_reason"] == "reasoning_loop"
    assert len(fn.calls) == limit + 1


# ---- consultants: guard events are recorded ------------------------- #

def test_guard_events_land_in_the_recorder(tmp_path):
    from consultants.engine.recorder import (MessageRecorder, RecorderMeta,
                                             guard_sink)
    assert guard_sink(None, role="researcher") is None
    rec = MessageRecorder(tmp_path / "transcript.db", meta=RecorderMeta(
        sid="csl-guard", cwd=str(tmp_path), question="q", effort="medium",
        topology="council", models={"researcher": "m"}))
    try:
        events = []
        fn = _Script([_resp(loop=LOOPED), _resp(content="ok")])
        sink = guard_sink(rec, role="researcher", round=1, lane_idx=2)
        runner.run_loop(
            {"model": "m", "messages": [{"role": "user", "content": "q"}]},
            str(tmp_path), config=runner.LoopConfig(
                force_first_tool_call=False,
                on_guard=lambda e: (events.append(e), sink(e))),
            tool_specs=SPEC, chat_fn=fn,
            tool_executor=lambda name, args, cwd: "")
        rows = [r for r in rec.list_runtime_events(since_event_id=0)
                if r["kind"] == "guard"]
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "researcher" and row["lane_idx"] == 2
        assert row["payload"]["guard"] == "reasoning_loop"
        assert row["payload"]["cycles"] == 117
    finally:
        rec.close()
