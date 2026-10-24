"""PolyKV placement on the chat wire (plan F1).

A payload without ``placement`` is sent exactly as before, on both
clients; with it, opencoti direct gets top-level fields and xOllama its
native ``session_id`` + ``placement{}``. The reply's grant and share land
in ``timings["polykv"]``, and prefill progress frames count as liveness.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import chat_client as cc
from claude_hooks.polykv import wire

PLACE = {"session_id": "s1~r2", "pool_id": "0", "num_ctx": 65536,
         "num_ctx_min": 32768, "keepalive": True, "return_progress": True,
         "chat_template_kwargs": {"enable_thinking": False}}


class _Resp:
    def __init__(self, body=None, lines=None, headers=None):
        self._body = json.dumps(body or {}).encode()
        self._lines = lines or []
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


class _Seq:
    def __init__(self, items):
        self.items, self.requests = list(items), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return self.items.pop(0)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)


def _payload(**extra):
    return {"model": "q", "messages": [{"role": "user", "content": "hi"}],
            "options": {"num_predict": 64, "temperature": 0.7}, **extra}


def _oc():
    c = cc.OpenAICompatChatClient("http://oc:8240/v1")
    c._context_length["q"] = 131072
    return c


# ----------------------------------------------------------- off is off

@pytest.mark.parametrize("stream", [False, True])
def test_no_placement_is_byte_identical_on_both_wires(stream):
    oc = _oc()
    base = oc._prepare(_payload(), strip_think=False, stream=stream)
    assert oc._prepare(_payload(placement={}), strip_think=False,
                       stream=stream) == base
    assert not {"session_id", "pool_id", "num_ctx", "cache_prompt",
                "return_progress"} & set(base)
    ol = cc.ChatClient("http://ol:11434")
    nb = ol._to_ollama(_payload())
    assert ol._to_ollama(_payload(placement={})) == nb
    assert "session_id" not in nb and "placement" not in nb


# ------------------------------------------------------------- the wires

def test_opencoti_gets_top_level_fields_pool_zero_included():
    body = _oc()._prepare(_payload(placement=PLACE), strip_think=False,
                          stream=True)
    assert body["session_id"] == "s1~r2" and body["cache_prompt"] is True
    assert body["pool_id"] == 0                          # pool 0 is a pool
    assert (body["num_ctx"], body["num_ctx_min"]) == (65536, 32768)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["stream_options"] == {"include_usage": True, "keepalive": True}
    assert body["return_progress"] is True
    plain = _oc()._prepare(_payload(placement=PLACE), strip_think=False,
                           stream=False)
    assert "stream_options" not in plain and "return_progress" not in plain


def test_continue_pool_needs_a_pool():
    body = wire.to_openai({}, {"pool_id": 4, "continue_pool": True}, stream=False)
    assert body == {"pool_id": 4, "continue_pool": True}
    with pytest.raises(ValueError):
        wire.to_openai({}, {"continue_pool": True}, stream=False)


def test_xollama_gets_session_id_and_placement_not_options(caplog):
    body = cc.ChatClient("http://xo:22434")._to_ollama(_payload(placement=PLACE))
    assert body["session_id"] == "s1~r2"
    assert body["placement"] == {"pool_id": 0, "num_ctx": 65536,
                                 "num_ctx_min": 32768}
    assert "num_ctx" not in body["options"]              # never the load size
    with caplog.at_level(logging.WARNING, logger="claude_hooks.polykv"):
        wire.to_ollama({}, {"pool_id": "2", "continue_pool": True})
    assert "continue_pool" in caplog.text


# -------------------------------------------------------- the observation

def test_reply_grant_and_share_land_in_timings():
    seq = _Seq([_Resp({"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}],
                       "opencoti": {"pool_id": 0, "n_pool_shared": 5917,
                                    "pool_match": 5917, "pool_len": 5917},
                       "timings": {"cache_n": 5917, "prompt_n": 33}},
                      headers={"X-Context-Window": "65536",
                               "X-OpenCoti-Boot-Id": "b1"})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = _oc().chat(_payload(placement=PLACE))
    pk = out["timings"]["polykv"]
    assert pk == {"session_id": "s1~r2", "pool_id": "0", "boot_id": "b1",
                  "context_window": 65536, "n_pool_shared": 5917,
                  "pool_match": 5917, "pool_len": 5917, "cache_n": 5917}
    sent = json.loads(seq.requests[0].data)
    assert sent["pool_id"] == 0 and sent["session_id"] == "s1~r2"


def test_a_silent_miss_is_recorded_as_a_warning():
    seq = _Seq([_Resp({"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}],
                       "opencoti": {"pool_id": 0, "n_pool_shared": 0,
                                    "pool_match": 3, "pool_len": 6000}})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = _oc().chat(_payload(placement=PLACE))
    assert "n_pool_shared 0" in out["timings"]["polykv"]["warnings"][0]


def test_no_placement_records_no_polykv():
    seq = _Seq([_Resp({"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}],
                       "opencoti": {"pool_id": 0, "n_pool_shared": 9}})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = _oc().chat(_payload())
    assert "polykv" not in out["timings"]


def test_stream_keeps_the_block_and_counts_prefill_as_liveness():
    lines = [b'data: {"prompt_progress": {"processed": 512, "total": 6000}}\n',
             b": keepalive prefill 1024/6000\n",
             b'data: ' + json.dumps({"choices": [{"delta": {"content": "hi"},
                                                  "finish_reason": "stop"}],
                                     "opencoti": {"pool_id": 0,
                                                  "n_pool_shared": 5900}}).encode()
             + b"\n", b"data: [DONE]\n"]
    ticks = []
    with patch("urllib.request.urlopen", side_effect=_Seq([_Resp(lines=lines)])):
        out = _oc().chat_streamed(_payload(placement=PLACE),
                                  on_token=lambda t: ticks.append(t))
    assert ticks[:2] == ["", ""]                         # progress, then keepalive
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["timings"]["polykv"]["n_pool_shared"] == 5900


def test_xollama_reply_records_the_cached_count():
    seq = _Seq([_Resp({"message": {"role": "assistant", "content": "ok"},
                       "done": True, "done_reason": "stop",
                       "prompt_eval_count": 30,
                       "prompt_eval_cached_count": 2840},
                      headers={"X-Context-Window": "32768"})])
    c = cc.ChatClient("http://xo:22434")
    c._probed_think["q"] = True
    with patch("urllib.request.urlopen", side_effect=seq):
        out = c.chat(_payload(placement={"session_id": "s~r0", "pool_id": "3"}))
    pk = out["timings"]["polykv"]
    assert pk["cache_n"] == 2840 and pk["context_window"] == 32768
    assert "warnings" not in pk
