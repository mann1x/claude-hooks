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
import os
import ssl
import threading
import time
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
    # Strip ``accept-encoding`` so upstream returns uncompressed
    # bytes — ``iter_raw`` can't decode gzip / br in the SseTail and
    # we'd lose all stream metrics (thinking, stop_reason, usage
    # deltas). Over localhost the bandwidth cost is trivial; Claude
    # Code never sees the encoding difference because our response
    # headers match the (uncompressed) body.
    "accept-encoding",
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


# Upstream (Anthropic / other Claude endpoints) can silently drop
# idle HTTP/2 connections well before our pool's keepalive_expiry
# would have retired them, surfacing as ``httpx.RemoteProtocolError``
# ("Server disconnected"). Shorter keepalive + in-forwarder retry
# papers over those drops without asking Claude Code to redo the
# whole request. We also retry a short list of upstream 5xx status
# codes that Anthropic's edge emits on transient overload or
# connection recycling, so the client never sees a spurious 502 the
# way it did when we only retried connection-level exceptions.
_KEEPALIVE_EXPIRY = float(os.environ.get("CLAUDE_HOOKS_PROXY_KEEPALIVE_SEC", "60"))
_UPSTREAM_RETRIES = int(os.environ.get("CLAUDE_HOOKS_PROXY_RETRIES", "10"))
_RETRY_BACKOFF_BASE = float(os.environ.get("CLAUDE_HOOKS_PROXY_RETRY_BACKOFF", "0.15"))
_RETRY_BACKOFF_MAX = float(os.environ.get("CLAUDE_HOOKS_PROXY_RETRY_BACKOFF_MAX", "0.5"))

# Pool-reset triggers for "sticky bad connection" scenarios. When a
# kept-alive HTTP/2 connection is pinned to a degraded upstream edge
# node, every retry on that connection sees the same 5xx — and the
# whole forward() call balloons to 30-120s while sibling connections
# in the pool serve other sessions fine. Two heuristics decide when
# to drop the pool so the next attempt opens a fresh connection
# (which the edge LB usually routes to a different node):
#
#  - SLOW_5XX_RESET_SEC: a retryable-5xx attempt that took at least
#    this long is almost certainly sticky, not just edge overload.
#  - 5XX_RESET_AFTER: this many consecutive retryable 5xx in one
#    forward() call regardless of duration — catches fast-502 floods
#    where the connection is sick but each attempt rejects quickly.
#
# Both default to values that fire only when something is genuinely
# wrong; healthy retries (sub-second blips) keep reusing the pool.
_SLOW_5XX_RESET_SEC = float(
    os.environ.get("CLAUDE_HOOKS_PROXY_SLOW_5XX_RESET_SEC", "5.0")
)
_5XX_RESET_AFTER = int(
    os.environ.get("CLAUDE_HOOKS_PROXY_5XX_RESET_AFTER", "3")
)

# Default 5xx codes we treat as retryable. Excludes 501 (Not Implemented)
# and 505-511 (protocol / semantic errors that won't change on retry).
_DEFAULT_RETRY_STATUS = frozenset({
    500, 502, 503, 504,
    520, 521, 522, 523, 524, 525, 526, 527, 529,
})


def _parse_status_set(raw: Optional[str]) -> frozenset:
    if not raw:
        return _DEFAULT_RETRY_STATUS
    out = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return frozenset(out) if out else _DEFAULT_RETRY_STATUS


_RETRY_ON_STATUS = _parse_status_set(
    os.environ.get("CLAUDE_HOOKS_PROXY_RETRY_STATUS")
)


class _RetryableStatus(Exception):
    """Raised by ``_forward_attempt`` when the upstream returned an HTTP
    status we treat as transient. Carries the buffered response so the
    top-level ``forward`` can either retry or synthesize an
    ``UpstreamResult`` that passes the authentic upstream error
    (headers + body) through to the client if all retries are exhausted.
    """

    def __init__(self, status: int, reason: str, body: bytes,
                 headers: dict) -> None:
        super().__init__(f"upstream returned retryable {status}")
        self.status = status
        self.reason = reason
        self.body = body
        self.headers = headers


