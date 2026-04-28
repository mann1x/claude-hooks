"""Cross-platform control surface for the long-lived claude-hooks-daemon.

The daemon is launched by an autostart entry installed by ``install.py``
(systemd / launchd / Windows Task Scheduler). Once running, this CLI
gives users a single command to inspect and manage it without
remembering each platform's tooling::

    claude-hooks-daemon-ctl status     # alive? autostart entry?
    claude-hooks-daemon-ctl start      # /Run / start / kickstart
    claude-hooks-daemon-ctl stop       # graceful _shutdown, fall back to platform End
    claude-hooks-daemon-ctl restart    # stop + bounded ping wait + start
    claude-hooks-daemon-ctl tail [-n]  # print log path or last N lines

Design choices:

- **Graceful first, force second.** ``stop`` issues an HMAC-signed
  ``_shutdown`` over the daemon's own socket; only on failure does it
  fall through to ``schtasks /End`` / ``systemctl --user stop`` /
  ``launchctl kill SIGTERM``. The daemon's reply arrives before its
  request_shutdown thread tears down the socket.
- **Exit codes are scriptable.** ``status`` returns 0 when the daemon
  is responding, 1 when down but registered, 2 when not registered at
  all. Other verbs return 0 on apparent success, non-zero on any
  inability to do the thing.
- **No new dependencies.** Re-uses ``install._start_daemon_via_platform``
  / ``_detect_existing_daemon_entry`` and adds tiny stop/log helpers
  alongside them rather than duplicating dispatch logic. Keeps the
  Windows / systemd / launchd quirks in one place.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Late imports to avoid pulling install.py at module-level — install.py
# triggers conda detection which is slow and irrelevant for status.
from claude_hooks import daemon_client
from claude_hooks.daemon import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SECRET_PATH


_GRACEFUL_STOP_TIMEOUT = 5.0   # seconds to wait for ping to drop after stop
_RESTART_PING_TIMEOUT = 15.0   # seconds to wait for ping to come up after start


# ===================================================================== #
# Platform-specific stop / log helpers
# ===================================================================== #
def _platform_stop() -> bool:
    """Force-stop via the platform manager. Returns True if the manager
    accepted the command (which doesn't strictly guarantee the process
    is gone, but is the best signal we have without sniffing again)."""
    if os.name == "nt":
        # schtasks /End is non-elevated-friendly for the current user's
        # tasks — no UAC prompt unlike /Create.
        rc = subprocess.run(
            ["schtasks", "/End", "/TN", "claude-hooks-daemon"],
            capture_output=True, text=True,
        )
        return rc.returncode == 0

    if Path("/etc/systemd/system/claude-hooks-daemon.service").exists():
        rc = subprocess.run(
            ["systemctl", "stop", "claude-hooks-daemon.service"],
            capture_output=True, text=True,
        )
        return rc.returncode == 0

    plist = (
        Path.home() / "Library" / "LaunchAgents"
        / "com.claude-hooks.daemon.plist"
    )
    if plist.exists():
        try:
            uid = os.getuid()  # type: ignore[attr-defined]
        except AttributeError:
            uid = 0
        rc = subprocess.run(
            ["launchctl", "kill", "SIGTERM",
             f"gui/{uid}/com.claude-hooks.daemon"],
            capture_output=True, text=True,
        )
        return rc.returncode == 0

    return False


def _platform_log_path() -> Optional[Path]:
    """Best-effort path to the daemon's stderr/stdout log, or None.

    On Linux/macOS the platform manager owns the log (journalctl /
    Apple's unified log), so we return None and ``tail`` prints the
    canonical command instead. On Windows we ship a fixed path because
    Task Scheduler doesn't expose its own log surface.
    """
    if os.name == "nt":
        # The Task Scheduler entry redirects stderr here. install.py
        # writes this path into the XML's Action.WorkingDirectory neighbour.
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".claude" / "claude-hooks-daemon.log"
    return None


def _platform_log_command() -> Optional[list[str]]:
    """The shell command users should run to tail logs on platforms
    where we don't own the file. Returns None if a file path is
    available via ``_platform_log_path`` instead."""
    if os.name == "nt":
        return None
    if Path("/etc/systemd/system/claude-hooks-daemon.service").exists():
        return ["journalctl", "-u", "claude-hooks-daemon.service", "-f"]
    if (Path.home() / "Library" / "LaunchAgents" / "com.claude-hooks.daemon.plist").exists():
        return ["log", "stream", "--predicate",
                "process == 'python' OR subsystem == 'com.claude-hooks.daemon'"]
    return None


# ===================================================================== #
# Verbs
# ===================================================================== #
def cmd_status(*, host: str, port: int, secret_path: Path) -> int:
    """Report whether the daemon is alive and the autostart entry exists.

    Exit codes::

      0   responding on host:port
      1   not responding but autostart is installed
      2   not responding and no autostart entry — daemon was never installed
    """
    alive = daemon_client.ping(
        host=host, port=port, secret_path=Path(secret_path),
    )
    entry = _detect_entry()

    if alive:
        print(f"daemon: responding on {host}:{port}")
        if entry:
            print(f"autostart: {entry}")
        else:
            print("autostart: NOT INSTALLED — daemon is running ad-hoc")
        return 0

    if entry:
        print(f"daemon: NOT RESPONDING on {host}:{port}")
        print(f"autostart: {entry}")
        print("hint: claude-hooks-daemon-ctl start")
        return 1

    print("daemon: NOT RESPONDING")
    print("autostart: NOT INSTALLED")
    print("hint: python install.py  (then opt in to the daemon)")
    return 2


def cmd_start(*, host: str, port: int, secret_path: Path,
              wait: float = _RESTART_PING_TIMEOUT) -> int:
    """Idempotent start. If already responding, do nothing and return 0."""
    if daemon_client.ping(
        host=host, port=port, secret_path=Path(secret_path), timeout=1.5,
    ):
        print(f"daemon: already responding on {host}:{port}")
        return 0

    if not _detect_entry():
        print("daemon: no autostart entry — run `python install.py` first",
              file=sys.stderr)
        return 2

    print("daemon: starting via platform manager...")
    _platform_start()
    if _wait_for_ping(host=host, port=port, secret_path=Path(secret_path),
                     timeout=wait):
        print(f"daemon: responding on {host}:{port}")
        return 0
    print(f"daemon: did not come up within {wait:.0f}s", file=sys.stderr)
    return 1


def cmd_stop(*, host: str, port: int, secret_path: Path,
             wait: float = _GRACEFUL_STOP_TIMEOUT) -> int:
    """Graceful first (HMAC ``_shutdown``), force second (platform End)."""
    spath = Path(secret_path)
    alive = daemon_client.ping(
        host=host, port=port, secret_path=spath, timeout=1.5,
    )
    if not alive:
        print("daemon: not running")
        # Don't bother with platform End if it isn't even up.
        return 0

    print("daemon: requesting graceful shutdown...")
    if daemon_client.shutdown(
        host=host, port=port, secret_path=spath, timeout=2.0,
    ) and _wait_for_ping_gone(
        host=host, port=port, secret_path=spath, timeout=wait,
    ):
        print("daemon: stopped")
        return 0

    print("daemon: graceful shutdown didn't take — falling back to platform manager")
    if _platform_stop() and _wait_for_ping_gone(
        host=host, port=port, secret_path=spath, timeout=wait,
    ):
        print("daemon: stopped (via platform manager)")
        return 0

    print(f"daemon: still responding {wait:.0f}s after stop attempts",
          file=sys.stderr)
    return 1


def cmd_restart(*, host: str, port: int, secret_path: Path) -> int:
    """Stop (idempotent) then start. Skips the stop when daemon isn't up."""
    rc_stop = cmd_stop(host=host, port=port, secret_path=secret_path)
    if rc_stop != 0:
        return rc_stop
    # A minor delay lets the platform manager fully release the port —
    # particularly relevant on Windows where the previous Task Scheduler
    # process may still be tearing down when we reissue /Run.
    time.sleep(0.5)
    return cmd_start(host=host, port=port, secret_path=secret_path)


def cmd_tail(*, n: int) -> int:
    """Show the platform-specific log path or invocation command."""
    path = _platform_log_path()
    if path is not None:
        if not path.exists():
            print(f"log file does not exist yet: {path}")
            print("hint: start the daemon first")
            return 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            print(f"could not read {path}: {e}", file=sys.stderr)
            return 1
        for line in lines[-n:]:
            sys.stdout.write(line)
        return 0

    cmd = _platform_log_command()
    if cmd is not None:
        print("daemon logs are owned by the platform — run:")
        print(f"  {' '.join(cmd)}")
        return 0

    print("no autostart entry installed — daemon writes to the calling shell's stderr",
          file=sys.stderr)
    return 2


# ===================================================================== #
# Helpers — small wrappers around install.py's existing dispatch
# ===================================================================== #
def _detect_entry() -> Optional[str]:
    """Lazy import of install._detect_existing_daemon_entry. Returns the
    short description string ('Windows scheduled task ...' / etc.) or None.
    Lazy because install.py runs conda detection at import."""
    try:
        import install  # noqa: WPS433
    except ImportError:
        return None
    fn = getattr(install, "_detect_existing_daemon_entry", None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def _platform_start() -> None:
    """Lazy import of install._start_daemon_via_platform."""
    try:
        import install  # noqa: WPS433
    except ImportError:
        return
    fn = getattr(install, "_start_daemon_via_platform", None)
    if fn is None:
        return
    try:
        fn()
    except Exception as e:
        print(f"  [!!] platform start failed: {e}", file=sys.stderr)


def _wait_for_ping(*, host: str, port: int, secret_path: Path,
                   timeout: float, interval: float = 0.25) -> bool:
    """Poll ping until True or timeout. Returns final state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if daemon_client.ping(
            host=host, port=port, secret_path=secret_path, timeout=0.5,
        ):
            return True
        time.sleep(interval)
    return False


def _wait_for_ping_gone(*, host: str, port: int, secret_path: Path,
                        timeout: float, interval: float = 0.1) -> bool:
    """Poll ping until False or timeout. Returns True if daemon stopped."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not daemon_client.ping(
            host=host, port=port, secret_path=secret_path, timeout=0.5,
        ):
            return True
        time.sleep(interval)
    return False


# ===================================================================== #
# CLI
# ===================================================================== #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claude-hooks-daemon-ctl",
        description="Status / start / stop / restart / tail the claude-hooks-daemon.",
    )
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"daemon host (default {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"daemon port (default {DEFAULT_PORT})")
    ap.add_argument("--secret", type=Path, default=DEFAULT_SECRET_PATH,
                    help="path to the daemon's HMAC secret file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="report responding / autostart / hints")
    sub.add_parser("start", help="start via platform manager (idempotent)")
    sub.add_parser("stop", help="graceful shutdown, fall back to platform End")
    sub.add_parser("restart", help="stop + start, with bounded ping waits")
    p_tail = sub.add_parser("tail", help="show last N lines or log command")
    p_tail.add_argument("-n", type=int, default=80,
                        help="number of trailing lines (default 80)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    common = {
        "host": args.host, "port": args.port, "secret_path": args.secret,
    }
    if args.cmd == "status":
        return cmd_status(**common)
    if args.cmd == "start":
        return cmd_start(**common)
    if args.cmd == "stop":
        return cmd_stop(**common)
    if args.cmd == "restart":
        return cmd_restart(**common)
    if args.cmd == "tail":
        return cmd_tail(n=args.n)
    return 2  # unreachable — argparse rejects unknown subcommand


if __name__ == "__main__":
    raise SystemExit(main())
