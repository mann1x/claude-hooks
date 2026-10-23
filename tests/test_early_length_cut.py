"""A turn the server cuts for length far below its allowance, with no
content, is the server out of context (``context_overflow``), not an answer
that ran long: continuing it strips the tools and makes a lane that was
still reading answer (compaction v7 smoke, 2026-10-23: 1 and 262 tokens of
~77k at ~49k prompt, two lanes on one KV pool)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_hooks.agent_loop import runner
from claude_hooks.get_advice import chat_client as cc
from claude_hooks.get_advice.chat_client import ChatClient, ChatFault
from consultants.engine import council

from tests.test_chat_client_transport import _Resp, _Seq
from tests.test_compaction_overflow import Backend, _history


def _cut(used, content="", thinking="", **extra):
    msg = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    d = {"message": msg, "done": True, "done_reason": "length",
         "prompt_eval_count": 49_953, "eval_count": used}
    d.update(extra)
    return d


def _body(n=76_317):
    return {"options": {"num_predict": n}}


@pytest.fixture(autouse=True)
def _no_sleep_no_slots(monkeypatch):
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")


def test_the_measured_cuts_are_early():
    assert cc._early_length_cut(_cut(1, thinking="Let"), _body())
    assert cc._early_length_cut(_cut(262, thinking="x" * 821), _body(78_966))


@pytest.mark.parametrize("data,body", [
    (_cut(1, content="Partial answer"), _body()),          # has an answer
    (_cut(1, tool_calls=None) | {"message": {"content": "", "tool_calls": [{}]}},
     _body()),                                              # has a call
    (_cut(4_000), _body(76_317)),                           # ran long
    (_cut(1), {"options": {}}),                             # allowance unknown
    (_cut(1), {"options": {"num_predict": True}}),          # not a count
    (_cut(1) | {"done_reason": "stop"}, _body()),           # not a cut
    (_cut(1) | {"eval_count": None}, _body()),              # usage unknown
])
def test_other_turns_are_not_early_cuts(data, body):
    assert cc._early_length_cut(data, body) is None


def test_the_openai_allowance_is_read_too():
    assert cc._early_length_cut(_cut(3), {"max_tokens": 8_192})
    assert cc._early_length_cut(_cut(3), {"max_tokens": 40}) is None


@pytest.mark.parametrize("stream", [False, True])
def test_the_client_raises_an_overflow_and_does_not_resend(stream):
    seq = _Seq([_Resp(_cut(1, thinking="Let"))])
    c = ChatClient("http://stub", max_retries=3)
    payload = {"model": "m", "messages": [{"role": "user", "content": "q"}],
               "options": {"num_predict": 76_317}}
    with patch("urllib.request.urlopen", side_effect=seq):
        with pytest.raises(ChatFault) as err:
            (c.chat_streamed if stream else c.chat)(payload)
    assert err.value.fault_class == cc.CONTEXT_OVERFLOW
    assert "1 of 76317" in str(err.value)
    assert len(seq.requests) == 1


def test_a_cut_that_ran_to_the_allowance_is_still_returned():
    seq = _Seq([_Resp(_cut(76_000, content="long answer"))])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = ChatClient("http://stub", max_retries=0).chat(
            {"model": "m", "messages": [], "options": {"num_predict": 76_317}})
    assert out["choices"][0]["message"]["content"] == "long answer"


SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def test_a_lane_cut_early_is_compacted_and_keeps_its_tools():
    early = ChatFault("cut early", fault_class="context_overflow")
    read = {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "read_file",
                                                                  "arguments": json.dumps({"path": "a.py"})}}]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5}}
    backend = Backend(ctx=131_072, script=[early, read])
    prepare = council._loop_payload_budgeter(backend, "m", role="researcher")
    sizes = []   # recover() resends the same dict, so measure at send time

    def sending(payload):
        sizes.append(("tools" in payload, len(payload["messages"])))
        return backend.chat(payload)

    out = runner.run_loop(
        {"model": "m", "messages": _history()}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 prepare_payload=prepare,
                                 max_iterations=3, force_answer_after=0),
        tool_specs=SPEC, chat_fn=prepare.recover(sending),
        tool_executor=lambda n, a, c: "body")
    # The lane's own calls: cut, resent compacted with tools on, after the read.
    assert [t for t, _ in sizes] == [True, True, True]
    assert sizes[1][1] < sizes[0][1]
    assert out["choices"][0]["message"]["content"] == "answer"
