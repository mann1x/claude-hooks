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

from claude_hooks.config import expand_user_path
from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.session_end")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("session_end") or {}
    if not hook_cfg.get("enabled", True):
        return None

    ep_cfg = config.get("episodic") or {}
    mode = (ep_cfg.get("mode") or "off").lower()

    if mode == "client":
        return _push_transcript(event, ep_cfg)
    elif mode == "server":
        return _local_sync(ep_cfg)

    log.debug("session ended (episodic off): %s", event.get("session_id"))
    return None


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
        subprocess.Popen(
            [episodic_bin, "sync", "--background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.debug("triggered local episodic-memory sync")
    except FileNotFoundError:
        log.debug("episodic-memory binary not found at %s", episodic_bin)
    except Exception as e:
        log.debug("episodic sync failed: %s", e)
    return None
