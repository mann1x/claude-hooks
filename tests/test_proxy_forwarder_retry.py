"""Tests for the rewritten api-proxy retry mechanism.

Two layers:

- **Pure decision policy** (``claude_hooks.proxy.retry``) — jittered
  backoff bounds, Retry-After parsing (both forms), the wall-clock
  deadline gate, status classification, circuit-breaker open/close. No
  sleeping, no HTTP.
- **Integration** through ``forwarder.forward()`` with ``_forward_attempt``
  and ``time.sleep`` stubbed — asserts the new contract: no whole-pool
  nuke on the retry path, deadline-bounded give-up that passes the
  authentic upstream error through, 429 pass-through, Retry-After
  honoring + clamp, breaker pre-attempt delay, and the flap counters.

Mirrors the mocking style of ``tests/test_caliber_proxy_retry.py``
(reset the process-global singletons per test; stub timing so nothing
actually sleeps).
"""

from __future__ import annotations

import random

import httpx
import pytest

from claude_hooks.proxy import forwarder as fwd
from claude_hooks.proxy import retry as rt
from claude_hooks.proxy.forwarder import UpstreamResult, _RetryableStatus, forward


@pytest.fixture(autouse=True)
def _clean_retry_env(monkeypatch):
    """Each test starts from default knobs + fresh process-global state."""
    for k in list(__import__("os").environ):
        if k.startswith("CLAUDE_HOOKS_PROXY_"):
            monkeypatch.delenv(k, raising=False)
    rt.reset_state()
    yield
    rt.reset_state()


@pytest.fixture
def captured_sleeps(monkeypatch):
    """Replace ``forwarder.time.sleep`` with a recorder (no real sleep)."""
    waits: list[float] = []
    monkeypatch.setattr(fwd.time, "sleep", lambda s: waits.append(s))
    return waits


def _ok_result(status: int = 200) -> UpstreamResult:
    return UpstreamResult(
        status=status, reason="OK",
        headers={"content-type": "application/json"},
        first_chunk=b'{"ok":true}', body_iter=iter([]),
        stats={"bytes_read": 11, "http_version": "HTTP/2"}, sse_tail=None,
    )


def _seq_attempt(monkeypatch, outcomes):
    """Stub ``_forward_attempt`` to yield ``outcomes`` in order. Each
    outcome is either an Exception instance to raise or an int status to
    return (200) / a ``_RetryableStatus`` instance to raise. Returns a
    counter dict."""
    calls = {"n": 0}
    seq = list(outcomes)

    def attempt(client, method, url, headers, body, retry_status=None):
        i = calls["n"]
        calls["n"] += 1
        item = seq[i] if i < len(seq) else _ok_result(200)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return _ok_result(item)
        return item

    monkeypatch.setattr(fwd, "_forward_attempt", attempt)
    return calls


def _fwd(**kw):
    return forward(
        "http://127.0.0.1:1", "POST", "/v1/messages",
        {"Content-Type": "application/json"}, b'{"x":1}',
        timeout=kw.pop("timeout", 30.0),
    )


# --------------------------------------------------------------------- #
# Pure decision helpers                                                  #
# --------------------------------------------------------------------- #


class TestComputeBackoff:
    def test_no_jitter_is_exponential_capped(self):
        assert rt.compute_backoff(0, 1.0, 20.0, jitter=False) == 1.0
        assert rt.compute_backoff(3, 1.0, 20.0, jitter=False) == 8.0
        assert rt.compute_backoff(10, 1.0, 20.0, jitter=False) == 20.0  # capped

    def test_full_jitter_within_bounds(self):
        r = random.Random(1234)
        for attempt in range(6):
            cap = min(1.0 * (2 ** attempt), 20.0)
            for _ in range(50):
                v = rt.compute_backoff(attempt, 1.0, 20.0, jitter=True, rng=r)
                assert 0.0 <= v <= cap


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert rt.parse_retry_after({"Retry-After": "5"}, 1000.0) == 5.0

    def test_zero_allowed(self):
        assert rt.parse_retry_after({"retry-after": "0"}, 1000.0) == 0.0

    def test_http_date_future(self):
        # 25s in the future from now → ~25 (clamped under cap 30).
        from email.utils import formatdate
        now = 1_000_000.0
        hdr = formatdate(now + 25, usegmt=True)
        v = rt.parse_retry_after({"Retry-After": hdr}, now, cap=30.0)
        assert v is not None and 24.0 <= v <= 26.0

    def test_clamped_to_cap(self):
        assert rt.parse_retry_after({"Retry-After": "999"}, 1000.0, cap=30.0) == 30.0

    def test_negative_is_none(self):
        assert rt.parse_retry_after({"Retry-After": "-3"}, 1000.0) is None

    def test_absent_and_garbage(self):
        assert rt.parse_retry_after({"x": "y"}, 1000.0) is None
        assert rt.parse_retry_after({"Retry-After": "soon-ish"}, 1000.0) is None
        assert rt.parse_retry_after({}, 1000.0) is None
        assert rt.parse_retry_after(None, 1000.0) is None


