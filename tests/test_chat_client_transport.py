"""ChatClient transport hardening (plan A2) and per-call timings (A5)."""

from __future__ import annotations

import email.utils
import io
import json
import time
import urllib.error
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import chat_client as cc
from claude_hooks.get_advice.chat_client import ChatClient, ChatFault


class _Resp:
    def __init__(self, body: dict, headers: dict | None = None):
        self._raw = json.dumps(body).encode()
        self.headers = headers or {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw

    def __iter__(self):
        return iter([self._raw + b"\n"])


def _http(code: int, body: str, headers: dict | None = None):
    return urllib.error.HTTPError("http://stub/api/chat", code, "x",
                                  headers or {}, io.BytesIO(body.encode()))


def _ok(content="hi", **extra):
    d = {"message": {"role": "assistant", "content": content},
         "done": True, "done_reason": "stop",
         "prompt_eval_count": 100, "eval_count": 20}
    d.update(extra)
    return d


class _Seq:
    def __init__(self, items):
        self.items = list(items)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if not self.items:
            raise AssertionError("unexpected extra call")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep_no_slots(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    with cc._ceilings_lock:
        cc._learned_ceilings.clear()
    yield sleeps


def _client():
    return ChatClient("http://stub", max_retries=3, retry_base_delay_s=1.0,
                      retry_max_delay_s=8.0)


def _sent(seq, i=-1):
    return json.loads(seq.requests[i].data)


@pytest.mark.parametrize("stream", [False, True])
def test_retry_after_seconds_is_honoured(stream, _no_sleep_no_slots):
    seq = _Seq([_http(429, "slow down", {"Retry-After": "7"}), _Resp(_ok())])
    with patch("urllib.request.urlopen", side_effect=seq):
        c = _client()
        out = c.chat_streamed({"model": "m", "messages": []}) if stream \
            else c.chat({"model": "m", "messages": []})
    assert out["choices"][0]["message"]["content"] == "hi"
    assert _no_sleep_no_slots == [7.0]


def test_retry_after_is_capped_and_http_date_parsed(_no_sleep_no_slots):
    later = email.utils.formatdate(time.time() + 3600, usegmt=True)
    seq = _Seq([_http(503, "busy", {"Retry-After": later}), _Resp(_ok())])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "m", "messages": []})
    assert _no_sleep_no_slots == [cc.RETRY_AFTER_MAX_S]


def test_no_retry_after_keeps_backoff(_no_sleep_no_slots):
    seq = _Seq([_http(429, "slow"), _Resp(_ok())])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "m", "messages": []})
    assert _no_sleep_no_slots == [1.0]


def test_empty_reply_is_asked_again_without_spending_a_retry():
    seq = _Seq([_Resp(_ok(content="")), _Resp(_ok(content=""))
                , _Resp(_ok("answer"))])
    c = ChatClient("http://stub", max_retries=0)
    with patch("urllib.request.urlopen", side_effect=seq):
        out = c.chat({"model": "m", "messages": []})
    assert out["choices"][0]["message"]["content"] == "answer"
    assert out["timings"]["empty_retries"] == 2


def test_empty_reply_is_reasked_with_the_reminder_once():
    # Plan D15: the resend is not identical; the reminder is added once.
    seq = _Seq([_Resp(_ok(content="")), _Resp(_ok(content="")),
                _Resp(_ok("answer"))])
    with patch("urllib.request.urlopen", side_effect=seq):
        ChatClient("http://stub", max_retries=0).chat(
            {"model": "m", "messages": [{"role": "user", "content": "q"}]})
    first, second, third = (_sent(seq, i)["messages"] for i in range(3))
    assert first == [{"role": "user", "content": "q"}]
    assert second != first, "the empty reply was resent unchanged"
    assert second[-1] == {"role": "user", "content": cc.EMPTY_TURN_REMINDER}
    assert third == second


def test_empty_reply_gives_up_after_two_and_returns_it():
    seq = _Seq([_Resp(_ok(content=""))] * 3)
    with patch("urllib.request.urlopen", side_effect=seq):
        out = ChatClient("http://stub", max_retries=0).chat(
            {"model": "m", "messages": []})
    assert out["choices"][0]["message"]["content"] == ""


