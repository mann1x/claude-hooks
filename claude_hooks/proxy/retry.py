"""Decision policy + process-global throttle state for the API proxy.

Mirrors the shape of :mod:`claude_hooks._chat_retry` (decision policy
only — the HTTP loop stays in the caller) but is specific to the
transparent ``api.anthropic.com`` proxy in :mod:`claude_hooks.proxy.forwarder`.

What changed from the old ad-hoc retry logic in ``forwarder.py``
(the "Apr-27 storm" code) and *why*:

- **Jittered exponential backoff** (full jitter) instead of the old
  ``min(0.15 * (attempt+1), 0.5)`` sub-second fixed step. Ten retries
  in ~5 s hammered an already-overloaded backend; spaced jittered
  attempts ride the overload out the way a well-behaved client does.
- **``Retry-After`` is honored** (integer-seconds *and* HTTP-date
  forms). The header is already on the buffered retryable response;
  the old loop ignored it.
- **A wall-clock deadline is the primary bound**, with attempt count
  only a safety cap. "Keep retrying so Claude Code's own client never
  exhausts *its* budget" means holding the request for a generous
  window — but with proper spacing, not a flood.
- **A process-global circuit breaker (OFF by default).** When the
  upstream client was a single shared HTTP/2 pool, every session's
  request flowed through one multiplexed connection, so a burst of 529 /
  connection drops on it signalled trouble for *all* sessions and a
  coordinated pre-attempt cooldown was protective. The default upstream
  client is now an HTTP/1.1 keepalive pool (mimicking Claude Code) where
  each session rides its own connection — a drop is isolated, and a
  global cooldown just penalises healthy sessions for one sibling's blip
  (that *was* the "throttle engages as soon as a 2nd session runs"
  failure mode). So the breaker is disabled unless
  ``CLAUDE_HOOKS_PROXY_BREAKER_ENABLED=1`` (worth it only under the h2
  rollback, ``CLAUDE_HOOKS_PROXY_UPSTREAM_HTTP=2``). See ``breaker_enabled``.

- **A surgical connection-error retry budget**, separate from the 5xx
  overload budget. A dropped/refused connection recovers by landing the
  next attempt on a fresh pool connection — a couple of fast (sub-second)
  retries, not the minute-long ride-out an upstream 529 brownout wants.

This module is **pure**: no sleeping, no HTTP, no I/O. ``forwarder.py``
owns ``time.sleep`` + the ``httpx`` calls and feeds wall-clock +
outcomes back in. That keeps every decision unit-testable without
timing, exactly like ``_chat_retry``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Optional

log = logging.getLogger("claude_hooks.proxy.retry")


# --- Defaults (tunable via env, see ``ApiProxyRetryConfig``) --------- #

# Wall-clock retry window. Sits under the proxy's read ``timeout``
# (default 120 s, server.py) so the eventual stream still has headroom.
DEFAULT_RETRY_DEADLINE_S = 90.0
# Exponential backoff base + cap. base * 2**attempt, full-jittered.
DEFAULT_RETRY_BASE_DELAY_S = 1.0
DEFAULT_RETRY_MAX_DELAY_S = 20.0
# Attempt-count safety net. Sized HIGH on purpose: the wall-clock
# deadline is meant to be the real bound, and a low cap undercuts it —
# live 2026-06-02 throttle data showed an 8-cap giving up at 45-60s
# (well under the 90s deadline) and wasting ride-out time. With
# 20s-capped jittered backoff you physically can't fit more than ~10-12
# attempts into the deadline anyway, so this just lets the deadline win.
DEFAULT_RETRY_MAX_ATTEMPTS = 15
# A hostile / buggy ``Retry-After`` can't park a session for minutes.
DEFAULT_RETRY_AFTER_CAP_S = 30.0
DEFAULT_JITTER = True
DEFAULT_HONOR_RETRY_AFTER = True

# 5xx codes we treat as retryable. Excludes 501 (Not Implemented) and
# 505-511 (protocol / semantic errors that won't change on retry).
# 429 is deliberately ABSENT — it's the client's own quota; retrying it
# in the proxy doesn't help (Claude Code + Retry-After handle it) and
# only adds load. It passes straight through.
DEFAULT_RETRY_STATUS = frozenset({
    500, 502, 503, 504,
    520, 521, 522, 523, 524, 525, 526, 527, 529,
})

# Connection-error retry budget (timeouts / network errors / protocol
# faults). SEPARATE from the 5xx-status budget above and deliberately
# *surgical*: a dropped/refused connection on the h1 keepalive pool is
# recovered by landing the next attempt on a fresh connection — that
# wants a couple of fast retries, not the minute-long ride-out the 5xx
# overload budget provides. Few attempts, short deadline, sub-second
# backoff so a genuine upstream brownout surfaces quickly instead of
# stalling Claude Code's own client.
DEFAULT_CONN_RETRY_MAX_ATTEMPTS = 3
DEFAULT_CONN_RETRY_DEADLINE_S = 25.0
DEFAULT_CONN_RETRY_BASE_DELAY_S = 0.25
DEFAULT_CONN_RETRY_MAX_DELAY_S = 2.0

# Circuit-breaker knobs (read once at ThrottleState construction).
DEFAULT_BREAKER_WINDOW_S = 30.0
DEFAULT_BREAKER_THRESHOLD = 4
DEFAULT_BREAKER_OPEN_S = 10.0
DEFAULT_BREAKER_EXTRA_DELAY_S = 2.0
# The cross-session breaker made sense for the old shared HTTP/2 pool:
# one stream-limit fault on the single multiplexed connection signalled
# trouble for *every* session riding it, so a coordinated cooldown was
# protective. With the default HTTP/1.1 keepalive pool each session's
# request rides its own connection — a drop is isolated, and a global
# pre-attempt delay just penalises healthy sessions for one sibling's
# blip (the "throttle engages when a 2nd session runs" report). So the
# breaker is OFF by default and only worth enabling under the h2
# rollback (CLAUDE_HOOKS_PROXY_UPSTREAM_HTTP=2). See ``breaker_enabled``.
DEFAULT_BREAKER_ENABLED = False

# Env knobs retired by this rewrite. Their semantics (sub-second fixed
# backoff, whole-pool nuke) are exactly what amplified the throttle, so
# they are intentionally not honored. Warn once if an operator still
# has them set so the silent behavior change is visible.
_RETIRED_ENV = (
    "CLAUDE_HOOKS_PROXY_RETRY_BACKOFF",
    "CLAUDE_HOOKS_PROXY_RETRY_BACKOFF_MAX",
    "CLAUDE_HOOKS_PROXY_SLOW_5XX_RESET_SEC",
    "CLAUDE_HOOKS_PROXY_5XX_RESET_AFTER",
    "CLAUDE_HOOKS_PROXY_PROTO_RESET_AFTER",
)


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_status_set(raw: Optional[str]) -> frozenset:
    if not raw:
        return DEFAULT_RETRY_STATUS
    out = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return frozenset(out) if out else DEFAULT_RETRY_STATUS


def warn_retired_env() -> None:
    """Log once if any retired env knob is still set."""
    present = [k for k in _RETIRED_ENV if os.environ.get(k)]
    if present:
        log.warning(
            "ignoring retired proxy retry env var(s) %s — superseded by "
            "the jittered-backoff + deadline + circuit-breaker policy "
            "(CLAUDE_HOOKS_PROXY_RETRY_DEADLINE_S / _BASE_DELAY_S / "
            "_MAX_DELAY_S / _MAX_ATTEMPTS, see docs/proxy.md)",
            ", ".join(present),
        )


# --- Decision helpers (pure) --------------------------------------- #


def is_retryable_status(status: int, retry_status: frozenset) -> bool:
    return status in retry_status


def compute_backoff(attempt: int,
                    base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
                    max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S,
                    jitter: bool = DEFAULT_JITTER,
                    rng: Optional[random.Random] = None) -> float:
    """Exponential backoff with **full jitter**, capped at ``max_delay_s``.

    ``attempt`` is 0-indexed (first retry uses ``base_delay_s``). With
    ``jitter`` the wait is uniform in ``[0, cap]``; without it the wait
    is the deterministic exponential ``cap`` (used by the no-jitter
    test path).
    """
    cap = min(base_delay_s * (2 ** attempt), max_delay_s)
    if cap < 0:
        cap = 0.0
    if not jitter:
        return cap
    r = rng if rng is not None else random
    return r.uniform(0.0, cap)


def _get_ci(headers: Optional[dict], key: str) -> Optional[str]:
    """Case-insensitive header lookup over a plain dict."""
    if not headers:
        return None
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return None


def parse_retry_after(headers: Optional[dict],
                      now: float,
                      cap: float = DEFAULT_RETRY_AFTER_CAP_S) -> Optional[float]:
    """Parse an HTTP ``Retry-After`` header into seconds.

    Accepts both forms: a non-negative integer (delta-seconds) and an
    HTTP-date. ``now`` is a unix timestamp (``time.time()``) used to
    turn an HTTP-date into a delta. Returns ``None`` when the header is
    absent / unparseable / negative; otherwise clamps the result to
    ``[0, cap]`` so a hostile value can't park the session.
    """
    val = _get_ci(headers, "retry-after")
    if not val:
        return None
    val = val.strip()
    secs: Optional[float]
    try:
        secs = float(val)
    except ValueError:
        secs = None
    if secs is None:
        # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT").
        try:
            dt = parsedate_to_datetime(val)
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        try:
            secs = dt.timestamp() - now
        except (ValueError, OverflowError, OSError):
            return None
    if secs is None or secs < 0:
        return None
    if secs > cap:
        secs = cap
    return float(secs)


class ApiProxyRetryConfig:
    """Resolved-at-construction retry config for the api proxy forwarder.

    Reads ``CLAUDE_HOOKS_PROXY_*`` env vars; falls back to defaults.
    Construct per-request so env changes are picked up restart-free (the
    proxy threads requests). ``max_attempts`` falls back to the legacy
    ``CLAUDE_HOOKS_PROXY_RETRIES`` name for operators who set it.
    """

    __slots__ = (
        "deadline_s", "base_delay_s", "max_delay_s", "max_attempts",
        "retry_after_cap_s", "jitter", "honor_retry_after", "retry_status",
        "conn_max_attempts", "conn_deadline_s",
        "conn_base_delay_s", "conn_max_delay_s",
    )

    def __init__(self) -> None:
        self.deadline_s = _float_env(
            "CLAUDE_HOOKS_PROXY_RETRY_DEADLINE_S", DEFAULT_RETRY_DEADLINE_S,
        )
        self.base_delay_s = _float_env(
            "CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S", DEFAULT_RETRY_BASE_DELAY_S,
        )
        self.max_delay_s = _float_env(
            "CLAUDE_HOOKS_PROXY_RETRY_MAX_DELAY_S", DEFAULT_RETRY_MAX_DELAY_S,
        )
        # New name wins; legacy CLAUDE_HOOKS_PROXY_RETRIES is the fallback.
        self.max_attempts = _int_env(
            "CLAUDE_HOOKS_PROXY_RETRY_MAX_ATTEMPTS",
            _int_env("CLAUDE_HOOKS_PROXY_RETRIES", DEFAULT_RETRY_MAX_ATTEMPTS),
        )
        self.retry_after_cap_s = _float_env(
            "CLAUDE_HOOKS_PROXY_RETRY_AFTER_CAP_S", DEFAULT_RETRY_AFTER_CAP_S,
        )
        self.jitter = _bool_env("CLAUDE_HOOKS_PROXY_RETRY_JITTER", DEFAULT_JITTER)
        self.honor_retry_after = _bool_env(
            "CLAUDE_HOOKS_PROXY_HONOR_RETRY_AFTER", DEFAULT_HONOR_RETRY_AFTER,
        )
        self.retry_status = _parse_status_set(
            os.environ.get("CLAUDE_HOOKS_PROXY_RETRY_STATUS")
        )
        # Surgical connection-error budget (separate from the 5xx budget).
        self.conn_max_attempts = _int_env(
            "CLAUDE_HOOKS_PROXY_CONN_RETRY_MAX_ATTEMPTS",
            DEFAULT_CONN_RETRY_MAX_ATTEMPTS,
        )
        self.conn_deadline_s = _float_env(
            "CLAUDE_HOOKS_PROXY_CONN_RETRY_DEADLINE_S",
            DEFAULT_CONN_RETRY_DEADLINE_S,
        )
        self.conn_base_delay_s = _float_env(
            "CLAUDE_HOOKS_PROXY_CONN_RETRY_BASE_DELAY_S",
            DEFAULT_CONN_RETRY_BASE_DELAY_S,
        )
        self.conn_max_delay_s = _float_env(
            "CLAUDE_HOOKS_PROXY_CONN_RETRY_MAX_DELAY_S",
            DEFAULT_CONN_RETRY_MAX_DELAY_S,
        )

    def disabled(self) -> bool:
        """``CLAUDE_HOOKS_PROXY_RETRY_MAX_ATTEMPTS=1`` (or 0) → no retries.

        One attempt total = pure pass-through. ``<= 1`` covers both the
        explicit-zero and the explicit-one cases.
        """
        return self.max_attempts <= 1


def next_delay(attempt: int,
               headers: Optional[dict],
               cfg: ApiProxyRetryConfig,
               now: float,
               rng: Optional[random.Random] = None,
               *,
               base_delay_s: Optional[float] = None,
               max_delay_s: Optional[float] = None) -> float:
    """Backoff for the *next* attempt: ``Retry-After`` wins when present
    and honored, else jittered exponential backoff.

    ``base_delay_s`` / ``max_delay_s`` override the cfg's 5xx-budget
    backoff envelope — the forwarder passes the surgical conn-error
    envelope (``cfg.conn_base_delay_s`` / ``cfg.conn_max_delay_s``) on
    the connection-failure path. When ``None`` the 5xx defaults apply.
    """
    if cfg.honor_retry_after:
        ra = parse_retry_after(headers, now, cfg.retry_after_cap_s)
        if ra is not None:
            return ra
    base = cfg.base_delay_s if base_delay_s is None else base_delay_s
    cap = cfg.max_delay_s if max_delay_s is None else max_delay_s
    return compute_backoff(attempt, base, cap, cfg.jitter, rng)


def should_retry(attempt: int,
                 elapsed: float,
                 next_wait: float,
                 cfg: ApiProxyRetryConfig,
                 *,
                 max_attempts: Optional[int] = None,
                 deadline_s: Optional[float] = None) -> bool:
    """Permit another attempt after the 0-indexed ``attempt`` just failed?

    Two gates: the attempt-count safety cap and — the primary bound —
    the wall-clock deadline (we don't start a backoff that would push us
    past it). ``max_attempts`` / ``deadline_s`` override the cfg's
    5xx-budget bounds; the forwarder passes the surgical conn-error
    budget (``cfg.conn_max_attempts`` / ``cfg.conn_deadline_s``) on the
    connection-failure path. ``cfg.disabled()`` (retries globally off
    via ``RETRY_MAX_ATTEMPTS<=1``) still vetoes both paths.
    """
    if cfg.disabled():
        return False
    cap = cfg.max_attempts if max_attempts is None else max_attempts
    deadline = cfg.deadline_s if deadline_s is None else deadline_s
    if attempt + 1 >= cap:
        return False
    return (elapsed + next_wait) < deadline


# --- Counter snapshot ---------------------------------------------- #


class FlapCounters:
    """Process-local counters for the proxy's ``/health`` endpoint.

    Captures upstream weather without persisting to disk. Mirrors the
    shape of ``_chat_retry.FlapCounters`` so the two proxies read alike.
    """

    __slots__ = (
        "upstream_529_total",
        "upstream_5xx_total",
        "upstream_conn_error_total",
        "upstream_429_passthrough_total",
        "retry_succeeded_total",
        "retry_exhausted_total",
        "breaker_open_total",
    )

    def __init__(self) -> None:
        self.upstream_529_total = 0
        self.upstream_5xx_total = 0
        self.upstream_conn_error_total = 0
        self.upstream_429_passthrough_total = 0
        self.retry_succeeded_total = 0
        self.retry_exhausted_total = 0
        self.breaker_open_total = 0

    def snapshot(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class ThrottleState:
    """Process-global circuit breaker coordinating concurrent sessions.

    Overload events (529 + connection drops) accumulate in a sliding
    window. Once ``threshold`` land within ``window_s`` the breaker
    opens for ``open_s`` seconds, during which every ``forward()`` adds
    a coordinated pre-attempt delay so the sessions sharing the one
    proxy pool back off *together* instead of storming.

    Methods are pure (no locking) so tests can drive them on a frozen
    clock; the module-level wrappers below add the lock for the
    forwarder's multithreaded use. All timestamps are
    ``time.monotonic()`` seconds.
    """

    __slots__ = (
        "window_s", "threshold", "open_s", "extra_delay_s",
        "_events", "_open_until", "_last_retry_after_s",
    )

    def __init__(self) -> None:
        self.window_s = _float_env(
            "CLAUDE_HOOKS_PROXY_BREAKER_WINDOW_S", DEFAULT_BREAKER_WINDOW_S,
        )
        self.threshold = _int_env(
            "CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD,
        )
        self.open_s = _float_env(
            "CLAUDE_HOOKS_PROXY_BREAKER_OPEN_S", DEFAULT_BREAKER_OPEN_S,
        )
        self.extra_delay_s = _float_env(
            "CLAUDE_HOOKS_PROXY_BREAKER_EXTRA_DELAY_S",
            DEFAULT_BREAKER_EXTRA_DELAY_S,
        )
        self._events: deque = deque()
        self._open_until: float = 0.0
        self._last_retry_after_s: Optional[float] = None

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def record_overload(self, now: float) -> bool:
        """Record an overload event. Returns True if the breaker is open
        afterwards (newly opened or already-open extended)."""
        self._events.append(now)
        self._prune(now)
        if len(self._events) >= self.threshold:
            self._open_until = max(self._open_until, now + self.open_s)
            return True
        return False

    def record_success(self, now: float) -> None:
        """A success clears accumulated pressure so the breaker stops
        re-opening. The existing ``_open_until`` cooldown still applies
        until it expires (conservative — one success mid-storm shouldn't
        instantly re-allow a flood)."""
        self._events.clear()

    def note_retry_after(self, secs: Optional[float]) -> None:
        if secs is not None and secs > 0:
            self._last_retry_after_s = secs

    def is_open(self, now: float) -> bool:
        return now < self._open_until

    def initial_extra_delay(self, now: float) -> float:
        """Coordinated pre-attempt delay while the breaker is open (0
        when closed). Rises toward the last (already-capped) Retry-After
        upstream told us, but never below the configured floor."""
        if not self.is_open(now):
            return 0.0
        base = self.extra_delay_s
        if self._last_retry_after_s:
            base = max(base, self._last_retry_after_s)
        return base

    def snapshot(self, now: float) -> dict:
        return {
            "open": self.is_open(now),
            "open_remaining_s": max(0.0, round(self._open_until - now, 3)),
            "recent_overloads": len(self._events),
            "last_retry_after_s": self._last_retry_after_s,
        }


# --- Process-wide singletons + locked wrappers --------------------- #

_LOCK = threading.Lock()
_COUNTERS = FlapCounters()
_THROTTLE = ThrottleState()


def counters() -> FlapCounters:
    return _COUNTERS


def throttle() -> ThrottleState:
    return _THROTTLE


def reset_state() -> None:
    """Rebuild both singletons. Test-only — picks up freshly-set env."""
    global _COUNTERS, _THROTTLE
    with _LOCK:
        _COUNTERS = FlapCounters()
        _THROTTLE = ThrottleState()


def breaker_enabled() -> bool:
    """Is the cross-session circuit breaker active?

    OFF by default (``DEFAULT_BREAKER_ENABLED``): with the default
    HTTP/1.1 keepalive pool each session rides its own connection, so a
    coordinated global cooldown penalises healthy sessions for one
    sibling's blip. Set ``CLAUDE_HOOKS_PROXY_BREAKER_ENABLED=1`` to
    re-enable it under the h2 rollback, where the shared multiplexed
    connection makes one fault a signal for every session on it. Read
    per-call so it tracks env restart-free."""
    return _bool_env("CLAUDE_HOOKS_PROXY_BREAKER_ENABLED", DEFAULT_BREAKER_ENABLED)


def pre_attempt_delay(now: float) -> float:
    """Coordinated delay to apply before an attempt (0 when the breaker
    is disabled or closed). Read under the lock so it's consistent with
    concurrent overload records."""
    if not breaker_enabled():
        return 0.0
    with _LOCK:
        return _THROTTLE.initial_extra_delay(now)


def record_overload(now: float, *, status: Optional[int] = None,
                    conn_error: bool = False,
                    retry_after: Optional[float] = None) -> None:
    """Record one failed attempt. 529 and connection drops feed the
    breaker (the throttle signals); other retryable 5xx are counted but
    do *not* open the breaker (they retry with backoff but shouldn't
    trigger the collective cooldown on a one-off)."""
    with _LOCK:
        feeds_breaker = conn_error or status == 529
        if conn_error:
            _COUNTERS.upstream_conn_error_total += 1
        elif status == 529:
            _COUNTERS.upstream_529_total += 1
        elif status is not None:
            _COUNTERS.upstream_5xx_total += 1
        if retry_after is not None:
            _THROTTLE.note_retry_after(retry_after)
        # Counters above always advance (so /health reflects the true
        # upstream weather); the breaker's sliding-window + open logic
        # only runs when the breaker is enabled, so a disabled breaker
        # never opens and ``breaker_open_total`` stays 0.
        if feeds_breaker and breaker_enabled():
            was_open = _THROTTLE.is_open(now)
            opened = _THROTTLE.record_overload(now)
            if opened and not was_open:
                _COUNTERS.breaker_open_total += 1


def record_success(now: float, *, retried: bool) -> None:
    with _LOCK:
        _THROTTLE.record_success(now)
        if retried:
            _COUNTERS.retry_succeeded_total += 1


def record_exhausted() -> None:
    with _LOCK:
        _COUNTERS.retry_exhausted_total += 1


def record_429_passthrough() -> None:
    with _LOCK:
        _COUNTERS.upstream_429_passthrough_total += 1


def snapshot(now: float) -> dict:
    """Combined counters + breaker snapshot for ``/health``."""
    with _LOCK:
        return {
            "upstream_flaps": _COUNTERS.snapshot(),
            "throttle": _THROTTLE.snapshot(now),
        }


warn_retired_env()