class TestNextDelay:
    def test_retry_after_wins(self):
        cfg = rt.ApiProxyRetryConfig()
        d = rt.next_delay(0, {"retry-after": "7"}, cfg, 1000.0)
        assert d == 7.0

    def test_honor_disabled_ignores_header(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_HONOR_RETRY_AFTER", "0")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_JITTER", "0")
        cfg = rt.ApiProxyRetryConfig()
        d = rt.next_delay(0, {"retry-after": "7"}, cfg, 1000.0)
        assert d == cfg.base_delay_s  # backoff, not the header


class TestShouldRetry:
    def test_attempt_cap(self):
        cfg = rt.ApiProxyRetryConfig()  # max_attempts 8
        assert rt.should_retry(6, 0.0, 0.1, cfg) is True   # 7th allowed
        assert rt.should_retry(7, 0.0, 0.1, cfg) is False  # 8th is the last

    def test_deadline_gate(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_DEADLINE_S", "10")
        cfg = rt.ApiProxyRetryConfig()
        assert rt.should_retry(0, 5.0, 4.0, cfg) is True    # 9 < 10
        assert rt.should_retry(0, 5.0, 6.0, cfg) is False   # 11 >= 10

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_MAX_ATTEMPTS", "1")
        cfg = rt.ApiProxyRetryConfig()
        assert cfg.disabled() is True
        assert rt.should_retry(0, 0.0, 0.0, cfg) is False


class TestIsRetryableStatus:
    def test_membership(self):
        s = rt.DEFAULT_RETRY_STATUS
        for code in (500, 502, 503, 504, 529):
            assert rt.is_retryable_status(code, s) is True
        for code in (200, 404, 429, 400):
            assert rt.is_retryable_status(code, s) is False


class TestThrottleState:
    def test_opens_at_threshold_and_closes(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_WINDOW_S", "30")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", "3")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_OPEN_S", "10")
        ts = rt.ThrottleState()
        now = 1000.0
        assert ts.record_overload(now) is False
        assert ts.record_overload(now + 1) is False
        assert ts.record_overload(now + 2) is True       # 3rd opens
        assert ts.is_open(now + 5) is True
        assert ts.is_open(now + 13) is False             # past open_s

    def test_success_clears_pressure(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", "2")
        ts = rt.ThrottleState()
        ts.record_overload(1000.0)
        ts.record_success(1000.5)
        # one fresh overload after a clear shouldn't re-open (needs 2)
        assert ts.record_overload(1001.0) is False

    def test_window_decay(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_WINDOW_S", "5")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", "3")
        ts = rt.ThrottleState()
        ts.record_overload(1000.0)
        ts.record_overload(1001.0)
        # 10s later the first two have aged out of the 5s window.
        assert ts.record_overload(1011.0) is False

    def test_extra_delay_floor_and_retry_after_rise(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", "1")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_OPEN_S", "100")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_EXTRA_DELAY_S", "2")
        ts = rt.ThrottleState()
        ts.record_overload(1000.0)
        assert ts.initial_extra_delay(1000.0) == 2.0      # floor
        ts.note_retry_after(8.0)
        assert ts.initial_extra_delay(1000.0) == 8.0      # rises toward RA
        assert ts.initial_extra_delay(2000.0) == 0.0      # closed → 0


# --------------------------------------------------------------------- #
# Integration through forward()                                         #
# --------------------------------------------------------------------- #


class TestForwardRetryIntegration:
    def test_529_then_200(self, monkeypatch, captured_sleeps):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_JITTER", "0")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", "0")
        rt.reset_state()
        calls = _seq_attempt(monkeypatch, [
            _RetryableStatus(529, "Overloaded", b'{"e":1}', {}),
            200,
        ])
        result = _fwd()
        assert result.status == 200
        assert calls["n"] == 2
        snap = rt.counters().snapshot()
        assert snap["upstream_529_total"] == 1
        assert snap["retry_succeeded_total"] == 1

    def test_conn_error_then_200(self, monkeypatch, captured_sleeps):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", "0")
        rt.reset_state()
        calls = _seq_attempt(monkeypatch, [
            httpx.RemoteProtocolError("Server disconnected"),
            200,
        ])
        result = _fwd()
        assert result.status == 200
        assert calls["n"] == 2
        assert rt.counters().snapshot()["upstream_conn_error_total"] == 1

    def test_no_pool_nuke_on_retry_path(self, monkeypatch, captured_sleeps):
        """The retry loop must NEVER call the whole-pool ``_reset_client``
        — that closed sibling sessions' connections + re-tripped the edge
        429 gate. Regression guard for the central fix."""
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", "0")
        rt.reset_state()
        from unittest import mock
        spy = mock.Mock(wraps=fwd._reset_client)
        monkeypatch.setattr(fwd, "_reset_client", spy)
        _seq_attempt(monkeypatch, [
            _RetryableStatus(529, "Overloaded", b'{"e":1}', {}),
            _RetryableStatus(529, "Overloaded", b'{"e":1}', {}),
            httpx.RemoteProtocolError("drop"),
            200,
        ])
        result = _fwd()
        assert result.status == 200
        spy.assert_not_called()

    def test_deadline_stops_before_attempt_cap(self, monkeypatch, captured_sleeps):
        """A persistent 529 with a backoff that would cross the deadline
        stops EARLY (deadline-bound, not the 8-attempt cap) and passes
        the authentic upstream 529 through verbatim."""
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_JITTER", "0")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", "5")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_MAX_DELAY_S", "100")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_DEADLINE_S", "6")
        rt.reset_state()
        hdrs = {"retry-after": "0", "x-keep": "1"}
        calls = _seq_attempt(monkeypatch, [
            _RetryableStatus(529, "Overloaded", b'{"down":1}', hdrs)
            for _ in range(20)
        ])
        result = _fwd()
        assert result.status == 529
        assert result.first_chunk == b'{"down":1}'
        assert result.headers.get("x-keep") == "1"     # headers verbatim
        # base 5 with jitter off; the first retry sleeps ~0 (retry-after 0),
        # but the deadline (6s) bounds well before 20 attempts.
        assert calls["n"] < 20
        assert rt.counters().snapshot()["retry_exhausted_total"] == 1
        assert result.stats.get("retry_outcome") == "exhausted"

    def test_429_passes_through_no_retry(self, monkeypatch, captured_sleeps):
        rt.reset_state()
        from unittest import mock
        spy = mock.Mock(wraps=fwd._reset_client)
        monkeypatch.setattr(fwd, "_reset_client", spy)
        calls = _seq_attempt(monkeypatch, [429])
        result = _fwd()
        assert result.status == 429
        assert calls["n"] == 1                  # never retried
        spy.assert_not_called()
        assert rt.counters().snapshot()["upstream_429_passthrough_total"] == 1

    def test_retry_after_honored(self, monkeypatch, captured_sleeps):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_JITTER", "0")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", "0")
        rt.reset_state()
        _seq_attempt(monkeypatch, [
            _RetryableStatus(529, "Overloaded", b'{"e":1}', {"retry-after": "3"}),
            200,
        ])
        result = _fwd()
        assert result.status == 200
        assert 3.0 in captured_sleeps          # waited the server-told 3s
        assert result.stats.get("retry_after_honored") is True

    def test_retry_after_clamped(self, monkeypatch, captured_sleeps):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_JITTER", "0")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_AFTER_CAP_S", "10")
        rt.reset_state()
        _seq_attempt(monkeypatch, [
            _RetryableStatus(529, "Overloaded", b'{"e":1}', {"retry-after": "999"}),
            200,
        ])
        result = _fwd()
        assert result.status == 200
        assert 10.0 in captured_sleeps         # clamped to cap, not 999

    def test_breaker_pre_attempt_delay(self, monkeypatch, captured_sleeps):
        """When the breaker is already open, forward() sleeps the
        coordinated delay BEFORE its first attempt."""
        import time as _t
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", "1")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_OPEN_S", "100")
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_BREAKER_EXTRA_DELAY_S", "4")
        rt.reset_state()
        # Open the breaker now (monotonic clock shared with forward()).
        rt.record_overload(_t.monotonic(), conn_error=True)
        calls = _seq_attempt(monkeypatch, [200])
        result = _fwd()
        assert result.status == 200
        assert calls["n"] == 1
        assert any(s >= 4.0 for s in captured_sleeps)   # paid the breaker delay

    def test_max_attempts_one_is_single_shot(self, monkeypatch, captured_sleeps):
        monkeypatch.setenv("CLAUDE_HOOKS_PROXY_RETRY_MAX_ATTEMPTS", "1")
        rt.reset_state()
        calls = _seq_attempt(monkeypatch, [
            _RetryableStatus(503, "Bad", b'{"e":1}', {}),
            200,
        ])
        result = _fwd()
        assert result.status == 503            # no retry → synthesized 503
        assert calls["n"] == 1

    def test_snapshot_shape_stable(self):
        snap = rt.snapshot(0.0)
        assert set(snap) == {"upstream_flaps", "throttle"}
        assert "breaker_open_total" in snap["upstream_flaps"]
        assert "open" in snap["throttle"]
