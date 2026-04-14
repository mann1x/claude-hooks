"""
Upstream forwarder using ``httpx`` with HTTP/2 + connection pooling.

Rationale: Anthropic's edge enforces a per-request-connection gate on
HTTP/1.1-per-request clients. Native Claude Code uses a single
HTTP/2 connection and multiplexes streams over it. We match that
profile with a module-level ``httpx.Client(http2=True)`` so the
proxy presents one well-behaved client to upstream, regardless of
how many requests Claude Code sends through us.

Handles:

- streaming response bodies (SSE + chunked), so extended thinking
  completes without buffering
- strip ``Host`` / ``Content-Length`` from inbound headers — httpx
  sets its own transport-level headers (``:authority`` in h2)
- propagate ``x-api-key`` / ``authorization`` / ``anthropic-*``
  verbatim — we never touch auth
- return a tuple so the handler can log metadata + mirror the body
  back to Claude Code
"""

from __future__ import annotations

import atexit
import logging
import ssl
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

try:
    import httpx
except ImportError as e:  # pragma: no cover - guarded at install time
    raise ImportError(
        "claude-hooks proxy requires httpx[http2]. Install with:\n"
        "    pip install 'httpx[http2]>=0.27'\n"
        "or re-run install.py with proxy.enabled=true to auto-install."
    ) from e

log = logging.getLogger("claude_hooks.proxy.forwarder")

# Headers we strip from the inbound request before forwarding. httpx
# (and HTTP/2) set their own transport-level equivalents.
_STRIP_REQUEST_HEADERS = frozenset({
    "host", "content-length", "connection", "transfer-encoding",
    "keep-alive", "proxy-authorization", "proxy-connection",
    "te", "trailer", "upgrade",
})

# Headers we strip from the upstream response before mirroring to the
# client. Keep content-type + SSE headers; let Python's http.server set
# the transport-level ones.
_STRIP_RESPONSE_HEADERS = frozenset({
    "connection", "transfer-encoding", "keep-alive",
    "proxy-authorization", "te", "trailer", "upgrade",
})

# Module-level pooled client. Lazily constructed on first forward().
# Thread-safe: httpx.Client is documented as safe for concurrent use.
_CLIENT_LOCK = threading.Lock()
_CLIENT: Optional[httpx.Client] = None
_CLIENT_TIMEOUT: Optional[float] = None


def _build_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        http2=True,
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=300.0,
        ),
        follow_redirects=False,
        # Do NOT read HTTPS_PROXY / NO_PROXY from env — we *are* the
        # proxy. If the host has those set pointing at us, trusting
        # env would cause infinite loops.
        trust_env=False,
    )


def _get_client(timeout: float) -> httpx.Client:
    global _CLIENT, _CLIENT_TIMEOUT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = _build_client(timeout)
            _CLIENT_TIMEOUT = timeout
        return _CLIENT


def _reset_client() -> None:
    """Close and drop the pooled client. Test-only / shutdown hook."""
    global _CLIENT, _CLIENT_TIMEOUT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:
                pass
            _CLIENT = None
            _CLIENT_TIMEOUT = None


atexit.register(_reset_client)


@dataclass
class UpstreamResult:
    status: int
    reason: str
    headers: dict[str, str]
    first_chunk: bytes                    # for metadata extraction
    body_iter: Iterable[bytes]            # the remaining bytes to stream to client
    bytes_read: int = 0                   # populated progressively by body_iter
    stats: dict = field(default_factory=dict)
    # SSE tail — populated as chunks flow past. After the stream is
    # fully drained, ``sse_tail.final_usage`` has the canonical
    # usage block (message_delta is the billing truth), and
    # ``sse_tail.stop_reason`` is e.g. 'end_turn' / 'tool_use' /
    # 'max_tokens'. None when the response wasn't SSE.
    sse_tail: "Optional[object]" = None


def forward(
    upstream_url: str,
    method: str,
    path_with_query: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
    ssl_ctx: Optional[ssl.SSLContext] = None,  # retained for API compat; httpx uses certifi
) -> UpstreamResult:
    """Forward one request upstream and return headers + a streaming body.

    The caller is responsible for consuming ``body_iter`` completely so the
    underlying stream is released back to the pool.
    """
    u = urlparse(upstream_url)
    if not u.scheme or not u.hostname:
        raise ValueError(f"upstream missing host: {upstream_url}")
    if u.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {u.scheme}")

    url = f"{u.scheme}://{u.netloc}{path_with_query}"

    out_headers = {
        k: v for k, v in headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    # httpx sets Host / :authority from URL automatically; no need
    # to pass it explicitly and it can confuse HTTP/2 negotiation.

    client = _get_client(timeout)

    req = client.build_request(
        method, url, headers=out_headers, content=body if body else None,
    )
    resp = client.send(req, stream=True)

    chunks_iter = resp.iter_raw(chunk_size=65536)

    # Pull up to 4 KB for metadata extraction. SSE's ``message_start``
    # fits well under that; JSON bodies are still streamed lazily.
    first_chunk = b""
    try:
        while len(first_chunk) < 4096:
            try:
                chunk = next(chunks_iter)
            except StopIteration:
                break
            if not chunk:
                continue
            first_chunk += chunk
    except Exception:
        try:
            resp.close()
        except Exception:
            pass
        raise

    response_headers: dict[str, str] = {}
    for k, v in resp.headers.items():
        if k.lower() in _STRIP_RESPONSE_HEADERS:
            continue
        response_headers[k] = v

    stats = {"bytes_read": len(first_chunk), "http_version": resp.http_version}

    # SSE responses stream the final ``usage`` block in a trailing
    # ``message_delta``. We attach a tailer that parses events as they
    # flow past. Bytes going to the client are verbatim.
    from claude_hooks.proxy.sse import SseTail
    # httpx.Headers.get is case-insensitive; response_headers dict may
    # have been rekeyed to lowercase (HTTP/2 normalizes).
    content_type = (resp.headers.get("content-type") or "").lower()
    is_sse = "text/event-stream" in content_type
    tail: Optional[SseTail] = SseTail() if is_sse else None

    if tail is not None and first_chunk:
        tail._feed(first_chunk)

    def _drain() -> Iterable[bytes]:
        try:
            for chunk in chunks_iter:
                if not chunk:
                    continue
                stats["bytes_read"] += len(chunk)
                if tail is not None:
                    tail._feed(chunk)
                yield chunk
        finally:
            if tail is not None and tail._buffer:
                tail._parse_event(tail._buffer)
                tail._buffer = b""
            try:
                resp.close()
            except Exception:
                pass

    return UpstreamResult(
        status=resp.status_code,
        reason=resp.reason_phrase or "",
        headers=response_headers,
        first_chunk=first_chunk,
        body_iter=_drain(),
        stats=stats,
        sse_tail=tail,
    )
