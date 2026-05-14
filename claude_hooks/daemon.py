"""
claude-hooks-daemon — long-lived hook executor (Tier 3.8 latency reduction).

When Claude Code fires a hook, the bin/claude-hook shim spawns a fresh Python
interpreter, imports the package, builds providers from config, and runs the
handler. Every step pays its own cost:

- ~50-200 ms Python interpreter startup (varies by host + venv shape)
- ~20-50 ms ``import claude_hooks.*``
- ~20 ms re-loading the JSON config + setting up logging
- ~50 ms instantiating providers
- per-MCP-call: a fresh socket / TCP handshake — no connection reuse

A long-running daemon owns all of this state once and answers requests in
milliseconds. Cross-platform IPC is TCP localhost (127.0.0.1) authenticated
with HMAC-signed requests — works identically on Linux, macOS, and Windows
without depending on Unix domain sockets or named pipes. Each request is one
line of JSON, each response is one line of JSON.

Wire protocol::

    REQUEST line:  {"id":N, "ts":<epoch>, "event":"Stop", "payload":{...}, "sig":"<hex>"}
    RESPONSE line: {"id":N, "ok":true, "result":{...}}            on success
                   {"id":N, "ok":false, "error":"...", "code":N}  on error

The HMAC signature covers ``id|ts|event|sha256(payload_json)`` so payloads
can be large without inflating the signature input. ``ts`` is checked
against a configurable replay window (default 60 s) to bound forgery cost
on a leaked secret. The shared secret lives in
``~/.claude/claude-hooks-daemon-secret`` (mode 0600), and the daemon
refuses to start if that file is missing or world-readable.

The daemon binds to 127.0.0.1 only — never to an external interface. Any
attempt to bind to 0.0.0.0 or another address is ignored at config load.

Design notes
------------

- **Stateless dispatch surface**. The daemon delegates each request to the
  same ``claude_hooks.dispatcher.dispatch`` that the inline run path uses,
  so behavioural changes land in both modes for free.

- **Connection model**: one connection per request (no keep-alive). Hooks
  fire infrequently enough (~one every few seconds at most) that the
  per-connection cost is negligible compared with the interpreter-startup
  it replaces. Keep-alive would complicate auth / bounded resources without
  meaningful win.

- **Threaded server**: each request is handled in its own short-lived
  thread so concurrent UserPromptSubmit + Stop hooks don't queue. Provider
  instances themselves are not thread-safe in general; we instantiate
  fresh providers per request the same way ``dispatch`` does today, which
  keeps thread-safety concerns inside the providers' own scope.

- **Failure mode**: daemon-side errors return ``{"ok": false, ...}``. The
  daemon never crashes its server loop on a bad request — bad framing,
  invalid HMAC, replay-too-old, missing handler are all individual
  rejections. ``Ctrl-C`` / SIGTERM trigger a clean shutdown.

This module is the server. ``claude_hooks.daemon_client`` is the
counterpart consumed by the ``bin/claude-hook`` shim.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.daemon")


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47018
DEFAULT_SECRET_PATH = Path.home() / ".claude" / "claude-hooks-daemon-secret"
DEFAULT_REPLAY_WINDOW_SECONDS = 60
PROTOCOL_VERSION = 1


# ===================================================================== #
# Secret management
# ===================================================================== #
def ensure_secret(path: Path = DEFAULT_SECRET_PATH) -> str:
    """Read or create the daemon's shared HMAC secret.

    On first call, generates a 32-byte random secret and writes it with
    mode 0600. On subsequent calls, reads it back and validates the
    permission bits — refuses to return a world-readable secret on
    POSIX hosts (Windows ACLs don't map cleanly so we skip the check
    there). Returns the secret as a hex string.
    """
    path = Path(path)
    if path.exists():
        if os.name == "posix":
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise RuntimeError(
                    f"daemon secret {path} is world/group-readable (mode "
                    f"{oct(mode)}); chmod 600 it before continuing"
                )
        return path.read_text(encoding="utf-8").strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    # Write atomically with restrictive perms.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(secret)
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    log.info("generated new daemon secret at %s", path)
    return secret


# ===================================================================== #
# HMAC helpers — used by both server and client
# ===================================================================== #
def sign_request(
    *, request_id: int, ts: float, event: str, payload_json: str, secret: str,
) -> str:
    """Return the hex HMAC signature for a request.

    Signed input is ``id|ts|event|sha256(payload_json)``. The payload
    body itself isn't signed directly so that callers can stream large
    payloads without buffering the whole thing into the HMAC.
    """
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    msg = f"{request_id}|{int(ts)}|{event}|{payload_hash}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_request(
    *, request_id: int, ts: float, event: str, payload_json: str,
    signature: str, secret: str,
    replay_window: int = DEFAULT_REPLAY_WINDOW_SECONDS,
    now: Optional[float] = None,
) -> Optional[str]:
    """Validate a request signature + freshness. Returns None on success
    or a short error string on failure (suitable for logging / error
    response). Uses ``hmac.compare_digest`` to avoid timing leaks."""
    expected = sign_request(
        request_id=request_id, ts=ts, event=event,
        payload_json=payload_json, secret=secret,
    )
    if not hmac.compare_digest(expected, signature):
        return "invalid signature"
    now_ts = now if now is not None else time.time()
    if abs(now_ts - ts) > replay_window:
        return f"replay window exceeded ({abs(now_ts - ts):.0f}s)"
    return None


# ===================================================================== #
# Server
# ===================================================================== #
class _RequestHandler(socketserver.StreamRequestHandler):
    """One connection = one request line. Reply with one response line."""

    # The DaemonServer instance sets these on itself; access via self.server.
    def handle(self) -> None:
        try:
            line = self.rfile.readline()
            if not line:
                return
            self._dispatch_line(line)
        except (ConnectionError, OSError) as e:
            log.debug("connection error from %s: %s", self.client_address, e)
        except Exception as e:  # pragma: no cover — defensive
            log.exception("unexpected handler error: %s", e)

    def _dispatch_line(self, line: bytes) -> None:
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._reply_error(0, f"bad JSON: {e}", code=400)
            return

        if not isinstance(req, dict):
            self._reply_error(0, "request must be a JSON object", code=400)
            return

        rid = req.get("id")
        if not isinstance(rid, int):
            self._reply_error(0, "missing/invalid id", code=400)
            return

        ts = req.get("ts")
        if not isinstance(ts, (int, float)):
            self._reply_error(rid, "missing/invalid ts", code=400)
            return

        event = req.get("event")
        if not isinstance(event, str) or not event:
            self._reply_error(rid, "missing/invalid event", code=400)
            return

        sig = req.get("sig")
        if not isinstance(sig, str):
            self._reply_error(rid, "missing/invalid sig", code=400)
            return

        payload = req.get("payload")
        if payload is None:
            payload = {}
        try:
            payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            self._reply_error(rid, f"non-serialisable payload: {e}", code=400)
            return

        # Auth first — every other check is informational.
        secret = self.server.secret  # type: ignore[attr-defined]
        replay = self.server.replay_window  # type: ignore[attr-defined]
        err = verify_request(
            request_id=rid, ts=ts, event=event,
            payload_json=payload_json, signature=sig, secret=secret,
            replay_window=replay,
        )
        if err is not None:
            self._reply_error(rid, err, code=401)
            return

        # Dispatch.
        try:
            result = self._run_handler(event, payload)
        except Exception as e:  # pragma: no cover — runtime safety net
            log.exception("handler %s crashed: %s", event, e)
            self._reply_error(rid, f"handler crashed: {e}", code=500)
            return

        self._reply_ok(rid, result)

    def _run_handler(self, event: str, payload: dict) -> Optional[dict]:
        """Invoke the dispatcher and return the handler output dict.

        Uses :func:`dispatcher.dispatch_capture` rather than redirecting
        ``sys.stdout`` because the daemon is multi-threaded
        (``ThreadingTCPServer``); the previous implementation mutated
        process-global ``sys.stdout`` and let two concurrent requests
        cross-write into each other's buffers (the symptom was a Stop
        hook receiving a UserPromptSubmit recall payload — see #199).
        """
        # Special protocol-only ops first.
        if event == "_ping":
            return {"protocol": PROTOCOL_VERSION, "pid": os.getpid()}
        if event == "_shutdown":
            self.server.request_shutdown()  # type: ignore[attr-defined]
            return {"shutdown": True}

        # Embedding-server lifecycle ops (v1.4). The EmbeddingManager
        # is only attached to the server when cfg["embedding"]["enabled"]
        # is true; absent → return a clean ``available=False`` answer so
        # the client can fall through to its primary embedder or surface
        # a config-error.
        if event in ("_embedding_ensure", "_embedding_status",
                     "_embedding_shutdown"):
            mgr = getattr(self.server, "embedding_manager", None)
            if mgr is None:
                return {"available": False,
                        "reason": "embedding manager not configured"}
            try:
                if event == "_embedding_ensure":
                    return mgr.ensure_running()
                if event == "_embedding_status":
                    return mgr.status()
                if event == "_embedding_shutdown":
                    mgr.shutdown()
                    return {"shutdown": True}
            except RuntimeError as e:
                # Spawn / port-busy / health-fail surface here. Return
                # the structured error rather than crashing the daemon
                # thread — the CompositeEmbedder uses this to fail over.
                return {"available": False, "reason": str(e)}

        from claude_hooks.dispatcher import dispatch_capture
        return dispatch_capture(event, payload)

    def _reply_ok(self, rid: int, result) -> None:
        self._send({"id": rid, "ok": True, "result": result})

    def _reply_error(self, rid: int, msg: str, *, code: int) -> None:
        self._send({"id": rid, "ok": False, "error": msg, "code": code})

    def _send(self, obj: dict) -> None:
        body = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass


class DaemonServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server bound to localhost. Refuses external addresses."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        secret: str,
        replay_window: int = DEFAULT_REPLAY_WINDOW_SECONDS,
        embedding_manager=None,
    ):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"daemon refuses to bind to non-loopback address {host!r}"
            )
        self.secret = secret
        self.replay_window = replay_window
        # v1.4: optional llamafile lifecycle owner. ``None`` when the
        # ``embedding`` block is absent / disabled; the
        # ``_embedding_ensure`` handler returns ``available: false``
        # in that case rather than crashing the request thread.
        self.embedding_manager = embedding_manager
        self._stop_event = threading.Event()
        super().__init__((host, port), _RequestHandler)

    def request_shutdown(self) -> None:
        """Trigger a clean shutdown from inside a request handler.

        If an embedding manager is attached, reap the llamafile child
        before signalling the server-loop shutdown — otherwise the
        long-running subprocess can outlive the daemon (it was spawned
        with ``start_new_session=True``) and orphan a 1.5 GB process
        the next daemon start would have to kill.
        """
        if self.embedding_manager is not None:
            try:
                self.embedding_manager.shutdown()
            except Exception as e:  # pragma: no cover - defensive
                log.warning("embedding manager shutdown failed: %s", e)
        self._stop_event.set()
        threading.Thread(target=self.shutdown, daemon=True).start()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event


def _build_embedding_manager(cfg: dict):
    """Build an EmbeddingManager from the loaded config or return
    ``None`` if the ``embedding`` block is missing / disabled.

    Lives outside ``serve()`` for testability — the unit tests patch
    this to inject a stub manager without needing a real llamafile.
    """
    block = cfg.get("embedding") if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or not block.get("enabled"):
        return None
    try:
        from claude_hooks.embedding_manager import (
            EmbeddingManager, config_from_dict,
        )
    except Exception as e:
        log.warning("embedding_manager import failed: %s", e)
        return None
    try:
        return EmbeddingManager(config_from_dict(cfg))
    except Exception as e:
        log.warning("EmbeddingManager construction failed: %s", e)
        return None


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    secret_path: Path = DEFAULT_SECRET_PATH,
    replay_window: int = DEFAULT_REPLAY_WINDOW_SECONDS,
) -> int:
    """Start the daemon. Blocks until ``serve_forever`` returns. Returns 0."""
    secret = ensure_secret(secret_path)

    # Try to wire up the embedding manager from current config. Loading
    # the config is best-effort — if it fails for any reason we still
    # serve hooks without the llamafile lifecycle attached, matching the
    # daemon's "fail open" stance on optional subsystems.
    embedding_mgr = None
    try:
        from claude_hooks.config import load_config

        cfg = load_config()
        embedding_mgr = _build_embedding_manager(cfg or {})
    except Exception as e:
        log.debug("could not load config for embedding manager: %s", e)

    server = DaemonServer(
        host, port,
        secret=secret, replay_window=replay_window,
        embedding_manager=embedding_mgr,
    )
    log.info("claude-hooks-daemon listening on %s:%d", host, port)
    if embedding_mgr is not None:
        embedding_mgr.start_reaper()
        log.info(
            "embedding manager attached (idle_timeout=%.0fs, port=%d, mode=%s)",
            embedding_mgr.cfg.idle_timeout_seconds,
            embedding_mgr.cfg.port,
            embedding_mgr.cfg.mode,
        )

    # Background update-check thread: re-reads config on every tick so
    # the user can flip ``update_check.enabled`` at runtime without
    # restarting the daemon. The thread is a daemon-thread and shares
    # ``server.stop_event`` so it dies cleanly on shutdown.
    update_thread = None
    try:
        from claude_hooks.update_check import UpdateCheckThread
        from claude_hooks.config import load_config

        update_thread = UpdateCheckThread(
            config_loader=load_config,
            stop_event=server.stop_event,
        )
        update_thread.start()
    except Exception as e:
        log.debug("update_check thread not started: %s", e)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("shutting down on Ctrl-C")
    finally:
        server.server_close()
        if embedding_mgr is not None:
            try:
                embedding_mgr.shutdown()
            except Exception as e:  # pragma: no cover - defensive
                log.warning(
                    "embedding manager shutdown failed at exit: %s", e
                )
        if update_thread is not None:
            # The thread reads stop_event already; just give it a
            # moment to wake from sleep before we return.
            update_thread.join(timeout=2.0)
    return 0


def _addr_in_use(host: str, port: int) -> bool:
    """Return True if ``host:port`` already accepts TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


if __name__ == "__main__":
    # Tiny CLI: `python -m claude_hooks.daemon` starts the server with defaults.
    # `--port N` overrides the bind port; everything else stays at default.
    import argparse
    parser = argparse.ArgumentParser(prog="claude-hooks-daemon")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--secret-path", type=Path, default=DEFAULT_SECRET_PATH)
    parser.add_argument(
        "--replay-window", type=int, default=DEFAULT_REPLAY_WINDOW_SECONDS,
    )
    args = parser.parse_args()
    if _addr_in_use(args.host, args.port):
        print(
            f"claude-hooks-daemon: {args.host}:{args.port} already in use",
            file=sys.stderr,
        )
        sys.exit(1)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sys.exit(serve(
        args.host, args.port,
        secret_path=args.secret_path,
        replay_window=args.replay_window,
    ))
