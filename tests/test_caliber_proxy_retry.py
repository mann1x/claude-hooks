"""Tests for the caliber-grounding-proxy cloud-resilience retry layer.

Covers:

- Retryable HTTP statuses (5xx) get retried with backoff and the
  successful follow-up wins.
- Retryable 4xx body patterns get retried.
- Non-retryable 4xx (genuine bad request) fails fast.
- 200-OK with empty content + no tool_calls + finish_reason!="length"
  is treated as soft failure and retried.
- 200-OK with empty content but ``finish_reason=length`` is NOT
  retried (legitimate truncation).
- ``CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS=0`` disables retries entirely.
- Retry-exhaustion path raises ``UpstreamError``.
- Process-wide flap counters increment correctly.
- Decision helpers (pure functions) round-trip the documented inputs.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import httpx
import pytest

from claude_hooks import _chat_retry
from claude_hooks._chat_retry import (
    DEFAULT_EMPTY_RETRY_MAX,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_S,
    DEFAULT_RETRY_MAX_DELAY_S,
    FlapCounters,
    ProxyRetryConfig,
    compute_backoff,
    is_retryable_empty_response,
    is_retryable_status,
)
from claude_hooks.caliber_proxy import ollama


# --------------------------------------------------------------------- #
# Decision-helper unit tests (pure functions, no HTTP)                  #
# --------------------------------------------------------------------- #


class TestIsRetryableStatus:
    def test_5xx_retryable(self):
        for code in (500, 502, 503, 504):
            assert is_retryable_status(code) is True

    def test_408_429_retryable(self):
        assert is_retryable_status(408) is True
        assert is_retryable_status(429) is True

    def test_2xx_3xx_not_retryable(self):
        for code in (200, 201, 204, 301, 302):
            assert is_retryable_status(code) is False

    def test_genuine_4xx_not_retryable(self):
        # 400 with body that doesn't match any transient pattern.
        assert is_retryable_status(400, "invalid request: bad model") is False
        assert is_retryable_status(404, "model not found") is False

    def test_4xx_with_transient_body_retryable(self):
        assert is_retryable_status(
            400, '{"error":"Value looks like object, but cant find closing"}'
        ) is True
        assert is_retryable_status(
            502, "Bad Gateway: upstream timeout"
        ) is True
        assert is_retryable_status(
            400, "unexpected end of JSON input"
        ) is True


class TestIsRetryableEmptyResponse:
    def _resp(self, content: Any, finish_reason: str = "stop",
              tool_calls: Any = None) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        return {
            "choices": [{"message": msg, "finish_reason": finish_reason}],
        }

    def test_empty_content_no_tools_stop_is_retryable(self):
        assert is_retryable_empty_response(self._resp("")) is True
        assert is_retryable_empty_response(self._resp(None)) is True
        assert is_retryable_empty_response(self._resp("   \n  ")) is True

    def test_truncation_not_retryable(self):
        # finish_reason=length is real truncation (model hit token cap),
        # NOT a soft failure — must not retry.
        assert is_retryable_empty_response(
            self._resp("", finish_reason="length")
        ) is False

    def test_content_present_not_retryable(self):
        assert is_retryable_empty_response(self._resp("hello")) is False

    def test_tool_calls_present_not_retryable(self):
        # Empty content but model emitted a tool call — that's a
        # legitimate "I'm calling a tool" response, not a soft failure.
        assert is_retryable_empty_response(
            self._resp("", tool_calls=[{"id": "x", "function": {}}])
        ) is False

    def test_malformed_treated_as_soft_failure(self):
        # When the response has no real content / tool_calls /
        # finish_reason, treat as the cloud-empty soft failure and
        # retry. Covers ``{}``, ``{"choices": []}`` (the helper
        # substitutes ``[{}]`` so choice[0] never IndexErrors), and
        # ``{"choices": [{}]}``. In production these come via
        # ``_to_openai_response``, never raw upstream — but a
        # genuinely malformed-but-shape-valid translation still
        # benefits from the empty-retry budget (capped at 5 attempts
        # by default to avoid spinning).
        assert is_retryable_empty_response({}) is True
        assert is_retryable_empty_response({"choices": []}) is True
        assert is_retryable_empty_response({"choices": [{}]}) is True


class TestComputeBackoff:
    def test_first_retry_uses_base(self):
        assert compute_backoff(0, 1.5, 90.0) == 1.5

    def test_doubles_per_attempt(self):
        assert compute_backoff(1, 1.5, 90.0) == 3.0
        assert compute_backoff(2, 1.5, 90.0) == 6.0
        assert compute_backoff(3, 1.5, 90.0) == 12.0

    def test_caps_at_max(self):
        # 1.5 * 2^7 = 192 → capped at 90.
        assert compute_backoff(7, 1.5, 90.0) == 90.0
        assert compute_backoff(20, 1.5, 90.0) == 90.0


# --------------------------------------------------------------------- #
# ProxyRetryConfig env-var tests                                        #
# --------------------------------------------------------------------- #


class TestProxyRetryConfig:
    def test_defaults(self, monkeypatch):
        for k in (
            "CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS",
            "CALIBER_GROUNDING_RETRY_BASE_DELAY_S",
            "CALIBER_GROUNDING_RETRY_MAX_DELAY_S",
            "CALIBER_GROUNDING_RETRY_ON_EMPTY",
            "CALIBER_GROUNDING_RETRY_EMPTY_MAX",
        ):
            monkeypatch.delenv(k, raising=False)
        c = ProxyRetryConfig()
        assert c.max_attempts == DEFAULT_MAX_RETRIES
        assert c.base_delay_s == DEFAULT_RETRY_BASE_DELAY_S
        assert c.max_delay_s == DEFAULT_RETRY_MAX_DELAY_S
        assert c.retry_on_empty is True
        assert c.empty_max == DEFAULT_EMPTY_RETRY_MAX
        assert c.disabled() is False

    def test_overrides(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS", "3")
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_BASE_DELAY_S", "0.1")
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_DELAY_S", "0.5")
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_ON_EMPTY", "0")
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_EMPTY_MAX", "2")
        c = ProxyRetryConfig()
        assert c.max_attempts == 3
        assert c.base_delay_s == 0.1
        assert c.max_delay_s == 0.5
        assert c.retry_on_empty is False
        assert c.empty_max == 2

    def test_disabled_when_zero(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS", "0")
        c = ProxyRetryConfig()
        assert c.disabled() is True

    def test_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS", "not-a-number")
        c = ProxyRetryConfig()
        assert c.max_attempts == DEFAULT_MAX_RETRIES


# --------------------------------------------------------------------- #
# End-to-end: chat_completions with mocked httpx client                 #
# --------------------------------------------------------------------- #


def _ok_response(content: str = "hello", model: str = "test-model") -> dict:
    """An Ollama-shaped 200 OK response body."""
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 3,
    }


def _empty_response(model: str = "test-model") -> dict:
    """Ollama 200 OK with content="" — the cloud soft-failure mode."""
    return {
        "model": model,
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 0,
    }


class _MockResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        if isinstance(body, dict):
            self._json = body
            self.text = json.dumps(body)
        else:
            self._json = None
            self.text = body

    def json(self):
        if self._json is None:
            raise json.JSONDecodeError("not json", self.text, 0)
        return self._json


@pytest.fixture
def fast_retry_env(monkeypatch):
    """Cap delays so tests don't actually sleep more than a few ms."""
    monkeypatch.setenv("CALIBER_GROUNDING_RETRY_BASE_DELAY_S", "0.001")
    monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_DELAY_S", "0.01")


