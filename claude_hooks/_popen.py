"""Shared subprocess-spawn helpers for daemonised / detached children.

Every long-lived or fire-and-forget ``subprocess.Popen`` we issue on
Windows needs ``CREATE_NO_WINDOW | DETACHED_PROCESS`` in
``creationflags`` so the child doesn't allocate (or inherit) a
console window on the user's desktop. POSIX equivalents — and the
shared idiom across the codebase — collapse onto
``start_new_session=True``.

The cross-platform pattern was scattered across at least seven
spawn sites (consultants_forwarder, store_async, code_graph
builder, gitnexus / axon integrations, session_end episodic sync,
lsp_engine.lsp's server spawn). Pre-v1.8.1 only the well-managed
spawns (chat_model_manager, embedding_manager, claudemem_reindex,
lsp_engine.client.spawn_daemon) had the flags; the rest could pop
a visible console flash on Windows.

#221 (2026-05-19) consolidates the pattern here. New spawn sites
should import :func:`detach_kwargs` rather than re-roll the
``if os.name == "nt"`` branch.

Example::

    from claude_hooks._popen import detach_kwargs

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detach_kwargs(),
    )

``detach_kwargs()`` does **not** set ``stdout`` / ``stderr`` — the
caller decides what to do with the child's pipes. It also does not
override ``cwd`` or ``env``; callers compose those as they always
have.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def detach_kwargs() -> dict[str, Any]:
    """Return the platform-specific ``subprocess.Popen`` kwargs needed
    to spawn a detached, windowless child.

    Windows: ``creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS``.
    POSIX:   ``start_new_session = True``.

    ``getattr`` shields against build-time absence of the flag
    constants on stripped-down Pythons — they evaluate to ``0`` and
    the bitwise-OR keeps the call safe.
    """
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            ),
        }
    return {"start_new_session": True}
