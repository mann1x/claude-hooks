"""Thin client that POSTs to Ollama's OpenAI-compatible
``/v1/chat/completions`` endpoint. Uses httpx for connection pooling
(same as the main API proxy) so the agent loop doesn't open a fresh
TCP connection per tool round-trip.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("claude_hooks.caliber_proxy.ollama")

try:
    import httpx
except ImportError as e:  # pragma: no cover - guarded at install time
    raise ImportError(
        "caliber-grounding-proxy requires httpx. Install with:\n"
        "    pip install 'httpx[http2]>=0.27'"
    ) from e


def default_upstream() -> str:
    # Ollama's OpenAI-compat endpoint. Port 11433 is the user's mapped
    # Ollama host (see memory); 11434 is the upstream default.
    return os.environ.get(
        "CALIBER_GROUNDING_UPSTREAM",
        "http://192.168.178.2:11433/v1",
    )


def default_timeout() -> float:
    try:
        return float(os.environ.get("CALIBER_GROUNDING_HTTP_TIMEOUT", "600"))
    except ValueError:
        return 600.0


_CLIENT: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            timeout=httpx.Timeout(default_timeout(), connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=4, max_connections=8,
            ),
            trust_env=False,
        )
    return _CLIENT


class UpstreamError(RuntimeError):
    """Upstream returned a non-2xx response. Carries the status and
    body so the proxy can relay a faithful error to the client instead
    of masking it as a success with empty choices."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"upstream returned {status}")
        self.status = status
        self.body = body


def chat_completions(payload: dict[str, Any],
                     upstream: Optional[str] = None,
                     ) -> dict[str, Any]:
    """POST ``payload`` to ``<upstream>/chat/completions`` and return the
    parsed JSON. Streaming is left to the caller — we always pass
    ``stream: false`` internally for the agent loop (individual tool
    rounds don't need streaming) and then reconstruct a final
    non-streaming response for the client. Raises ``UpstreamError`` on
    non-2xx responses so callers don't silently produce empty replies.
    """
    url = (upstream or default_upstream()).rstrip("/") + "/chat/completions"
    client = _get_client()
    log.debug("ollama POST %s", url)
    # Optional dump of outgoing payload — set CALIBER_GROUNDING_DUMP_DIR
    # to capture one payload per request for debugging tool-use loops.
    dump_dir = os.environ.get("CALIBER_GROUNDING_DUMP_DIR")
    if dump_dir:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            import time as _time
            fname = os.path.join(
                dump_dir, f"req-{int(_time.time()*1000)}.json",
            )
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass
    resp = client.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = resp.text[:500]
        raise UpstreamError(resp.status_code, body)
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"upstream returned non-JSON ({resp.status_code}): "
            f"{resp.text[:200]}"
        ) from e


def close() -> None:
    """Close the pooled client. Called from server shutdown."""
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None