@pytest.fixture
def reset_counters():
    """Each test starts with a fresh counter state."""
    fresh = FlapCounters()
    with mock.patch.object(_chat_retry, "_COUNTERS", fresh):
        yield fresh


@pytest.fixture
def mock_client(monkeypatch):
    """Replace the proxy's pooled httpx.Client with a configurable mock."""
    client = mock.MagicMock(spec=httpx.Client)
    monkeypatch.setattr(ollama, "_CLIENT", client)
    return client


def _payload() -> dict:
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
    }


class TestChatCompletionsRetry:
    def test_5xx_retried_then_success(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        # 503 then 200.
        mock_client.post.side_effect = [
            _MockResponse(503, "service unavailable"),
            _MockResponse(200, _ok_response("ok!")),
        ]
        out = ollama.chat_completions(_payload())
        assert out["choices"][0]["message"]["content"] == "ok!"
        assert mock_client.post.call_count == 2
        assert reset_counters.upstream_5xx_total == 1
        assert reset_counters.upstream_retry_succeeded_total == 1
        assert reset_counters.upstream_retry_exhausted_total == 0

    def test_retryable_4xx_body_pattern_retried(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        # 400 with cloud-validator-flap body pattern.
        mock_client.post.side_effect = [
            _MockResponse(400, {
                "error": "Value looks like object, but cant find closing }",
            }),
            _MockResponse(200, _ok_response()),
        ]
        out = ollama.chat_completions(_payload())
        assert out["choices"][0]["message"]["content"] == "hello"
        assert reset_counters.upstream_retryable_4xx_total == 1

    def test_non_retryable_4xx_fails_fast(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        mock_client.post.return_value = _MockResponse(404, {
            "error": "model not found",
        })
        with pytest.raises(ollama.UpstreamError) as exc:
            ollama.chat_completions(_payload())
        assert exc.value.status == 404
        # Single attempt — no retry.
        assert mock_client.post.call_count == 1
        assert reset_counters.upstream_5xx_total == 0
        assert reset_counters.upstream_retry_exhausted_total == 0

    def test_5xx_exhausts_to_upstream_error(
        self, mock_client, fast_retry_env, monkeypatch, reset_counters,
    ):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS", "2")
        # 3 sequential 503s — first attempt + 2 retries — then exhausted.
        mock_client.post.return_value = _MockResponse(503, "down")
        with pytest.raises(ollama.UpstreamError) as exc:
            ollama.chat_completions(_payload())
        assert exc.value.status == 503
        assert mock_client.post.call_count == 3  # 1 initial + 2 retries
        assert reset_counters.upstream_5xx_total == 2
        assert reset_counters.upstream_retry_exhausted_total == 1
        assert reset_counters.upstream_retry_succeeded_total == 0

    def test_empty_content_retried_then_filled(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        mock_client.post.side_effect = [
            _MockResponse(200, _empty_response()),
            _MockResponse(200, _ok_response("recovered")),
        ]
        out = ollama.chat_completions(_payload())
        assert out["choices"][0]["message"]["content"] == "recovered"
        assert mock_client.post.call_count == 2
        assert reset_counters.upstream_empty_total == 1
        assert reset_counters.upstream_retry_succeeded_total == 1

    def test_empty_with_finish_length_not_retried(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        # Truncation is real — model hit num_predict cap. Don't retry.
        truncated = _empty_response()
        truncated["done_reason"] = "length"
        mock_client.post.return_value = _MockResponse(200, truncated)
        out = ollama.chat_completions(_payload())
        assert out["choices"][0]["finish_reason"] == "length"
        assert mock_client.post.call_count == 1
        assert reset_counters.upstream_empty_total == 0

    def test_disabled_via_env(
        self, mock_client, fast_retry_env, monkeypatch, reset_counters,
    ):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS", "0")
        mock_client.post.return_value = _MockResponse(503, "down")
        with pytest.raises(ollama.UpstreamError) as exc:
            ollama.chat_completions(_payload())
        assert exc.value.status == 503
        # No retry — single attempt.
        assert mock_client.post.call_count == 1

    def test_empty_disabled_via_env(
        self, mock_client, fast_retry_env, monkeypatch, reset_counters,
    ):
        monkeypatch.setenv("CALIBER_GROUNDING_RETRY_ON_EMPTY", "0")
        mock_client.post.return_value = _MockResponse(200, _empty_response())
        out = ollama.chat_completions(_payload())
        # Empty response passed through to caller with no retry.
        assert out["choices"][0]["message"]["content"] == ""
        assert mock_client.post.call_count == 1
        assert reset_counters.upstream_empty_total == 0

    def test_network_error_retried(
        self, mock_client, fast_retry_env, reset_counters,
    ):
        # First call raises a transport error, second succeeds.
        mock_client.post.side_effect = [
            httpx.ConnectError("boom"),
            _MockResponse(200, _ok_response("ok")),
        ]
        out = ollama.chat_completions(_payload())
        assert out["choices"][0]["message"]["content"] == "ok"
        assert reset_counters.upstream_5xx_total == 1
        assert reset_counters.upstream_retry_succeeded_total == 1

    def test_first_attempt_success_no_retry_counter(
        self, mock_client, reset_counters,
    ):
        # Clean path — counters stay at 0.
        mock_client.post.return_value = _MockResponse(200, _ok_response())
        ollama.chat_completions(_payload())
        snap = reset_counters.snapshot()
        assert snap == {
            "upstream_5xx_total": 0,
            "upstream_retryable_4xx_total": 0,
            "upstream_empty_total": 0,
            "upstream_retry_succeeded_total": 0,
            "upstream_retry_exhausted_total": 0,
        }
