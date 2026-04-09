"""
Hook event dispatcher.

The single entry point ``run.py`` reads the event name from argv[1], parses
the event JSON from stdin, and calls ``dispatch(event_name, event)``. The
dispatcher routes to the matching handler in ``claude_hooks/hooks/`` and
emits the JSON response (if any) on stdout.

A handler returns a dict that becomes the JSON written to stdout. Returning
``None`` means "no output, exit 0". Any unhandled exception is caught,
logged, and turned into a no-op so the hook never blocks Claude.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any, Optional

from claude_hooks.config import expand_user_path, load_config, project_disabled
from claude_hooks.providers import REGISTRY, Provider, ServerCandidate

# Map Claude Code event name → handler module name (under claude_hooks/hooks/).
HANDLERS = {
    "UserPromptSubmit": "user_prompt_submit",
    "SessionStart": "session_start",
    "Stop": "stop",
    "SessionEnd": "session_end",
    "PreToolUse": "pre_tool_use",
}


def dispatch(event_name: str, event: dict) -> int:
    """Run the handler for ``event_name`` and write its output. Returns exit code."""
    cfg = load_config()
    _setup_logging(cfg)
    log = logging.getLogger("claude_hooks.dispatcher")

    log.debug("dispatch: event=%s session=%s", event_name, event.get("session_id"))

    # Project-level opt-out via marker file.
    cwd = event.get("cwd") or ""
    marker = cfg.get("disable_marker_filename") or ".claude-hooks-disable"
    if cwd and project_disabled(cwd, marker):
        log.info("project disabled via %s — exiting silently", marker)
        return 0

    handler_name = HANDLERS.get(event_name)
    if not handler_name:
        log.debug("no handler for event '%s' — no-op", event_name)
        return 0

    try:
        module = __import__(
            f"claude_hooks.hooks.{handler_name}",
            fromlist=["handle"],
        )
    except Exception as e:
        log.error("failed to import handler %s: %s", handler_name, e)
        return 0

    handle = getattr(module, "handle", None)
    if not callable(handle):
        log.error("handler %s has no handle() function", handler_name)
        return 0

    providers = build_providers(cfg)

    try:
        output = handle(event=event, config=cfg, providers=providers)
    except Exception:
        log.error("handler %s crashed:\n%s", handler_name, traceback.format_exc())
        return 0

    if output:
        try:
            sys.stdout.write(json.dumps(output))
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception as e:
            log.error("failed to write output: %s", e)
            return 0

    return 0


def build_providers(cfg: dict) -> list[Provider]:
    """
    Instantiate all enabled providers from the config. Providers with no
    URL configured are skipped silently. Returns the list in registration
    order so output is deterministic.
    """
    log = logging.getLogger("claude_hooks.dispatcher")
    out: list[Provider] = []
    provider_cfgs = (cfg.get("providers") or {})

    for cls in REGISTRY:
        pcfg = provider_cfgs.get(cls.name) or {}
        if not pcfg.get("enabled"):
            continue
        url = (pcfg.get("mcp_url") or "").strip()
        if not url:
            log.debug("provider %s has no mcp_url configured — skipping", cls.name)
            continue
        candidate = ServerCandidate(
            server_key=cls.name,
            url=url,
            headers=pcfg.get("headers") or {},
            source="config",
            confidence="manual",
        )
        out.append(cls(candidate, options=pcfg))
    return out


def _setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging") or {}
    level_name = (log_cfg.get("level") or "info").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("claude_hooks")
    if root.handlers:
        return  # already configured

    path_str = log_cfg.get("path") or ""
    handler: logging.Handler
    if path_str:
        try:
            log_path = expand_user_path(path_str)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


def read_event_from_stdin() -> dict:
    """Read the event JSON Claude Code pipes to a hook on stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
