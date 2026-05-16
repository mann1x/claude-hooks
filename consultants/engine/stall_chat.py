"""Adapter that turns a streaming chat method + stall thresholds
into a synchronous ``chat(payload) -> dict`` callable the agent
loop runner can use without modification.

The agent loop calls ``chat_fn(payload)`` and expects a dict back.
The stall infrastructure (``consultants.engine.stall``) needs
token-level visibility to detect mid-stream stalls. This module
bridges the two: ``make_stall_protected_chat_fn`` wraps a
``chat_streamed`` method so each invocation runs inside a
``StallMonitor`` with a fresh ``StallController``.

Two flavors:

- :func:`make_stall_protected_chat_fn` — the full streaming path.
  Requires a chat client with a ``chat_streamed(payload, on_token,
  cancel_check)`` method. Used for ``ChatClient`` (Ollama-native).

- :func:`make_hard_cap_only_chat_fn` — the fallback for chat
  clients without streaming support (e.g. older
  ``LlamafileAgentChatClient`` revs that only expose ``chat()``).
  Wraps the sync call in a worker thread + hard-cap timer. No
  stall detection (no token visibility) — only the absolute
  ceiling. Local llamafile rarely stalls, so this is acceptable
  for now; M3c can swap it for real streaming when llamafile gets
  it.

Both factories return callables with the same signature
``(payload: dict) -> dict`` so the researcher node uses them
interchangeably.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from consultants.engine.stall import (
    CancelledByOrchestrator,
    HardCapExceeded,
    StallConfig,
    StallController,
    StallMonitor,
    StallOutcome,
    StallRetryExhausted,
)

log = logging.getLogger("consultants.engine.stall_chat")


ChatFn = Callable[[dict], dict]


def make_stall_protected_chat_fn(
    chat_streamed: Callable[..., dict],
    *,
    stall_threshold_s: float,
    hard_cap_s: float,
    retries: int = 1,
    check_interval_s: float = 5.0,
    retry_backoff_s: float = 2.0,
    on_event: Optional[Callable[[dict], None]] = None,
) -> ChatFn:
    """Build a ``chat(payload) -> dict`` callable that routes each
    invocation through a :class:`StallMonitor`.

    ``chat_streamed`` is a bound method (typically
    ``ChatClient.chat_streamed``) whose signature is
    ``(payload, *, on_token, cancel_check) -> dict``. The factory
    adapts it into a stall-monitored ``ChatStreamedFn`` the
    orchestrator can drive.

    Thresholds match the names on ``RuntimeControl`` so callers
    can do::

        chat_fn = make_stall_protected_chat_fn(
            client.chat_streamed,
            stall_threshold_s=runtime_get(
                state, "stall_threshold_s", default=300.0),
            hard_cap_s=runtime_get(
                state, "per_lane_hard_s", default=3600.0),
            retries=runtime_get(
                state, "stall_retries", default=1),
        )

    Errors:
    - Upstream errors from the chat call propagate unchanged
      (caller decides retry semantics).
    - ``StallRetryExhausted`` and ``HardCapExceeded`` are re-raised
      so the researcher's existing try/except can tombstone the
      lane.
    """
    cfg = StallConfig(
        stall_threshold_s=stall_threshold_s,
        hard_cap_s=hard_cap_s,
        retries=retries,
        check_interval_s=check_interval_s,
        retry_backoff_s=retry_backoff_s,
    )

    def chat_streamed_fn(payload: dict,
                         controller: StallController) -> dict:
        # Each call gets its own controller. We pass
        # ``controller.mark_token`` and ``controller.is_cancelled``
        # so the streaming reader updates the watchdog as tokens
        # arrive.
        def on_token(_delta: str) -> None:
            controller.mark_token()

        return chat_streamed(
            payload,
            on_token=on_token,
            cancel_check=controller.is_cancelled,
        )

    def chat_fn(payload: dict) -> dict:
        monitor = StallMonitor(cfg, on_event=on_event)
        return monitor.run(chat_streamed_fn, payload)

    return chat_fn


def make_hard_cap_only_chat_fn(
    chat: ChatFn,
    *,
    hard_cap_s: float,
    on_event: Optional[Callable[[dict], None]] = None,
) -> ChatFn:
    """Wrap a non-streaming ``chat(payload) -> dict`` callable in a
    worker thread + a hard-cap timer.

    No stall detection (no token visibility) — only the absolute
    wall-clock ceiling. If the inner call exceeds ``hard_cap_s``,
    raises :class:`HardCapExceeded` and abandons the worker thread
    (it dies with the process; daemon thread).

    Used for clients that lack a ``chat_streamed`` method. The
    researcher's fallback path; not the primary mechanism.
    """

    def chat_fn(payload: dict) -> dict:
        result_box: dict[str, Any] = {"result": None}
        error_box: dict[str, Optional[BaseException]] = {"error": None}

        def worker():
            try:
                result_box["result"] = chat(payload)
            except BaseException as e:  # noqa: BLE001
                error_box["error"] = e

        t0 = time.monotonic()
        t = threading.Thread(target=worker, daemon=True,
                             name="hard_cap.worker")
        t.start()
        t.join(timeout=hard_cap_s)
        elapsed = time.monotonic() - t0
        if t.is_alive():
            # Hard cap blown. Worker is leaked (daemon=True) so it
            # dies with the process.
            if on_event is not None:
                try:
                    on_event({
                        "kind": "stall.attempt.hard_cap",
                        "attempt": 1,
                        "elapsed_s": elapsed,
                        "tokens": 0,
                    })
                except Exception:  # pragma: no cover
                    log.exception("on_event raised; ignored")
            raise HardCapExceeded(
                f"chat() exceeded hard cap of {hard_cap_s}s "
                f"({elapsed:.1f}s elapsed)",
                outcome=StallOutcome.HARD_CAP_EXCEEDED,
                attempts=1,
                tokens_emitted=0,
                elapsed_s=elapsed,
            )
        if error_box["error"] is not None:
            raise error_box["error"]
        if on_event is not None:
            try:
                on_event({
                    "kind": "stall.attempt.ok",
                    "attempt": 1,
                    "elapsed_s": elapsed,
                    "tokens": 0,
                })
            except Exception:  # pragma: no cover
                log.exception("on_event raised; ignored")
        return result_box["result"]   # type: ignore[return-value]

    return chat_fn


def stall_protected_chat_fn_for(
    chat_client: Any,
    *,
    stall_threshold_s: float,
    hard_cap_s: float,
    retries: int = 1,
    check_interval_s: float = 5.0,
    retry_backoff_s: float = 2.0,
    on_event: Optional[Callable[[dict], None]] = None,
) -> ChatFn:
    """Pick the right protector for ``chat_client``.

    - If the client exposes ``chat_streamed(...)``, use full stall
      detection.
    - If only ``chat(...)``, fall back to hard-cap-only protection.
    - If neither, return ``chat_client.chat`` unchanged (gracefully
      degrades for test stubs / legacy clients) — the only
      observable behavior change is no protection at all, which
      mirrors v1 behavior.

    The factory takes the live thresholds from the caller (typically
    derived from ``RuntimeControl`` at researcher-node entry), so a
    mid-flight mutation re-builds the protector on the next round.
    """
    if chat_client is None or not hasattr(chat_client, "chat"):
        raise TypeError(
            "stall_protected_chat_fn_for: chat_client must expose .chat",
        )

    if hasattr(chat_client, "chat_streamed"):
        return make_stall_protected_chat_fn(
            chat_client.chat_streamed,
            stall_threshold_s=stall_threshold_s,
            hard_cap_s=hard_cap_s,
            retries=retries,
            check_interval_s=check_interval_s,
            retry_backoff_s=retry_backoff_s,
            on_event=on_event,
        )
    # Streaming not available — hard-cap-only fallback.
    log.info(
        "stall_protected_chat_fn_for: %s has no chat_streamed; "
        "using hard-cap-only protection",
        type(chat_client).__name__,
    )
    return make_hard_cap_only_chat_fn(
        chat_client.chat,
        hard_cap_s=hard_cap_s,
        on_event=on_event,
    )


__all__ = [
    "ChatFn",
    "make_stall_protected_chat_fn",
    "make_hard_cap_only_chat_fn",
    "stall_protected_chat_fn_for",
    # Re-export so callers don't have to dual-import.
    "CancelledByOrchestrator",
    "HardCapExceeded",
    "StallRetryExhausted",
]