def _build_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        http2=True,
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
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

    Transparently retries on ``httpx.RemoteProtocolError`` ("Server
    disconnected") — upstream sometimes drops pooled connections
    before we notice, and the failure is safe to retry as long as no
    bytes have reached our client yet (the retry happens strictly
    *before* ``UpstreamResult`` is returned).
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
    # Pin Accept-Encoding to identity so httpx doesn't re-add a
    # gzip / br offer of its own; SseTail relies on reading the raw
    # SSE bytes and can't decode compressed streams.
    out_headers["Accept-Encoding"] = "identity"
    # httpx sets Host / :authority from URL automatically; no need
    # to pass it explicitly and it can confuse HTTP/2 negotiation.

    client = _get_client(timeout)

    last_exc: Optional[Exception] = None
    consecutive_5xx = 0
    for attempt in range(_UPSTREAM_RETRIES + 1):
        attempt_started = time.monotonic()
        try:
            return _forward_attempt(client, method, url, out_headers, body)
        except (httpx.RemoteProtocolError, httpx.ConnectError,
                _RetryableStatus) as e:
            last_exc = e
            if attempt >= _UPSTREAM_RETRIES:
                break
            # Back off briefly so we don't hammer a server that's
            # actively closing connections or overloaded. Pool naturally
            # drops the dead connection so the retry will open a fresh
            # one.
            wait = min(_RETRY_BACKOFF_BASE * (attempt + 1),
                       _RETRY_BACKOFF_MAX)
            time.sleep(wait)
            # If the failure looks like a sticky-bad-connection symptom,
            # drop the pool so the next attempt opens a fresh upstream
            # connection (different edge LB hop). RemoteProtocolError /
            # ConnectError already imply the connection is dead — only
            # 5xx retries need this nudge.
            elapsed = time.monotonic() - attempt_started
            if isinstance(e, _RetryableStatus):
                consecutive_5xx += 1
                if (elapsed >= _SLOW_5XX_RESET_SEC
                        or consecutive_5xx >= _5XX_RESET_AFTER):
                    log.info(
                        "draining pool after retryable %d "
                        "(elapsed=%.2fs, consecutive=%d): "
                        "next retry will open a fresh upstream connection",
                        e.status, elapsed, consecutive_5xx,
                    )
                    _reset_client()
                    client = _get_client(timeout)
                    consecutive_5xx = 0
            else:
                consecutive_5xx = 0
            log.debug("retry %d after %s: %s",
                      attempt + 1, type(e).__name__, e)

    assert last_exc is not None  # loop only exits via return or break
    # If the last failure was a retryable upstream status, hand the
    # authentic upstream response through to the client rather than
    # masking it with our own ``proxy_error`` 502. Connection-level
    # exceptions still propagate — the caller turns those into 502.
    if isinstance(last_exc, _RetryableStatus):
        return _synthesize_result(last_exc)
    raise last_exc


def _synthesize_result(exc: _RetryableStatus) -> "UpstreamResult":
    """Construct an ``UpstreamResult`` from a buffered retryable-status
    response. Used after retries are exhausted to pass the upstream
    error through verbatim."""
    body = exc.body or b""
    first = body[:4096]
    rest = body[4096:]
    body_iter: Iterable[bytes] = iter([rest]) if rest else iter([])
    return UpstreamResult(
        status=exc.status,
        reason=exc.reason,
        headers=dict(exc.headers),
        first_chunk=first,
        body_iter=body_iter,
        stats={"bytes_read": len(body), "http_version": "HTTP/buffered"},
        sse_tail=None,
    )


def _forward_attempt(
    client: httpx.Client,
    method: str,
    url: str,
    out_headers: dict[str, str],
    body: bytes,
) -> UpstreamResult:
    """One upstream attempt. Raises ``httpx.RemoteProtocolError`` /
    ``httpx.ConnectError`` on connection-level failures, or
    ``_RetryableStatus`` on upstream HTTP 5xx codes in
    ``_RETRY_ON_STATUS``, so ``forward`` can retry. Other exceptions
    propagate unchanged.
    """
    req = client.build_request(
        method, url, headers=out_headers, content=body if body else None,
    )
    resp = client.send(req, stream=True)

    # Retryable upstream error: buffer the (usually small) error body
    # and release the connection before raising so the retry lands on
    # a fresh stream. We keep the original headers + body so the caller
    # can mirror them verbatim if retries are exhausted.
    if resp.status_code in _RETRY_ON_STATUS:
        try:
            body_bytes = resp.read()
        except Exception:
            body_bytes = b""
        kept_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _STRIP_RESPONSE_HEADERS
        }
        status = resp.status_code
        reason = resp.reason_phrase or ""
        try:
            resp.close()
        except Exception:
            pass
        raise _RetryableStatus(
            status=status,
            reason=reason,
            body=body_bytes,
            headers=kept_headers,
        )

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
