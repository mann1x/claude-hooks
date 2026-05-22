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

    Windows: ``creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS |
    CREATE_BREAKAWAY_FROM_JOB``.
    POSIX:   ``start_new_session = True``.

    ``CREATE_BREAKAWAY_FROM_JOB`` (v1.10.6+, surfaced on pandorum
    2026-05-22): ``DETACHED_PROCESS`` alone only detaches a child
    from its parent's console — it does **not** detach from a job
    object the parent may be in. SSH / cmd / some sandboxes put
    transient jobs around the invocation tree; when the parent
    exits, the OS terminates every member of the job, including
    the daemon we just spawned. The user observed this as
    "daemon dies seconds after the spawn script exits" even though
    the spawn flags were set. ``CREATE_BREAKAWAY_FROM_JOB`` makes
    the child a sibling of the job rather than a member, so the
    job's cleanup-on-parent-exit doesn't reach it. The pandorum
    bench: 75036 alive at t=2s in-script, 4s post-exit, 30s
    post-exit (status RPC still responds with running:true). Pre-
    fix the same flow saw the daemon vanish within 0.5 s of parent
    exit. The flag is harmless when the parent has no job (the
    common case) — Windows ignores it silently.

    Strict-job environments (``JOB_OBJECT_LIMIT_BREAKAWAY_OK``
    cleared) refuse the flag with ``ACCESS_DENIED``. Use
    :func:`popen_detached` rather than raw ``subprocess.Popen``
    when you want a graceful fallback to "no breakaway" in those
    contexts.

    ``getattr`` shields against build-time absence of the flag
    constants on stripped-down Pythons — they evaluate to ``0`` and
    the bitwise-OR keeps the call safe.
    """
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            ),
        }
    return {"start_new_session": True}


def popen_detached(cmd, **kwargs) -> "subprocess.Popen":
    """``subprocess.Popen`` wrapper that applies :func:`detach_kwargs`
    and gracefully falls back when ``CREATE_BREAKAWAY_FROM_JOB``
    is refused.

    The fallback covers the rare Windows case where the parent is
    in a job object whose ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` bit is
    cleared (sandboxing / certain containerised environments). In
    that case ``CreateProcess`` returns ``ERROR_ACCESS_DENIED`` and
    Popen raises ``OSError`` with ``winerror == 5``. We retry
    without the breakaway flag — the child won't survive parent
    exit there, but at least it spawns; otherwise the operator
    has no daemon at all.

    Callers that need to handle the spawn failure themselves can
    keep using raw ``subprocess.Popen(**detach_kwargs())`` and
    react to OSError. Use this helper for the common case where
    "best effort, fall back to less-detached" is the right move.
    """
    merged_kwargs = dict(kwargs)
    detach = detach_kwargs()
    # Merge creationflags rather than overwrite — callers may set
    # additional flags (e.g. CREATE_NEW_CONSOLE for debugging).
    if "creationflags" in detach:
        merged_kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | detach["creationflags"]
        )
    for k, v in detach.items():
        if k != "creationflags":
            merged_kwargs.setdefault(k, v)

    try:
        return subprocess.Popen(cmd, **merged_kwargs)
    except OSError as e:
        # winerror == 5 (ERROR_ACCESS_DENIED) when the parent's
        # job rejects breakaway. Strip the BREAKAWAY bit and
        # retry; if it still fails the OSError propagates as
        # before.
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if (
            os.name != "nt"
            or not breakaway
            or getattr(e, "winerror", None) != 5
            or not (merged_kwargs.get("creationflags", 0) & breakaway)
        ):
            raise
        merged_kwargs["creationflags"] &= ~breakaway
        return subprocess.Popen(cmd, **merged_kwargs)


def silent_subprocess_kwargs() -> dict[str, Any]:
    """Return the platform-specific kwargs needed to spawn a
    **non-detached** child that still has no visible console window.

    Distinct from :func:`detach_kwargs` because callers that
    ``subprocess.run`` (block on the child + capture output) must NOT
    set ``DETACHED_PROCESS`` — that flag detaches the child from the
    parent's stdio, which breaks ``capture_output=True`` /
    ``stdin=PIPE`` / ``stdout=PIPE``. ``CREATE_NO_WINDOW`` alone is
    the right move there.

    Why this matters: the LSP-engine daemon itself runs windowless
    (spawn flags applied at ``client._spawn_daemon``), so it has no
    console. When it then calls ``subprocess.run`` for a compile
    command like ``msbuild`` WITHOUT this flag, Windows allocates a
    fresh console for the child — which pops up on the user's
    desktop. The daemon should be a framework-level guarantee that
    every child of every operation it runs is windowless; callers
    shouldn't have to know to set this. Surfaced when an msbuild
    window flashed in front of the user on pandorum 2026-05-22.

    Windows: ``creationflags = CREATE_NO_WINDOW``.
    POSIX:   ``{}`` (empty) — no equivalent needed.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


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
