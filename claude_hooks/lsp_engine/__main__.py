"""CLI entry point for the LSP engine daemon.

Subcommands:

- ``daemon``  — run the long-lived per-project daemon (used by the
  spawn flow in :mod:`claude_hooks.lsp_engine.client`).
- ``status``  — print whether a daemon is running for the project,
  the open files, and the active LSP children. Also detects stale
  lock files (PID recorded but process gone).
- ``stop``    — ask a running daemon to shut down gracefully via the
  existing ``shutdown`` IPC op. No-op if no daemon is running.
- ``cleanup`` — remove the per-project state directory when no live
  daemon holds the lock. Use after a crash to clear stale locks
  that prevent a fresh spawn.

Run with ``python -m claude_hooks.lsp_engine <subcmd> ...``.

v1.10.3: ``stop`` and ``cleanup`` added per the pandorum
2026-05-22 incident — there was previously no graceful way to
shut a daemon down (Stop-Process leaves a stale lock + state dir
behind, blocking the next spawn). ``status`` now liveness-probes
the lock-file PID so the reported ``pid`` reflects an actually-
running process; a stale lock surfaces as ``stale_lock: true``.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from claude_hooks.lsp_engine.client import (
    LspEngineClient,
    daemon_pid,
)
from claude_hooks.lsp_engine.daemon import (
    Daemon,
    DaemonAlreadyRunning,
    load_daemon_config,
    lock_path_for,
    pid_is_alive,
    project_dir,
    socket_path_for,
)
from claude_hooks.lsp_engine.ipc import _is_socket_alive

log = logging.getLogger("claude_hooks.lsp_engine.cli")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m claude_hooks.lsp_engine",
        description="Session-scoped LSP engine daemon (Phase 1).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    d = sub.add_parser("daemon", help="Run the per-project daemon (foreground).")
    d.add_argument(
        "--project", required=True,
        help="Absolute path to the project root.",
    )
    d.add_argument(
        "--state-base", default=None,
        help="Override base directory for daemon state "
             "(default: ~/.claude/lsp-engine/).",
    )
    d.add_argument(
        "--cclsp-config", default=None,
        help="Override path to cclsp.json (default: $CCLSP_CONFIG_PATH or "
             "<project>/cclsp.json).",
    )
    d.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    s = sub.add_parser("status", help="Print daemon status for a project.")
    s.add_argument(
        "--project", required=True,
        help="Absolute path to the project root.",
    )
    s.add_argument(
        "--state-base", default=None,
        help="Override base directory for daemon state.",
    )

    st = sub.add_parser(
        "stop",
        help="Ask the daemon for this project to shut down gracefully.",
    )
    st.add_argument(
        "--project", required=True,
        help="Absolute path to the project root.",
    )
    st.add_argument(
        "--state-base", default=None,
        help="Override base directory for daemon state.",
    )

    cl = sub.add_parser(
        "cleanup",
        help="Remove the per-project state dir when no live daemon "
             "holds the lock. Use after a crash to clear stale state.",
    )
    cl.add_argument(
        "--project", required=True,
        help="Absolute path to the project root.",
    )
    cl.add_argument(
        "--state-base", default=None,
        help="Override base directory for daemon state.",
    )
    cl.add_argument(
        "--force", action="store_true",
        help="Remove the state dir even if a live daemon holds the "
             "lock (kill it first via 'stop'). Use with care.",
    )

    return p


def _run_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state_base = Path(args.state_base) if args.state_base else None
    servers, engine_cfg = load_daemon_config(
        args.project, cclsp_config_path=args.cclsp_config,
    )
    daemon = Daemon(
        project_root=args.project,
        servers=servers,
        engine_config=engine_cfg,
        state_base=state_base,
    )
    try:
        daemon.run()
    except DaemonAlreadyRunning as e:
        log.error("%s", e)
        return 2
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """Print daemon status as JSON.

    v1.10.3: liveness-probes the lock-file PID. Previously the output
    reported the PID straight from the lock file regardless of whether
    the process still existed — confusing during the pandorum
    2026-05-22 forensics where ``status`` returned a dead PID alongside
    ``running: false``. Now the schema is:

    - ``running: true``  — socket alive, daemon responsive. ``pid``
      reflects the live process.
    - ``running: false`` — socket dead. ``pid`` is None unless a
      stale lock points at a still-live process (rare; would mean the
      daemon died but couldn't unlink its socket). ``stale_lock: true``
      when a lock file exists with a PID that no longer corresponds to
      a running process — that's the signal for ``cleanup``.
    """
    state_base = Path(args.state_base) if args.state_base else None
    sock = socket_path_for(args.project, base=state_base)
    lock_pid = daemon_pid(args.project, state_base=state_base)
    socket_alive = _is_socket_alive(sock)

    if not socket_alive:
        stale_lock = lock_pid is not None and not pid_is_alive(lock_pid)
        # When the lock PID is dead, report pid=None so consumers don't
        # mistake it for a live daemon.
        reported_pid = None if stale_lock else lock_pid
        print(json.dumps({
            "running": False,
            "pid": reported_pid,
            "socket": str(sock),
            "stale_lock": stale_lock,
        }))
        return 0

    # Socket alive — talk to the daemon for the rich payload.
    client = LspEngineClient(sock, session_id="status-cli")
    client.connect()
    try:
        info = client.status()
    finally:
        client.close()
    info["pid"] = lock_pid
    info["socket"] = str(sock)
    info["running"] = True
    info["stale_lock"] = False
    print(json.dumps(info, indent=2))
    return 0


def _run_stop(args: argparse.Namespace) -> int:
    """Ask the daemon for this project to shut down gracefully.

    Sends the daemon's existing ``shutdown`` IPC op (see
    ``Daemon._handle_request`` at op == "shutdown"). The daemon
    schedules its own teardown on a background thread and responds OK
    before exiting, so we get a clean exit code.

    Exit codes:
    - 0 = stop sent, daemon acknowledged
    - 1 = no daemon running (or socket dead) — nothing to stop
    - 2 = stop attempt failed (IPC error, RPC rejected)
    """
    state_base = Path(args.state_base) if args.state_base else None
    sock = socket_path_for(args.project, base=state_base)
    if not _is_socket_alive(sock):
        print(json.dumps({
            "stopped": False,
            "reason": "no daemon running",
            "socket": str(sock),
        }))
        return 1

    client = LspEngineClient(sock, session_id="stop-cli")
    try:
        client.connect()
        # Direct IPC call — bypass attach/detach since we're terminating.
        resp = client._ipc.call("shutdown", session="stop-cli")
        ok = bool(resp.get("ok"))
    except (OSError, RuntimeError) as e:
        print(json.dumps({
            "stopped": False,
            "reason": f"{type(e).__name__}: {e}",
            "socket": str(sock),
        }))
        return 2
    finally:
        try:
            client._ipc.close()
        except Exception:
            pass

    print(json.dumps({
        "stopped": ok,
        "reason": None if ok else resp.get("error"),
        "socket": str(sock),
    }))
    return 0 if ok else 2


def _run_cleanup(args: argparse.Namespace) -> int:
    """Remove the per-project state dir when no live daemon holds it.

    The state dir (``~/.claude/lsp-engine/<hash>/``) holds
    ``daemon.lock``, ``project`` (human-readable resolved path), and
    historically the ``daemon.sock`` on POSIX. A daemon that crashed
    or was killed via ``Stop-Process`` leaves these behind; a fresh
    ``connect_or_spawn`` then sees a stale lock and either races or
    refuses. ``cleanup`` removes the dir when safe (or always, under
    ``--force``).

    Exit codes:
    - 0 = removed (or already absent)
    - 1 = live daemon holds the lock — refuse unless ``--force``
    - 2 = removal failed (permission, etc.)
    """
    state_base = Path(args.state_base) if args.state_base else None
    pdir = project_dir(args.project, base=state_base)
    lock_pid = daemon_pid(args.project, state_base=state_base)
    sock = socket_path_for(args.project, base=state_base)

    if not pdir.exists():
        print(json.dumps({
            "removed": False,
            "reason": "state dir already absent",
            "dir": str(pdir),
        }))
        return 0

    live_daemon = (
        _is_socket_alive(sock)
        or (lock_pid is not None and pid_is_alive(lock_pid))
    )
    if live_daemon and not args.force:
        print(json.dumps({
            "removed": False,
            "reason": (
                f"live daemon (pid={lock_pid}) holds the lock — "
                f"stop it first or pass --force"
            ),
            "dir": str(pdir),
            "pid": lock_pid,
        }))
        return 1

    try:
        shutil.rmtree(pdir)
    except OSError as e:
        print(json.dumps({
            "removed": False,
            "reason": f"{type(e).__name__}: {e}",
            "dir": str(pdir),
        }))
        return 2

    print(json.dumps({
        "removed": True,
        "dir": str(pdir),
        "forced": bool(args.force) and live_daemon,
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.subcommand == "daemon":
        return _run_daemon(args)
    if args.subcommand == "status":
        return _run_status(args)
    if args.subcommand == "stop":
        return _run_stop(args)
    if args.subcommand == "cleanup":
        return _run_cleanup(args)
    return 1  # pragma: no cover — argparse forbids this


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
