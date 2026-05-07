"""Lightweight stdlib tracer for the council pipeline.

Emits one JSONL event per node entry/exit, LLM call, and tool call so
we can build a waterfall after a consultation finishes. Zero deps,
~no overhead per event (one ``json.dumps`` + one ``write()``).

Usage at wiring time (``consultants/server/runner.py``)::

    tracer = Tracer.for_session(sid)
    chat_clients = {r: TracedChat(c, role=r, tracer=tracer)
                    for r, c in chat_clients.items()}
    tool_exec = traced_tool(tool_executor, tracer=tracer)
    # then pass these into GraphDeps as usual

Output file: ``~/.claude/consultants-traces/<sid>.jsonl``. Read via
``scripts/consultants_trace_summary.py`` which prints a per-role
waterfall.

Disabled by default; opt-in via the env var
``CONSULTANTS_TRACE=1`` so production runs pay nothing extra. The
tracer is a no-op when disabled.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("consultants.engine.trace")


_TRACE_DIR_DEFAULT = "~/.claude/consultants-traces"


def _enabled() -> bool:
    return os.environ.get("CONSULTANTS_TRACE", "").strip() in ("1", "true", "yes", "on")


class Tracer:
    """Per-session JSONL tracer. Thread-safe (the LangGraph runtime
    serializes node execution but tool calls inside the researcher's
    sub-loop can interleave LLM + tool spans, so we lock the writer).

    A disabled tracer (``CONSULTANTS_TRACE`` unset) is a no-op for
    every method — callers don't need to gate on enabled-ness.
    """

    def __init__(self, sid: str, path: Path, enabled: bool = True) -> None:
        self.sid = sid
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._role_stack: list[str] = []  # for nested role context

    @classmethod
    def for_session(cls, sid: str, *, base_dir: Optional[str] = None,
                    enabled: Optional[bool] = None) -> "Tracer":
        """Build a tracer for a session.

        ``enabled`` precedence:
          1. Explicit ``enabled=`` argument (per-request override from
             the CLI / consult body).
          2. Env var ``CONSULTANTS_TRACE`` (process-wide default).
          3. Off.

        When disabled, every method on the returned tracer is a no-op
        and no file is created — production runs pay nothing.
        """
        if enabled is None:
            enabled = _enabled()
        base = Path(os.path.expanduser(base_dir or _TRACE_DIR_DEFAULT))
        if enabled:
            base.mkdir(parents=True, exist_ok=True)
        return cls(sid=sid, path=base / f"{sid}.jsonl", enabled=enabled)

    def _write(self, event: dict) -> None:
        if not self.enabled:
            return
        event = {"ts": time.time(), "sid": self.sid, **event}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as e:  # pragma: no cover
                log.warning("trace write failed (sid=%s): %s", self.sid, e)

    @contextlib.contextmanager
    def span(self, role: str, **extra: Any) -> Iterator[None]:
        """Context manager around an entire node execution."""
        if not self.enabled:
            yield
            return
        self._role_stack.append(role)
        t0 = time.monotonic()
        self._write({"event": "node_enter", "role": role, **extra})
        try:
            yield
        finally:
            self._write({
                "event": "node_exit",
                "role": role,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                **extra,
            })
            self._role_stack.pop()

    def current_role(self) -> Optional[str]:
        return self._role_stack[-1] if self._role_stack else None

    def record_llm(self, *, role: str, model: str, duration_ms: int,
                   prompt_tokens: int = 0, completion_tokens: int = 0,
                   iter_index: Optional[int] = None,
                   tool_calls: int = 0,
                   error: Optional[str] = None) -> None:
        self._write({
            "event": "llm_call",
            "role": role,
            "model": model,
            "duration_ms": duration_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "iter_index": iter_index,
            "tool_calls": tool_calls,
            "error": error,
        })

    def record_tool(self, *, role: str, tool: str, duration_ms: int,
                    output_chars: int = 0,
                    error: Optional[str] = None) -> None:
        self._write({
            "event": "tool_call",
            "role": role,
            "tool": tool,
            "duration_ms": duration_ms,
            "output_chars": output_chars,
            "error": error,
        })


# ----------------------- chat client wrapper --------------------- #
# We wrap the raw ``chat_client.chat(payload)`` callable so every LLM
# round-trip emits an ``llm_call`` span. The wrapper preserves the
# duck-typed shape (anything that has a ``.chat(payload)`` method).

class TracedChat:
    """Drop-in wrapper around any ChatClient-like object.

    Forwards every attribute access to the wrapped client; only
    ``.chat(payload)`` is intercepted to add timing + token-count
    extraction. If the wrapped reply doesn't contain a ``usage``
    block (some Ollama responses), token counts default to 0.
    """

    def __init__(self, client: Any, *, role: str, tracer: Tracer) -> None:
        self._client = client
        self._role = role
        self._tracer = tracer
        self._iter_count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def chat(self, payload: dict, *args: Any, **kwargs: Any) -> dict:
        t0 = time.monotonic()
        self._iter_count += 1
        idx = self._iter_count
        model = (payload or {}).get("model", "unknown")
        err: Optional[str] = None
        reply: dict = {}
        try:
            reply = self._client.chat(payload, *args, **kwargs)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            dt_ms = int((time.monotonic() - t0) * 1000)
            usage = (reply or {}).get("usage") or {}
            choices = (reply or {}).get("choices") or []
            tc_count = 0
            if choices:
                msg = (choices[0] or {}).get("message") or {}
                tc_count = len(msg.get("tool_calls") or [])
            self._tracer.record_llm(
                role=self._role,
                model=str(model),
                duration_ms=dt_ms,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                iter_index=idx,
                tool_calls=tc_count,
                error=err,
            )
        return reply


# ----------------------- tool executor wrapper ------------------- #
# The researcher's tool_executor is a plain callable
# ``(name: str, args_str: str, cwd: str) -> output_str``. We wrap it
# so every call emits a ``tool_call`` span. Caliber's executor is the
# real one; tests pass mocks. The wrapper tags every call with the
# role currently active in the tracer's role-stack so the post-hoc
# waterfall can group tool calls under the right role.

def traced_tool(executor: Callable[..., str], *, tracer: Tracer) -> Callable[..., str]:
    def _wrapped(name: str, args_str: str, cwd: str, *args: Any,
                 **kwargs: Any) -> str:
        t0 = time.monotonic()
        err: Optional[str] = None
        out = ""
        try:
            out = executor(name, args_str, cwd, *args, **kwargs)
            return out
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            dt_ms = int((time.monotonic() - t0) * 1000)
            role = tracer.current_role() or "unknown"
            tracer.record_tool(
                role=role,
                tool=name,
                duration_ms=dt_ms,
                output_chars=len(out or ""),
                error=err,
            )
    return _wrapped


# ----------------------- node-fn wrapper ------------------------- #

def traced_node(fn: Callable[[dict], dict], *, role: str,
                tracer: Tracer) -> Callable[[dict], dict]:
    """Wrap a LangGraph node function to emit enter/exit spans."""
    def _wrapped(state: dict) -> dict:
        with tracer.span(role):
            return fn(state)
    return _wrapped