def test_reasoning_only_or_length_cut_is_not_empty():
    assert not cc._is_empty_reply(
        {"message": {"content": "", "thinking": "hmm"}, "done_reason": "stop"})
    assert not cc._is_empty_reply(
        {"message": {"content": ""}, "done_reason": "length"})
    assert not cc._is_empty_reply(
        {"message": {"content": "", "tool_calls": [{}]}, "done_reason": "stop"})


def test_learned_output_ceiling_retries_once_and_is_remembered():
    refusal = _http(400, '{"error":"max_tokens exceeds maximum output tokens (65536)"}')
    seq = _Seq([refusal, _Resp(_ok()), _Resp(_ok())])
    c = ChatClient("http://stub", max_retries=0)
    payload = {"model": "m:cloud", "messages": [], "options": {"num_predict": 96000}}
    with patch("urllib.request.urlopen", side_effect=seq):
        c.chat(payload)
        assert _sent(seq)["options"]["num_predict"] == 65536
        assert c.learned_output_ceiling("m:cloud") == 65536
        c.chat(payload)  # second call is clamped before sending
        assert _sent(seq)["options"]["num_predict"] == 65536
    assert len(seq.requests) == 3


@pytest.mark.parametrize("code,body,fault", [
    (404, '{"error":"model \'nope\' not found"}', cc.MODEL_MISSING),
    (400, "prompt is too long for the context window", cc.CONTEXT_OVERFLOW),
    (401, "unauthorized", cc.BAD_REQUEST),
])
def test_final_failures_carry_their_fault_class(code, body, fault):
    seq = _Seq([_http(code, body)])
    with patch("urllib.request.urlopen", side_effect=seq):
        with pytest.raises(ChatFault) as ei:
            _client().chat({"model": "m", "messages": []})
    assert ei.value.fault_class == fault
    assert ei.value.status == code
    assert isinstance(ei.value, RuntimeError)
    assert f"HTTP {code}" in str(ei.value)
    assert len(seq.requests) == 1  # model-missing never spends a retry


def test_transport_fault_after_retries():
    seq = _Seq([_http(502, "bad gateway")] * 4)
    with patch("urllib.request.urlopen", side_effect=seq):
        with pytest.raises(ChatFault) as ei:
            _client().chat({"model": "m", "messages": []})
    assert ei.value.fault_class == cc.TRANSPORT


def test_timings_from_ollama_durations_and_granted_window():
    body = _ok(load_duration=5_000_000, prompt_eval_duration=500_000_000,
               eval_duration=1_000_000_000, total_duration=1_600_000_000)
    seq = _Seq([_Resp(body, headers={"X-Context-Window": "32768"})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = ChatClient("http://stub").chat({"model": "m", "messages": []})
    t = out["timings"]
    assert t["prompt_eval_ms"] == 500 and t["eval_ms"] == 1000
    assert t["load_ms"] == 5
    assert t["prefill_tok_s"] == 200.0 and t["decode_tok_s"] == 20.0
    assert t["granted_window"] == 32768
    assert t["attempts"] == 1 and "queue_ms" in t


def test_streamed_timings_record_time_to_first_token():
    lines = [json.dumps({"message": {"role": "assistant", "content": "a"},
                         "done": False}).encode() + b"\n",
             json.dumps(_ok(content="")).encode() + b"\n"]

    class _Stream(_Resp):
        def __iter__(self):
            return iter(lines)

    seq = _Seq([_Stream({})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = ChatClient("http://stub").chat_streamed({"model": "m", "messages": []})
    assert out["choices"][0]["message"]["content"] == "a"
    assert "ttft_ms" in out["timings"]


def test_api_key_and_base_url_normalisation():
    c = ChatClient("http://h:11434/api/", api_key="k")
    assert c.base_url == "http://h:11434"
    seq = _Seq([_Resp(_ok())])
    with patch("urllib.request.urlopen", side_effect=seq):
        c.chat({"model": "m", "messages": []})
    assert seq.requests[0].get_header("Authorization") == "Bearer k"
    seq2 = _Seq([_Resp(_ok())])
    with patch("urllib.request.urlopen", side_effect=seq2):
        ChatClient("http://h").chat({"model": "m", "messages": []})
    assert seq2.requests[0].get_header("Authorization") is None
