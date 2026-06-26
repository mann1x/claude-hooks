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
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

from claude_hooks.proxy import retry

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


# Upstream (Anthropic / other Claude endpoints) can silently drop idle
# HTTP/2 connections well before our pool's keepalive_expiry would have
# retired them, surfacing as ``httpx.RemoteProtocolError`` ("Server
# disconnected"). A short keepalive retires stale connections ahead of
# upstream's silent idle-drop; httpx evicts a connection that *raised*
# on its own, so a retry on the shared client transparently lands on a
# fresh connection without disturbing sibling sessions.
#
# The retry *policy* (jittered backoff, Retry-After honoring, the
# wall-clock deadline, the cross-session circuit breaker) lives in
# ``claude_hooks.proxy.retry`` — a pure, unit-testable module. This
# file owns only the HTTP loop + ``time.sleep``. The old whole-pool
# ``_reset_client()`` nuke on the retry path is gone: it closed sibling
# sessions' live connections (collective storm) *and* recreated the
# per-request fresh-connection profile the HTTP/2 pool exists to avoid
# (which itself tripped Anthropic's edge-429 gate). See docs/proxy.md
# "Retry / throttle resilience".
_KEEPALIVE_EXPIRY = float(os.environ.get("CLAUDE_HOOKS_PROXY_KEEPALIVE_SEC", "60"))

# TCP keepalive on the UPSTREAM socket. Claude Code's native client sets
# SO_KEEPALIVE (~60s idle); without it our connection sits truly idle
# during long server-side "thinking" windows (xhigh effort / large
# context) and an on-path stateful device reaps it at ~60s, surfacing as
# RemoteProtocolError "Server disconnected". ``_KEEPALIVE_EXPIRY`` above
# only retires POOL-idle connections between requests — it cannot keep an
# in-flight, mid-request-idle connection alive. Kernel keepalive probes
# do. KEEPIDLE is deliberately < the observed ~60s reap window.
_KEEPALIVE_IDLE = int(os.environ.get("CLAUDE_HOOKS_PROXY_TCP_KEEPIDLE", "30"))
_KEEPALIVE_INTVL = int(os.environ.get("CLAUDE_HOOKS_PROXY_TCP_KEEPINTVL", "15"))
_KEEPALIVE_CNT = int(os.environ.get("CLAUDE_HOOKS_PROXY_TCP_KEEPCNT", "4"))


