"""Deprecated tracer shim — kept as no-op pass-through.

In v1.0 this module wrote a JSONL stream to
``~/.claude/consultants-traces/<sid>.jsonl`` whenever
``CONSULTANTS_TRACE=1`` (or ``--trace``) was set. v1.1 supersedes
that with the per-session SQLite ``transcript.db`` produced by
``consultants/engine/recorder.py`` — same data, structured
queries, opaque to text indexers (the privacy property the user
called out as the reason to pick SQLite).

Rather than rip out every ``Tracer.for_session(...)`` /
``TracedChat`` / ``traced_tool`` / ``traced_node`` call site
(scattered across the runner + graph builder), we keep the symbols
as no-op shims:

- ``Tracer`` always reports disabled and writes nothing.
- ``TracedChat`` is a thin pass-through that still exposes
  ``_client`` so the runner's ChatClient extraction
  (``getattr(c, "_client", c)``) keeps working unchanged.
- ``traced_tool`` and ``traced_node`` simply return the wrapped
  callable unchanged.

The legacy JSONL path is gone in v1.1. ``CONSULTANTS_TRACE`` and
the ``--trace`` / ``--no-trace`` CLI flags are still parsed for
backward compatibility but they emit a deprecation warning the
first time they're observed in a process.

To inspect a session post-hoc, point
``scripts/consultants_trace_summary.py`` at the session sid; it
queries the ``transcript.db`` directly. The waterfall format
matches the v1.0 output so muscle memory carries.

This shim will be removed entirely in v1.2.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("consultants.engine.trace")


_DEPRECATION_LOGGED = False
_DEPRECATION_LOCK = threading.Lock()


def _log_deprecation_once(reason: str) -> None:
    """Emit a one-shot deprecation log per process."""
    global _DEPRECATION_LOGGED
    with _DEPRECATION_LOCK:
        if _DEPRECATION_LOGGED:
            return
        _DEPRECATION_LOGGED = True
    log.warning(
        "consultants tracer is deprecated (%s). v1.1 records the "
        "same data — and more — into "
        "<cwd>/.claude-hooks/consultants/<sid>/transcript.db. "
        "CONSULTANTS_TRACE / --trace / --no-trace are now no-ops "
        "and will be removed in v1.2. Use "
        "scripts/consultants_trace_summary.py <sid> for the "
        "waterfall view.",
        reason,
    )


class Tracer:
    """No-op tracer. Every method does nothing; ``enabled`` is
    permanently ``False``. Kept so existing call sites keep
    importing cleanly until v1.2."""

    def __init__(self, sid: str = "", path: Any = None,
                 enabled: bool = False) -> None:
        self.sid = sid
        self.path = path
        self.enabled = False  # always — see module docstring
        self._role_stack: list[str] = []

    @classmethod
    def for_session(cls, sid: str, *,
                    base_dir: Optional[str] = None,
                    enabled: Optional[bool] = None) -> "Tracer":
        # Nudge the deprecation log when someone explicitly opts in.
        if enabled or _legacy_env_enabled():
            _log_deprecation_once(
                "CONSULTANTS_TRACE / --trace requested but ignored"
            )
        return cls(sid=sid)

    @contextlib.contextmanager
    def span(self, role: str, **extra: Any) -> Iterator[None]:
        # Push/pop role so any caller depending on ``current_role()``
        # mid-span still gets a sane answer (caliber's traced_tool
        # for the v1.0 wrapping pattern). Tests rely on this.
        self._role_stack.append(role)
        try:
            yield
        finally:
            self._role_stack.pop()

    def current_role(self) -> Optional[str]:
        return self._role_stack[-1] if self._role_stack else None

    def record_llm(self, **_kw: Any) -> None:
        return None

    def record_tool(self, **_kw: Any) -> None:
        return None


def _legacy_env_enabled() -> bool:
    return os.environ.get("CONSULTANTS_TRACE", "").strip() in (
        "1", "true", "yes", "on",
    )


# ----------------------- pass-through wrappers ------------------- #
# Kept for source compatibility. The runner wraps each ChatClient in
# TracedChat so it can later extract the raw client via ``_client``
# for warm-handle reuse — see make_runner / make_follow_up_runner.
# The wrapper no longer records anything; it just forwards.

class TracedChat:
    """Thin pass-through. Forwards every attribute, including
    ``.chat(payload)``. The ``_client`` attribute remains so the
    runner can extract the raw ChatClient for live-session retention
    (see ``state._chat_clients`` / ``getattr(c, "_client", c)``)."""

    def __init__(self, client: Any, *, role: str = "",
                 tracer: Optional[Tracer] = None) -> None:
        self._client = client
        self._role = role
        self._tracer = tracer or Tracer()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def chat(self, payload: dict, *args: Any, **kwargs: Any) -> dict:
        return self._client.chat(payload, *args, **kwargs)


def traced_tool(executor: Callable[..., str], *,
                tracer: Optional[Tracer] = None) -> Callable[..., str]:
    """No-op wrapper — returns the executor unchanged. Kept as a
    function so existing callers keep working without an import
    change."""
    return executor


def traced_node(fn: Callable[[dict], dict], *, role: str = "",
                tracer: Optional[Tracer] = None) -> Callable[[dict], dict]:
    """No-op wrapper — returns the node fn unchanged. Same
    rationale as ``traced_tool``."""
    return fn


# Process-startup courtesy: if the legacy env var is set, log the
# deprecation once at import time so operators see it without
# having to fire a consultation first. Cheap (one os.environ lookup
# + one no-op call when unset).
if _legacy_env_enabled():
    _log_deprecation_once(
        "CONSULTANTS_TRACE=1 set in environment but ignored"
    )
