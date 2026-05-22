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

    from claude_hooks._popen import detach_kwargs, windowless_python_executable

    proc = subprocess.Popen(
        [windowless_python_executable(), "-m", "claude_hooks.some_daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detach_kwargs(),
    )

``detach_kwargs()`` does **not** set ``stdout`` / ``stderr`` — the
caller decides what to do with the child's pipes. It also does not
override ``cwd`` or ``env``; callers compose those as they always
have.

v1.10.1 adds :func:`windowless_python_executable` — a sibling helper
that swaps ``python.exe`` (console-subsystem) for ``pythonw.exe``
(windows-subsystem) when spawning Python children on Windows.
``detach_kwargs()`` alone is **not enough** for Python children:
``python.exe`` can force-allocate a console at interpreter startup
even with ``CREATE_NO_WINDOW | DETACHED_PROCESS`` set. Combining
both helpers gives the same windowless-spawn guarantee that the
Windows scheduled tasks (registered via ``install.find_conda_env_
pythonw``) get for ``claude-hooks-daemon`` / proxy / forwarder.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
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


def windowless_python_executable() -> str:
    """Return the windowless Python interpreter path for daemon spawns.

    On Windows, ``sys.executable`` typically points at ``python.exe`` —
    a **console-subsystem** binary. Even when its child is started with
    ``CREATE_NO_WINDOW | DETACHED_PROCESS``, the Python interpreter
    itself can force-allocate a console at startup (Windows allocates a
    console for console-subsystem applications that touch the standard
    streams). That console appears as a visible ``cmd.exe`` window on
    the user's desktop, hosting the daemon's logger output — exactly
    what the v1.10.0 LSP-engine daemon hit on pandorum 2026-05-22.

    The fix is to swap ``python.exe`` for ``pythonw.exe`` — a
    **windows-subsystem** binary that has no auto-allocated console. We
    look for it next to ``sys.executable`` in two layouts:

    1. ``...\\envs\\claude-hooks\\pythonw.exe`` (conda Windows)
    2. ``...\\envs\\claude-hooks\\Scripts\\pythonw.exe`` (some venvs)

    Mirrors :func:`install.find_conda_env_pythonw` but resolves at
    runtime against the live interpreter — no need to re-discover the
    conda env. Falls back to ``sys.executable`` unchanged when:

    - we're on POSIX (``pythonw.exe`` doesn't exist),
    - we're on Windows but the interpreter is already ``pythonw.exe``,
    - we're on Windows but no ``pythonw.exe`` sibling exists (stripped
      Python build, custom embedded interpreter, etc.).

    The ``CREATE_NO_WINDOW | DETACHED_PROCESS`` flags from
    :func:`detach_kwargs` remain belt-and-braces — they handle the
    inherit-parent-console path while this helper handles the
    auto-allocate path.
    """
    if os.name != "nt":
        return sys.executable

    py = Path(sys.executable)
    name = py.name.lower()
    if name == "pythonw.exe":
        return str(py)

    # Layout 1: ``...\envs\<name>\python.exe`` — sibling pythonw.exe.
    pyw = py.with_name("pythonw.exe")
    if pyw.is_file():
        return str(pyw)

    # Layout 2: ``...\envs\<name>\Scripts\python.exe`` — also sibling.
    # (Handled by the same with_name() call above, but kept explicit
    # for parity with install.find_conda_env_pythonw's layout 2.)

    return sys.executable