def _keepalive_socket_options() -> list:
    """SO_KEEPALIVE (+ Linux idle/intvl/cnt tuning when available) so the
    upstream connection survives long silent thinking windows the way
    Claude Code's native client does. SO_KEEPALIVE is portable; the
    TCP_KEEP* knobs are Linux-only and guarded by ``hasattr``."""
    opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _KEEPALIVE_IDLE))
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPALIVE_INTVL))
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPALIVE_CNT))
    return opts


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
    # Build the transport explicitly so we can pass socket_options (TCP
    # keepalive). http2 + limits move onto the transport — they are
    # ignored on httpx.Client when a custom transport is supplied.
    transport = httpx.HTTPTransport(
        http2=True,
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        ),
        socket_options=_keepalive_socket_options(),
        # Do NOT read HTTPS_PROXY / NO_PROXY from env — we *are* the
        # proxy. If the host has those set pointing at us, trusting
        # env would cause infinite loops.
        trust_env=False,
        retries=0,
    )
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
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

    Transparently retries (spaced, deadline-bounded — see
    ``claude_hooks.proxy.retry``) on transport-level failures
    (``httpx.TimeoutException`` / ``httpx.NetworkError`` /
    ``httpx.RemoteProtocolError``) and on retryable upstream statuses.
    All are safe to retry because no byte has reached our client yet —
    the retry happens strictly *before* ``UpstreamResult`` is returned.
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

    cfg = retry.ApiProxyRetryConfig()
    client = _get_client(timeout)

    last_exc: Optional[Exception] = None
    attempt = 0                       # 0-indexed; 0 = first attempt
    backoff_total = 0.0               # cumulative sleep, seconds
    retry_after_honored = False
    start = time.monotonic()

    # Cross-session circuit breaker: when upstream is in an overload
    # window, every session sharing this one proxy pool adds a
    # coordinated delay *before its first attempt* so a second session
    # can't pile on and turn a transient overload into a collective
    # storm. 0 when the breaker is closed.
    pre = retry.pre_attempt_delay(start)
    if pre > 0:
        time.sleep(pre)
        backoff_total += pre

    while True:
        try:
            result = _forward_attempt(
                client, method, url, out_headers, body,
                retry_status=cfg.retry_status,
            )
            now = time.monotonic()
            # 429 is the client's own quota, not an overload — it passes
            # straight through (never retried). Count it for visibility.
            if result.status == 429:
                retry.record_429_passthrough()
            retry.record_success(now, retried=attempt > 0)
            _stamp_retry_stats(
                result.stats, attempt, backoff_total,
                retry_after_honored, retry.throttle().is_open(now), "ok",
            )
            return result
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as e:
            # Transport-level failure — the throttle's other faces:
            # connect/read/write/pool *timeouts* (``TimeoutException``),
            # connect/read/write *errors* (``NetworkError``, incl.
            # ``ConnectError``), and mid-stream server disconnects
            # (``RemoteProtocolError``). All are safe to retry here
            # because no byte has reached the client yet. httpx has
            # already evicted the dead connection, so the next attempt
            # on the shared client gets a fresh one — no whole-pool nuke
            # (which would kill sibling sessions and re-trip the edge-429
            # gate). hdrs is empty; there's no upstream response to read
            # Retry-After from.
            last_exc = e
            now = time.monotonic()
            hdrs: dict = {}
            ra: Optional[float] = None
            retry.record_overload(now, conn_error=True)
        except _RetryableStatus as e:
            last_exc = e
            now = time.monotonic()
            hdrs = e.headers
            ra = (retry.parse_retry_after(hdrs, time.time(),
                                          cfg.retry_after_cap_s)
                  if cfg.honor_retry_after else None)
            retry.record_overload(now, status=e.status, retry_after=ra)

        elapsed = now - start
        wait = retry.next_delay(attempt, hdrs, cfg, time.time())
        if ra is not None:
            retry_after_honored = True
        # While the breaker is open, use its coordinated delay as a floor.
        floor = retry.pre_attempt_delay(now)
        if floor > wait:
            wait = floor
        if not retry.should_retry(attempt, elapsed, wait, cfg):
            break
        log.debug(
            "proxy retry %d after %s (wait %.2fs, elapsed %.1fs)",
            attempt + 1, type(last_exc).__name__, wait, elapsed,
        )
        time.sleep(wait)
        backoff_total += wait
        attempt += 1

    assert last_exc is not None  # loop only exits via return or break
    retry.record_exhausted()
    # If the last failure was a retryable upstream status, hand the
    # authentic upstream response through to the client rather than
    # masking it with our own ``proxy_error`` 502. Connection-level
    # exceptions still propagate — the caller turns those into 502.
    if isinstance(last_exc, _RetryableStatus):
        result = _synthesize_result(last_exc)
        _stamp_retry_stats(
            result.stats, attempt, backoff_total,
            retry_after_honored, False, "exhausted",
        )
        return result
    # Transport-error exhaustion → the caller turns this into a 502.
    # Carry the retry telemetry ON the exception so the 502 log line can
    # record it: the re-raise path has no ``UpstreamResult`` to stamp, so
    # without this an exhausted-after-N-retries 502 is indistinguishable
    # in the JSONL from a never-retried one (both ``retry_count`` null).
    try:
        last_exc._proxy_retry_stats = {  # type: ignore[attr-defined]
            "retry_count": attempt,
            "backoff_total_ms": int(backoff_total * 1000),
            "retry_outcome": "exhausted",
        }
    except Exception:
        pass
    raise last_exc


def _stamp_retry_stats(stats: dict, retries: int, backoff_total_s: float,
                       retry_after_honored: bool, breaker_open: bool,
                       outcome: str) -> None:
    """Record per-request retry telemetry onto the ``UpstreamResult``
    stats dict — but only when something noteworthy happened, so the
    common zero-retry success path keeps the JSONL line slim (the
    server omits null keys)."""
    if (retries or retry_after_honored or breaker_open
            or outcome != "ok"):
        stats["retry_count"] = retries
        stats["backoff_total_ms"] = int(backoff_total_s * 1000)
        stats["retry_after_honored"] = retry_after_honored
        stats["breaker_open"] = breaker_open
        stats["retry_outcome"] = outcome


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
    retry_status: frozenset = retry.DEFAULT_RETRY_STATUS,
) -> UpstreamResult:
    """One upstream attempt. Raises ``httpx.RemoteProtocolError`` /
    ``httpx.ConnectError`` on connection-level failures, or
    ``_RetryableStatus`` on upstream HTTP codes in ``retry_status``, so
    ``forward`` can retry. Other exceptions propagate unchanged.
    """
    req = client.build_request(
        method, url, headers=out_headers, content=body if body else None,
    )
    resp = client.send(req, stream=True)

    # Retryable upstream error: buffer the (usually small) error body
    # and release the connection before raising so the retry lands on
    # a fresh stream. We keep the original headers + body so the caller
    # can mirror them verbatim if retries are exhausted.
    if resp.status_code in retry_status:
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

    # Pull the FIRST non-empty chunk for metadata extraction and return
    # immediately. We deliberately do NOT block accumulating a fixed 4 KB:
    # during a long "thinking" window upstream may emit a small
    # ``message_start`` then go quiet (only periodic pings), and looping
    # for more bytes would withhold the response headers + first byte from
    # Claude Code for tens of seconds — tripping its client-side body
    # timeout (the SSE-TTFB failure class). httpx ``iter_raw`` yields each
    # network read as it lands, so one ``next`` is the earliest byte;
    # ``message_start`` fits in it, and richer/final metadata still flows
    # via the SseTail attached to the body below.
    first_chunk = b""
    try:
        while True:
            try:
                chunk = next(chunks_iter)
            except StopIteration:
                break
            if not chunk:
                continue
            first_chunk = chunk
            break
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
