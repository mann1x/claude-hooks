"""Is this the session someone is working in, or a process it spawned?

claude-hooks exists for the interactive session: recall for the person
typing, a store of what they did, their mailbox. A ``claude -p`` run is
not that session even when it starts in the same directory — Caliber's
pre-commit refresh runs one in the repo on every commit, and so does any
script or tool that shells out to Claude Code. Running the hooks there
was all cost and some damage: it recalled memory into a prompt nobody
reads, stored the run's turns as if they were the operator's (they came
back later as recall), registered the run under the project's mailbox
alias — evicting the real session and then deleting the row at exit —
and handed it the project's unread mail with a Stop nudge to read it,
which it did. The session the mail was for never saw it.

Claude Code says which kind of run it is, to hooks and MCP children
alike (measured on 2.1.280):

==============================  ===========  ==================
variable                        interactive  ``claude -p`` / SDK
==============================  ===========  ==================
``CLAUDE_CODE_ENTRYPOINT``      ``cli``      ``sdk-cli`` / ``sdk-ts`` / ``sdk-py``
``CLAUDE_CODE_SESSION_ATTENDED`` ``1``        ``0``
==============================  ===========  ==================

``CLAUDE_CODE_CHILD_SESSION=1`` is **not** a signal: the parent exports
it to everything it spawns, its own tool shells included, so it is set
in both columns.

With neither variable set (an older Claude Code, another client) the
run counts as interactive, so nothing that works today stops working.
``hooks.run_in_subprocesses: true`` in the config turns the gate off.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

SUBPROCESS_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-ts", "sdk-py"})


def subprocess_reason(env: Optional[Mapping[str, str]] = None) -> str:
    """Why this run is a spawned, unattended one — or "" if it is not."""
    env = os.environ if env is None else env
    attended = (env.get("CLAUDE_CODE_SESSION_ATTENDED") or "").strip()
    if attended == "0":
        return "CLAUDE_CODE_SESSION_ATTENDED=0"
    entry = (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip().lower()
    if entry in SUBPROCESS_ENTRYPOINTS:
        return f"CLAUDE_CODE_ENTRYPOINT={entry}"
    return ""


def is_subprocess(env: Optional[Mapping[str, str]] = None) -> bool:
    return bool(subprocess_reason(env))


def hooks_allowed(config: Optional[dict] = None,
                  env: Optional[Mapping[str, str]] = None) -> bool:
    """Should claude-hooks act in this run at all?"""
    if not is_subprocess(env):
        return True
    if config is None:
        try:
            from claude_hooks.config import load_config
            config = load_config()
        except Exception:
            return False
    return bool((config.get("hooks") or {}).get("run_in_subprocesses", False))
