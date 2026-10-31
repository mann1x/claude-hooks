"""Plan D1: verbatim paragraph repetition — measure, ration, nudge."""
from __future__ import annotations

from claude_hooks import reasoning_repetition as rr
from claude_hooks.agent_loop import runner

LOOP_PARA = ("Actually, I think I found it! The retry window is read in the "
             "wrong unit. These should all be fine. Let me look more "
             "carefully at the caller before I change it.")


def _block(repeats: int = 7, distinct: int = 15) -> str:
    # kessj msg 30's shape: one paragraph seven times among fresh ones.
    fresh = [f"Now the module ledger_{k}.py is checked, and it is fine because "
             f"its function f{k} only reads the config once, at import, and "
             f"the value it reads is the one the caller wrote in step {k}; so "
             f"the next thing to look at is the writer of key_{k}." for k in range(distinct)]
    paras = fresh[:]
    for j in range(repeats):
        paras.insert(2 + j * 3, LOOP_PARA)
    return "\n\n".join(paras)


def test_kessj_shape_is_a_loop():
    m = rr.measure(_block())
    assert m.max_repeat == 7 and m.duplicates == 6
    assert rr.is_loop(m)


def test_both_signals_are_required():
    # Many repeats but a low duplicate fraction: not a loop.
    m = rr.measure(_block(repeats=4, distinct=200))
    assert m.max_repeat == 4 and m.duplicate_fraction < 0.25
    assert not rr.is_loop(m)


def test_a_quoted_code_block_is_never_counted():
    code = "```\ndef dPw(c, x):\n    return c.get(x) or default_value_for(x)\n```"
    text = "\n\n".join([code] * 40)
    assert rr.measure(text) is None


def test_short_block_is_not_judged():
    assert rr.measure("\n\n".join([LOOP_PARA] * 10)) is None


def test_nudger_fires_once_then_needs_recovery_and_cooldown():
    n = rr.RepetitionNudger()
    looping = _block()
    first = n.inspect(looping, 0)
    assert first and first.startswith("[SYSTEM MESSAGE]")
    assert "7 times" in first and "Actually, I think I found it!" in first
    assert n.inspect(looping, 5) is None          # budget spent
    clean = "\n".join(f"Line {k} of fresh work on file_{k}." for k in range(800))
    assert len(clean) >= rr.DEFAULT.recovery_tokens * rr.CHARS_PER_TOKEN
    assert n.inspect(clean, 6) is None            # earns the budget back
    assert n.spent == 0
    assert n.inspect(looping, 7) is not None      # a different loop, later


def test_cooldown_survives_recovery():
    n = rr.RepetitionNudger()
    assert n.inspect(_block(), 0)
    clean = "x " * 20000
    n.inspect(clean, 1)
    assert n.spent == 0
    assert n.inspect(_block(), 2) is None         # 2 - 0 < cooldown 3


# ---- run_loop ------------------------------------------------------- #

def _turn(thinking="", tool=True, cid="c1", content=""):
    msg = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    if tool:
        msg["tool_calls"] = [{"id": cid, "type": "function", "function": {
            "name": "read_file", "arguments": f'{{"path": "{cid}.py"}}'}}]
    return {"choices": [{"message": msg,
                         "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def test_run_loop_nudges_a_looping_tool_call_turn_once():
    script = [_turn(_block(), cid="a"), _turn(_block(), cid="b"),
              _turn(_block(), cid="c"), _turn(_block(), cid="d"),
              _turn(tool=False, content="done")]
    sent, events = [], []

    def chat_fn(payload):
        sent.append(list(payload["messages"]))
        return script.pop(0)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 on_guard=events.append),
        tool_specs=[], chat_fn=chat_fn,
        tool_executor=lambda n, a, c: "body")
    assert out["choices"][0]["message"]["content"] == "done"
    nudges = [m for m in sent[-1] if m.get("_harness")]
    assert len(nudges) == 1
    # Appended after the turn's tool result, before the next call.
    i = sent[-1].index(nudges[0])
    assert sent[-1][i - 1]["role"] == "tool"
    assert [e["guard"] for e in events] == ["reasoning_repetition"]
    assert events[0]["max_repeat"] == 7
