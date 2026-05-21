"""
SessionEnd handler — fires when a session terminates.

If ``episodic.mode`` is ``client`` and a ``server_url`` is configured,
pushes the session transcript to the remote episodic-server for indexing.
On the server host, triggers a local ``episodic-memory sync`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from claude_hooks._popen import detach_kwargs
from claude_hooks.config import expand_user_path
from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.session_end")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("session_end") or {}
    if not hook_cfg.get("enabled", True):
        return None

    # LSP engine (v1.9+) — detach this session from the daemon so its
    # affinity locks release and the daemon's refcount drops cleanly.
    # Best-effort, no-op when engine disabled / daemon already gone.
    _detach_lsp_engine_session(event, config)

    ep_cfg = config.get("episodic") or {}
    mode = (ep_cfg.get("mode") or "off").lower()

    if mode == "client":
        return _push_transcript(event, ep_cfg)
    elif mode == "server":
        return _local_sync(ep_cfg)

    log.debug("session ended (episodic off): %s", event.get("session_id"))
    return None


def _detach_lsp_engine_session(event: dict, config: dict) -> None:
    """Best-effort detach from the LSP engine daemon. Soft-fails
    silently on every dependency issue (daemon already reaped, socket
    missing, ipc error). Detaching is courtesy: even if we skip,
    affinity locks expire on their own debounce timer."""
    try:
        from claude_hooks import lsp_integration as _lsp
    except Exception as e:
        log.debug("lsp_engine SessionEnd import skipped: %s", e)
        return

    if not _lsp.engine_enabled(config):
        return
    eng_cfg = _lsp.lsp_engine_cfg(config)
    if not eng_cfg.get("detach_on_session_end", True):
        return

    # SessionEnd needs a real session_id; the fallback (hook-<pid>-<ms>)
    # would never match what SessionStart attached with, so we skip
    # rather than detach the wrong session.
    sid = (event.get("session_id") or "").strip()
    if not sid:
        log.debug("lsp_engine SessionEnd: no session_id; skipping detach")
        return

    cwd = event.get("cwd") or os.getcwd()
    client = _lsp.open_client_safely(
        project_root=cwd, session_id=sid, cfg=config,
    )
    if client is None:
        return
    try:
        try:
            client.detach()
        except Exception as e:
            log.debug("lsp_engine: detach RPC failed: %s", e)
    finally:
        try:
            client.close()
        except Exception as e:
            log.debug("lsp_engine: client.close failed: %s", e)


def _push_transcript(event: dict, ep_cfg: dict) -> Optional[dict]:
    """Push the session transcript to the remote episodic-server."""
    server_url = ep_cfg.get("server_url", "").rstrip("/")
    if not server_url:
        log.warning("episodic client mode but no server_url configured")
        return None

    transcript_path = event.get("transcript_path")
    if not transcript_path:
        log.debug("no transcript_path in event — nothing to push")
        return None

    tp = Path(os.path.expanduser(transcript_path))
    if not tp.exists():
        log.debug("transcript not found: %s", tp)
        return None

    # Read the transcript.
    try:
        data = tp.read_bytes()
    except OSError as e:
        log.warning("failed to read transcript %s: %s", tp, e)
        return None

    if len(data) < 100:
        log.debug("transcript too small (%d bytes), skipping", len(data))
        return None

    # Derive project and session info from the event.
    cwd = event.get("cwd", "")
    session_id = event.get("session_id", tp.stem)
    source_host = socket.gethostname()

    timeout = float(ep_cfg.get("timeout", 10.0))

    headers = {
        "Content-Type": "application/x-ndjson",
        "X-Project": cwd,
        "X-Session-Id": session_id,
        "X-Source-Host": source_host,
        "Connection": "close",
    }
    req = urllib.request.Request(
        f"{server_url}/ingest",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            log.info(
                "transcript pushed to %s: %d bytes, project=%s",
                server_url, len(data), result.get("project", "?"),
            )
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        log.warning("episodic push failed: %s", e)
        return None

    return None


def _local_sync(ep_cfg: dict) -> Optional[dict]:
    """Trigger a local episodic-memory sync (server mode)."""
    episodic_bin = ep_cfg.get("binary", "episodic-memory")
    try:
        # Windows: hook context inherits a console from the .cmd shim;
        # detach_kwargs adds CREATE_NO_WINDOW so the episodic-memory
        # sync child doesn't flash one.
        subprocess.Popen(
            [episodic_bin, "sync", "--background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach_kwargs(),
        )
        log.debug("triggered local episodic-memory sync")
    except FileNotFoundError:
        log.debug("episodic-memory binary not found at %s", episodic_bin)
    except Exception as e:
        log.debug("episodic sync failed: %s", e)
    return None
