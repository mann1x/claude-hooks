"""ChatClient waits out a network outage without spending retries,
and only for failures that are one."""
from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from claude_hooks.get_advice import chat_client as cc  # noqa: E402

OK = {"message": {"role": "assistant", "content": "hi"}, "done": True,
      "prompt_eval_count": 1, "eval_count": 1}
DNS_502 = ('{"error":"Post \\"https://ollama.com:443/api/chat\\": dial tcp: '
           'lookup ollama.com: no such host"}')


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))


@pytest.fixture
def client(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cc.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(cc.time, "monotonic", lambda: clock[0])

    def make(failures, **kw):
        seq = list(failures)

        def urlopen(req, timeout=None):
            if seq:
                raise seq.pop(0)
            return _Resp(json.dumps(OK).encode())
        monkeypatch.setattr(cc.urllib.request, "urlopen", urlopen)
        c = cc.ChatClient("http://relay:11434", max_retries=0,
                          retry_base_delay_s=1.0, **kw)
        c._probed_think["m:cloud"] = True
        return c, clock
    return make


def test_an_outage_is_waited_out_without_spending_retries(client):
    fails = [urllib.error.URLError(socket.gaierror("no host")) for _ in range(5)] + [_http(502, DNS_502) for _ in range(5)]
    c, clock = client(fails, outage_wait_s=1800)
    assert c.chat({"model": "m:cloud", "messages": []})["choices"][0]["message"]["content"] == "hi"
    assert clock[0] > 60  # backed off, capped per wait
    assert clock[0] < 1800


def test_off_by_default_so_interactive_callers_fail_fast(client):
    c, _ = client([urllib.error.URLError(socket.gaierror("no host"))])
    with pytest.raises(urllib.error.URLError):
        c.chat({"model": "m:cloud", "messages": []})


def test_a_spent_budget_falls_back_to_the_ordinary_retries(client):
    fails = [urllib.error.URLError(ConnectionRefusedError())] * 200
    c, clock = client(fails, outage_wait_s=120)
    with pytest.raises(urllib.error.URLError):
        c.chat({"model": "m:cloud", "messages": []})
    assert 120 <= clock[0] < 200


def test_a_slow_model_is_not_an_outage():
    assert not cc.is_outage(TimeoutError("read timed out"))
    assert not cc.is_outage(_http(502, '{"error":"model overloaded"}'), "model overloaded")
    assert cc.is_outage(_http(502, DNS_502), DNS_502)
    assert cc.is_outage(ConnectionResetError())


def test_the_streamed_path_waits_too_and_honours_cancel(client, monkeypatch):
    fails = [urllib.error.URLError(socket.gaierror("no host"))] * 3
    c, _ = client(fails, outage_wait_s=600)
    monkeypatch.setattr(c, "_consume_ndjson", lambda resp, **kw: OK)
    assert c.chat_streamed({"model": "m:cloud", "messages": []})["choices"]
    c2, _ = client([urllib.error.URLError(socket.gaierror("no host"))] * 3,
                   outage_wait_s=600)
    from consultants.engine.stall import CancelledByOrchestrator
    with pytest.raises(CancelledByOrchestrator):
        c2.chat_streamed({"model": "m:cloud", "messages": []},
                         cancel_check=lambda: True)
