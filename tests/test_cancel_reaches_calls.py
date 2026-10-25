"""A council's cancel reaches the calls already running.

The node gate skips nodes not yet started; a call already retrying a dead
server kept loading the model there for its whole retry budget after the
council was cancelled (arm B, 2026-10-24: one 500 after each failed load,
every ~30 s). The run's cancel rides into every call as ``cancel_check``
(``TracedChat(stop=)``), the client checks it before each attempt and
through every backoff, and the stall monitor raises it rather than
returning nothing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_hooks.get_advice import chat_client as cc
from consultants.engine.stall import CancelledByOrchestrator
from consultants.engine.stall_chat import make_stall_protected_chat_fn
from consultants.engine.trace import TracedChat
from tests.test_chat_client_transport import _http, _ok, _Resp, _Seq


@pytest.fixture(autouse=True)
def _no_slots(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")


def _client():
    return cc.ChatClient("http://stub", max_retries=3, retry_base_delay_s=30.0,
                         retry_max_delay_s=30.0)


def _payload():
    return {"model": "m", "messages": [{"role": "user", "content": "q"}]}


LOAD_FAILED = '{"error":"llama-server reported out-of-memory during startup"}'


class TestTheClient:

    @pytest.mark.parametrize("stream", [False, True])
    def test_a_cancel_ends_the_retry_backoff(self, stream, monkeypatch):
        sleeps = []
        monkeypatch.setattr(cc.time, "sleep", sleeps.append)
        seq = _Seq([_http(500, LOAD_FAILED)] * 4)
        stop = lambda: len(seq.requests) >= 1   # noqa: E731 — cancelled after the first
        with patch("urllib.request.urlopen", seq):
            with pytest.raises(CancelledByOrchestrator):
                if stream:
                    _client().chat_streamed(_payload(), cancel_check=stop)
                else:
                    _client().chat(_payload(), cancel_check=stop)
        # One attempt, and the 30 s backoff was never slept whole.
        assert len(seq.requests) == 1
        assert all(t <= cc.CANCEL_POLL_S for t in sleeps)

    def test_a_cancel_before_the_call_sends_nothing(self):
        seq = _Seq([])
        with patch("urllib.request.urlopen", seq):
            with pytest.raises(CancelledByOrchestrator):
                _client().chat(_payload(), cancel_check=lambda: True)
        assert seq.requests == []

    def test_no_cancel_keeps_the_retries(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(cc.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(cc.time, "sleep",
                            lambda s: clock.__setitem__("t", clock["t"] + s))
        seq = _Seq([_http(500, LOAD_FAILED), _Resp(_ok())])
        with patch("urllib.request.urlopen", seq):
            assert _client().chat(_payload(), cancel_check=lambda: False)
        assert len(seq.requests) == 2

    def test_the_nap_sleeps_in_slices_while_not_cancelled(self, monkeypatch):
        naps = []
        clock = {"t": 0.0}
        monkeypatch.setattr(cc.time, "monotonic", lambda: clock["t"])

        def sleep(s):
            naps.append(s)
            clock["t"] += s
        monkeypatch.setattr(cc.time, "sleep", sleep)
        cc._nap(2.0, lambda: False)
        assert sum(naps) == pytest.approx(2.0)
        assert max(naps) <= cc.CANCEL_POLL_S


class _Takes:
    def __init__(self):
        self.checks = []

    def chat(self, payload, *, cancel_check=None):
        self.checks.append(cancel_check)
        return {"ok": True}

    def chat_streamed(self, payload, *, on_token=None, cancel_check=None):
        self.checks.append(cancel_check)
        return {"ok": True}


class _Stub:
    def chat(self, payload):
        return {"ok": True}


class _Placed:
    """Stands for ``PlacedChat``: forwards everything to its inner."""

    def __init__(self, inner):
        self._placed_inner = inner

    def chat(self, payload, *args, **kwargs):
        return self._placed_inner.chat(payload, *args, **kwargs)


class TestTracedChat:

    def test_the_run_cancel_rides_on_every_call(self):
        flag = {"on": False}
        inner = _Takes()
        t = TracedChat(_Placed(inner), stop=lambda: flag["on"])
        t.chat(_payload())
        check = inner.checks[-1]
        assert check() is False
        flag["on"] = True
        assert check() is True

    def test_the_callers_own_check_is_kept(self):
        inner = _Takes()
        t = TracedChat(inner, stop=lambda: False)
        t.chat_streamed(_payload(), cancel_check=lambda: True)
        assert inner.checks[-1]() is True

    def test_a_streamed_call_gets_the_run_cancel_beside_its_own(self):
        inner = _Takes()
        t = TracedChat(inner, stop=lambda: True)
        t.chat_streamed(_payload(), cancel_check=lambda: False)
        assert inner.checks[-1]() is True

    def test_a_client_without_cancel_check_is_called_as_before(self):
        t = TracedChat(_Stub(), stop=lambda: True)
        assert t.chat(_payload()) == {"ok": True}
        # A client without chat_streamed still has none (the stall layer
        # picks its protection by hasattr).
        assert not hasattr(t, "chat_streamed")


def test_the_stall_monitor_raises_a_callers_cancel():
    def streamed(payload, *, on_token=None, cancel_check=None):
        raise CancelledByOrchestrator("council cancelled")

    fn = make_stall_protected_chat_fn(streamed, stall_threshold_s=60,
                                      hard_cap_s=600, check_interval_s=0.01)
    with pytest.raises(CancelledByOrchestrator):
        fn(_payload())


def test_every_role_client_the_runner_builds_carries_the_run_cancel():
    """A ``TracedChat`` built without ``stop=`` is a role whose calls
    outlive the council's cancel."""
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "consultants/server/runner.py"
    calls = [n for n in ast.walk(ast.parse(src.read_text()))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TracedChat"]
    assert calls
    assert all(any(k.arg == "stop" for k in c.keywords) for c in calls), [
        c.lineno for c in calls if not any(k.arg == "stop" for k in c.keywords)]
