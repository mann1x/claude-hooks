"""CLI entry point for the LSP engine daemon.

Two subcommands:

- ``daemon`` — run the long-lived per-project daemon (used by the
  spawn flow in :mod:`claude_hooks.lsp_engine.client`).
- ``status`` — print whether a daemon is running for the project,
  the open files, and the active LSP children.

Run with ``python -m claude_hooks.lsp_engine <subcmd> ...``.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    state_base = Path(args.state_base) if args.state_base else None
    sock = socket_path_for(args.project, base=state_base)
    pid = daemon_pid(args.project, state_base=state_base)
    if not _is_socket_alive(sock):
        print(json.dumps({"running": False, "pid": pid, "socket": str(sock)}))
        return 0
    # Talk to the daemon for the rich status payload.
    client = LspEngineClient(sock, session_id="status-cli")
    client.connect()
    try:
        info = client.status()
    finally:
        client.close()
    info["pid"] = pid
    info["socket"] = str(sock)
    info["running"] = True
    print(json.dumps(info, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.subcommand == "daemon":
        return _run_daemon(args)
    if args.subcommand == "status":
        return _run_status(args)
    return 1  # pragma: no cover — argparse forbids this


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
