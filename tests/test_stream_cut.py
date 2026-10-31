"""A stream that ends with no terminal signal was cut in transit. Read as a
reply, it would be stamped ``stop`` and nothing downstream could tell the
fragment from a finished answer, so it is retried as transport and never
returned."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import chat_client as cc


class _Resp:
    status = 200
    headers: dict = {}

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"".join(self._lines)


def _ndjson(*objs):
    return [(json.dumps(o) + "\n").encode() for o in objs]


def _sse(*objs, end=True):
    out = [b"data: " + json.dumps(o).encode() + b"\n" for o in objs]
    if end:
        out.append(b"data: [DONE]\n")
    return out


CUT_NDJSON = _ndjson({"message": {"role": "assistant", "content": "now PROVEN as the"},
                      "done": False})
WHOLE_NDJSON = _ndjson(
    {"message": {"role": "assistant", "content": "whole answer."}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True,
     "done_reason": "stop", "eval_count": 3})
DELTA = {"choices": [{"delta": {"content": "now PROVEN as the"}}]}
FINISH = {"choices": [{"delta": {}, "finish_reason": "stop"}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)


def _ollama(**kw):
    return cc.ChatClient("http://stub", timeout_s=5.0, max_retries=2,
                         retry_base_delay_s=0.0, retry_max_delay_s=0.0, **kw)


def _openai():
    c = cc.OpenAICompatChatClient("http://oc:8240/v1", max_retries=2,
                                  retry_base_delay_s=0.0)
    c.served_model = "/models/q.gguf"
    return c


def _seq(*streams):
    items = [_Resp(s) for s in streams]
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        return items.pop(0)
    return urlopen, calls


PAYLOAD = {"model": "stub", "messages": [{"role": "user", "content": "hi"}]}


def _content(out):
    return out["choices"][0]["message"]["content"]


def test_ollama_cut_stream_is_retried_and_the_retry_is_the_answer():
    urlopen, calls = _seq(CUT_NDJSON, WHOLE_NDJSON)
    with patch("urllib.request.urlopen", urlopen):
        out = _ollama().chat_streamed(dict(PAYLOAD))
    assert _content(out) == "whole answer."
    assert len(calls) == 2


def test_ollama_stream_cut_every_time_never_returns_the_fragment():
    urlopen, _ = _seq(CUT_NDJSON, CUT_NDJSON, CUT_NDJSON)
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(Exception, match="terminal signal"):
            _ollama().chat_streamed(dict(PAYLOAD))


def test_openai_cut_stream_is_retried():
    urlopen, calls = _seq(_sse(DELTA, end=False), _sse(DELTA, FINISH))
    with patch("urllib.request.urlopen", urlopen):
        _openai().chat_streamed(dict(PAYLOAD, model="@bs2"))
    assert len(calls) == 2


def test_openai_stream_cut_every_time_never_returns_the_fragment():
    urlopen, _ = _seq(*[_sse(DELTA, end=False)] * 3)
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(Exception, match="terminal signal"):
            _openai().chat_streamed(dict(PAYLOAD, model="@bs2"))


@pytest.mark.parametrize("stream", [_sse(DELTA, end=True),
                                    _sse(DELTA, FINISH, end=False)])
def test_openai_either_terminal_signal_is_enough(stream):
    urlopen, calls = _seq(stream)
    with patch("urllib.request.urlopen", urlopen):
        out = _openai().chat_streamed(dict(PAYLOAD, model="@bs2"))
    assert _content(out) == "now PROVEN as the"
    assert len(calls) == 1
