"""Ask a running tool to stop.

A council lane runs each tool call in a worker thread and keeps checking
whether the run is wrapping up or cancelled (``execute_tool_calls(stop_check=)``
in ``claude_hooks/agent_loop/runner.py``). On a stop the lane answers the
call and moves on at once; Python cannot kill the worker thread, so it sets
this flag and a tool that walks or loops (grep, glob) checks ``requested()``
and returns early instead of running on in the background. A tool that
never checks simply finishes unobserved.

The flag lives in a ContextVar set inside the worker's own context, so it
reaches only the call it belongs to.
"""
from __future__ import annotations

import contextvars
import threading
from typing import Optional

_STOP: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar(
    "claude_hooks_tool_stop", default=None)


def bind(event: threading.Event) -> None:
    """Attach ``event`` to the current context (the tool's worker)."""
    _STOP.set(event)


def requested() -> bool:
    """True once the caller has given up on this tool call."""
    event = _STOP.get()
    return event is not None and event.is_set()
