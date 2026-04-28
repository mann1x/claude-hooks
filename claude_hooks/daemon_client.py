"""
claude-hooks-daemon client (Tier 3.8 latency reduction).

Thin counterpart to ``claude_hooks.daemon``. Used by the bin/claude-hook
shim (and by the run.py fallback path) to send a hook event to a running
daemon instead of spinning up a fresh interpreter.

Failure mode is the whole point. Any of these conditions cause
:func:`call` to return ``None``:

- daemon not running on the configured port
- secret file missing (daemon not initialised yet)
- TCP connect refused, timeout, or any read error
- protocol error or HMAC rejection from the daemon

The caller is expected to check for ``None`` and fall back to in-process
dispatch — that's the "daemon-or-fallback" semantic that lets users
install claude-hooks without a daemon at all and still keep things
working. It also means a crashed daemon is self-healing: the next hook
invocation just runs in-process and the user can `systemctl --user
restart claude-hooks-daemon` (or equivalent) at leisure.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Optional

from claude_hooks.daemon import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SECRET_PATH,
    PROTOCOL_VERSION,
    sign_request,
)

log = logging.getLogger("claude_hooks.daemon_client")


_REQUEST_ID_COUNTER = 0


def _next_id() -> int:
    global _REQUEST_ID_COUNTER
    _REQUEST_ID_COUNTER += 1
    return _REQUEST_ID_COUNTER


def _read_secret(secret_path: Path) -> Optional[str]:
    """Read the daemon's HMAC secret. Returns None on missing/unreadable."""
    try:
        return secret_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def call(
    event: str,
    payload: dict,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    secret_path: Path = DEFAULT_SECRET_PATH,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Send one request to the daemon and return the parsed response, or
    None if the daemon was unreachable / returned an error.

    The returned dict shape is::

        {"ok": True,  "result": <handler-output-or-None>}
        {"ok": False, "error": "...", "code": 401}

    Callers can distinguish "daemon unavailable, run inline" (None)
    from "daemon answered but rejected the request" (dict with ok=False).
    """
    secret = _read_secret(Path(secret_path))
    if secret is None:
        log.debug("daemon secret not present at %s — fallback", secret_path)
        return None

    rid = _next_id()
    ts = time.time()
    try:
        payload_json = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        log.debug("payload not serialisable: %s", e)
        return None

    sig = sign_request(
        request_id=rid, ts=ts, event=event,
        payload_json=payload_json, secret=secret,
    )
    request = {
        "id": rid, "ts": int(ts), "event": event,
        "payload": payload or {}, "sig": sig,
    }
    body = (json.dumps(request) + "\n").encode("utf-8")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(body)
            # Tell the daemon we won't write any more — it expects exactly
            # one line per connection.
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks: list[bytes] = []
            while True:
                try:
                    buf = sock.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not buf:
                    break
                chunks.append(buf)
            raw = b"".join(chunks)
    except (ConnectionRefusedError, socket.timeout, TimeoutError, OSError) as e:
        log.debug("daemon unreachable on %s:%d: %s", host, port, e)
        return None

    if not raw:
        return None

    try:
        # Server returns a single line; accept extra trailing whitespace.
        line = raw.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log.debug("invalid response from daemon: %s", e)
        return None


def ping(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    secret_path: Path = DEFAULT_SECRET_PATH,
    timeout: float = 2.0,
) -> bool:
    """Return True iff the daemon is alive and authenticates this client."""
    resp = call(
        "_ping", {},
        host=host, port=port, secret_path=secret_path, timeout=timeout,
    )
    if not resp or not resp.get("ok"):
        return False
    result = resp.get("result") or {}
    return result.get("protocol") == PROTOCOL_VERSION
