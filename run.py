#!/usr/bin/env python3
"""
Cross-platform entry point for claude-hooks.

Invoked from settings.json hook commands as:

    python3 /path/to/claude-hooks/run.py <EventName>
    python  C:\\path\\to\\claude-hooks\\run.py <EventName>

Reads the event JSON from stdin, dispatches to the matching handler, and
writes any hook output to stdout.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Make the repo's claude_hooks/ package importable without needing pip
    # install or PYTHONPATH gymnastics.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    if len(sys.argv) < 2:
        sys.stderr.write("usage: run.py <EventName>\n")
        return 0  # never block Claude — exit 0 even on misuse

    event_name = sys.argv[1]

    # Only the session someone is working in gets the hooks — never a
    # `claude -p` / SDK run it (or a tool like Caliber) spawned. Checked
    # here, in the process Claude Code started, because the daemon that
    # may serve the event below has its own environment, not the
    # caller's. See claude_hooks/session_kind.py.
    from claude_hooks.session_kind import hooks_allowed
    if not hooks_allowed():
        try:
            sys.stdin.read()          # drain, so the writer never sees EPIPE
        except Exception:
            pass
        return 0

    from claude_hooks.dispatcher import dispatch, read_event_from_stdin

    event = read_event_from_stdin()

    # The project root, which the event does not carry: its ``cwd`` is
    # wherever the session last cd'd to. Taken from this process's env
    # because the daemon below has its own, not the caller's.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir and isinstance(event, dict):
        event.setdefault("claude_project_dir", project_dir)

    # Tier 3.8: try the long-lived daemon first if it's running. The
    # client returns None on any failure (no secret, refused connect,
    # timeout, bad response) and we fall back to in-process dispatch.
    # That keeps the daemon strictly optional — installs without it
    # work exactly as before.
    if os.environ.get("CLAUDE_HOOKS_DAEMON_DISABLE", "").strip() not in ("1", "true", "yes"):
        try:
            from claude_hooks.daemon_client import call as _daemon_call
            resp = _daemon_call(event_name, event)
        except Exception:
            resp = None
        if resp is not None and resp.get("ok"):
            result = resp.get("result")
            if isinstance(result, dict):
                import json as _json
                sys.stdout.write(_json.dumps(result))
                sys.stdout.write("\n")
                sys.stdout.flush()
            return 0

    return dispatch(event_name, event)


if __name__ == "__main__":
    raise SystemExit(main())
