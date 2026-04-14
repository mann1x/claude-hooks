"""
Upstream HTTPS forwarder using ``http.client``.

Stdlib only. Handles:

- streaming response bodies (SSE + chunked), so extended thinking
  completes without buffering
- drop ``Host`` / ``Content-Length`` from inbound headers (we set them
  ourselves based on the upstream URL + body we forward)
- propagate ``x-api-key`` / ``authorization`` / ``anthropic-*`` headers
  verbatim — we never touch auth
- return a tuple so the handler can log metadata + mirror the body
  back to Claude Code
"""

from __future__ import annotations

import http.client
import logging
import ssl
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

log = logging.getLogger("claude_hooks.proxy.forwarder")

# Headers we strip from the inbound request before forwarding.
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


@dataclass
class UpstreamResult:
    status: int
    reason: str
    headers: dict[str, str]
    first_chunk: bytes                    # for metadata extraction
    body_iter: Iterable[bytes]            # the remaining bytes to stream to client
    bytes_read: int = 0                   # populated progressively by body_iter
    stats: dict = field(default_factory=dict)


def forward(
    upstream_url: str,
    method: str,
    path_with_query: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
    ssl_ctx: Optional[ssl.SSLContext] = None,
) -> UpstreamResult:
    """Forward one request upstream and return headers + a streaming body.

    The caller is responsible for consuming ``body_iter`` completely so the
    underlying connection closes cleanly.
    """
    u = urlparse(upstream_url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    if host is None:
        raise ValueError(f"upstream missing host: {upstream_url}")

    if u.scheme == "https":
        ctx = ssl_ctx or ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    out_headers = {
        k: v for k, v in headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    out_headers["Host"] = host
    if body:
        out_headers["Content-Length"] = str(len(body))

    conn.request(method, path_with_query, body=body or None, headers=out_headers)
    resp = conn.getresponse()

    # Read enough to parse metadata (headers + first chunk). For SSE the
    # first 4 KB is plenty for ``message_start``; for JSON responses we
    # still stream the rest lazily so we don't buffer giant bodies.
    first_chunk = resp.read(4096)

    response_headers: dict[str, str] = {}
    for k, v in resp.getheaders():
        if k.lower() in _STRIP_RESPONSE_HEADERS:
            continue
        response_headers[k] = v

    stats = {"bytes_read": len(first_chunk)}

    def _drain() -> Iterable[bytes]:
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                stats["bytes_read"] += len(chunk)
                yield chunk
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return UpstreamResult(
        status=resp.status,
        reason=resp.reason or "",
        headers=response_headers,
        first_chunk=first_chunk,
        body_iter=_drain(),
        stats=stats,
    )
