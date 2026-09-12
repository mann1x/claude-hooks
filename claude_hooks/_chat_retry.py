"""Shared chat-call retry policy for Ollama upstream calls.

Both the consultants engine's ``ChatClient``
(``claude_hooks/get_advice/chat_client.py``) and the
caliber-grounding-proxy's upstream client
(``claude_hooks/caliber_proxy/ollama.py``) need to ride out the same
class of cloud Ollama flaps:

- 408 / 429 / 5xx (genuine upstream blips)
- 4xx with cloud-validator-flap bodies ("Value looks like object…",
  "Bad Gateway", "unexpected end")
- 200 OK with empty content + no tool_calls + finish_reason!="length"
  (a soft failure where the upstream silently dropped the response)

This module owns only the **decision policy** (which responses are
retryable, what backoff to wait). The actual HTTP call stays in each
caller — they use different stacks (urllib vs httpx) and have
different mid-retry-side-effects (think-strip, request payload mutation).

Constants live here so a single repo-wide change updates both callers.
"""

from __future__ import annotations

import os
from typing import Optional


# --- Defaults (tunable via env, see ``proxy_retry_config``) -------- #

# Empirical: Ollama Cloud is *very* unreliable on /api/chat — about
# 20% of calls return 5xx, and a non-trivial fraction return 400 with
# upstream parser errors that the same body retried wins seconds later.
# Sized so a single chat call survives a multi-minute cloud incident:
#   1.5 + 3 + 6 + 12 + 24 + 48 + 90·9 = 904 s ≈ 15.1 min total budget.
DEFAULT_MAX_RETRIES = 15
DEFAULT_RETRY_BASE_DELAY_S = 1.5
DEFAULT_RETRY_MAX_DELAY_S = 90.0

# Empty-content retry has its own (smaller) budget so a model that
# *legitimately* produces empty replies doesn't burn the whole 5xx
# budget. Empirical: cloud gemma4 emits empty content roughly 1 in 50
# calls; 5 retries covers ~all observed cases.
DEFAULT_EMPTY_RETRY_MAX = 5


# Retryable HTTP statuses on the upstream POST.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# 4xx response bodies that look like transient cloud parser/validator
# flaps rather than real "you sent bad data" errors. Substring match
# against the response body. Conservative — anything not on this list
# fails fast.
RETRYABLE_4XX_BODY_SUBSTRINGS: tuple[str, ...] = (
    "Value looks like object",       # cloud JSON validator hiccup
    "but can't find closing",        # same
    "unexpected end",                # truncated stream from upstream
    "Bad Gateway",                   # 4xx body wrapping a 502 upstream
)


# --- Decision helpers ---------------------------------------------- #


def is_retryable_status(status: int, body: Optional[str] = None) -> bool:
    """Should we retry given the upstream status code and (optional) body?

    - 408 / 429 / 5xx → always.
    - Other 4xx → only if the body matches a known transient pattern.
    - 2xx / 3xx → not retryable (caller should consume the response).
    """
    if status in RETRYABLE_STATUS:
        return True
    if 400 <= status < 500 and body:
        return any(s in body for s in RETRYABLE_4XX_BODY_SUBSTRINGS)
    return False


def is_retryable_empty_response(response: dict) -> bool:
    """Should we retry a 200-OK response that looks empty?

    Soft-failure pattern: upstream returned 200 with no content and no
    tool_calls and finish_reason is NOT "length" (real truncation).
    Accepts an OpenAI-shaped response dict (``{"choices": [...]}``).

    Returns False on any malformed/unexpected shape so we don't
    accidentally retry on a partial response we already partly
    processed.
    """
    try:
        choice = (response.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        finish_reason = choice.get("finish_reason")
    except (AttributeError, IndexError, TypeError):
        return False

    has_content = bool((content or "").strip())
    has_tools = bool(tool_calls)
    truncated = finish_reason == "length"
    return not has_content and not has_tools and not truncated


def compute_backoff(attempt: int,
                    base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
                    max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S) -> float:
    """Exponential backoff capped at ``max_delay_s``.

    ``attempt`` is 0-indexed (first retry uses ``base_delay_s``).
    """
    return min(base_delay_s * (2 ** attempt), max_delay_s)


# --- Proxy-specific config knobs (env-var driven) ------------------ #


class ProxyRetryConfig:
    """Resolved-at-construction-time retry config for the
    caliber-grounding-proxy. Reads
    ``CALIBER_GROUNDING_RETRY_*`` env vars; falls back to defaults.

    Construct on every request so an operator can change the env and
    restart-free pickup happens on the next call (since the proxy
    threads requests).
    """

    __slots__ = (
        "max_attempts", "base_delay_s", "max_delay_s",
        "retry_on_empty", "empty_max",
    )

    def __init__(self) -> None:
        self.max_attempts = self._int_env(
            "CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS",
            DEFAULT_MAX_RETRIES,
        )
        self.base_delay_s = self._float_env(
            "CALIBER_GROUNDING_RETRY_BASE_DELAY_S",
            DEFAULT_RETRY_BASE_DELAY_S,
        )
        self.max_delay_s = self._float_env(
            "CALIBER_GROUNDING_RETRY_MAX_DELAY_S",
            DEFAULT_RETRY_MAX_DELAY_S,
        )
        self.retry_on_empty = self._bool_env(
            "CALIBER_GROUNDING_RETRY_ON_EMPTY", True,
        )
        self.empty_max = self._int_env(
            "CALIBER_GROUNDING_RETRY_EMPTY_MAX",
            DEFAULT_EMPTY_RETRY_MAX,
        )

    @staticmethod
    def _int_env(key: str, default: int) -> int:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _float_env(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    @staticmethod
    def _bool_env(key: str, default: bool) -> bool:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def disabled(self) -> bool:
        """``CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS=0`` → fully disabled."""
        return self.max_attempts <= 0


# --- Counter snapshot -------------------------------------------- #


class FlapCounters:
    """Process-local counters for the proxy's /health endpoint.

    Captures upstream weather without persisting to disk:

    - ``upstream_5xx_total`` — count of HTTP failures we considered
      retryable (status-based, doesn't include 4xx body matches).
    - ``upstream_retryable_4xx_total`` — count of 4xx bodies that
      matched a transient-pattern substring.
    - ``upstream_empty_total`` — count of 200-OK empties.
    - ``upstream_retry_succeeded_total`` — count of calls where the
      final response landed via at least one retry (i.e. the first
      attempt failed but a later one succeeded). Increment once per
      call, not once per retry.
    - ``upstream_retry_exhausted_total`` — calls that hit the cap and
      failed (caller saw the upstream error).
    """

    __slots__ = (
        "upstream_5xx_total",
        "upstream_retryable_4xx_total",
        "upstream_empty_total",
        "upstream_retry_succeeded_total",
        "upstream_retry_exhausted_total",
    )

    def __init__(self) -> None:
        self.upstream_5xx_total = 0
        self.upstream_retryable_4xx_total = 0
        self.upstream_empty_total = 0
        self.upstream_retry_succeeded_total = 0
        self.upstream_retry_exhausted_total = 0

    def snapshot(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}


# Process-wide singleton for the proxy. Imported by both
# ``caliber_proxy/ollama.py`` (writer) and ``caliber_proxy/server.py``
# (reader, exposes via /health).
_COUNTERS = FlapCounters()


def counters() -> FlapCounters:
    return _COUNTERS
