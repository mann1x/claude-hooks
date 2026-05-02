#!/usr/bin/env python3
"""
claude-hooks installer.

Cross-platform interactive installer that:

1. Detects MCP servers in ~/.claude.json (Linux) or %USERPROFILE%\\.claude.json
2. Asks each provider to identify its candidates by name
3. Falls back to tool-probe detection for unmatched providers
4. Asks the user to confirm matches (and prompts for URL if none found)
5. Verifies each chosen server with a real MCP call
6. Writes config/claude-hooks.json
7. Backs up and merges hook entries into ~/.claude/settings.json
   (entries owned by claude-hooks are tagged with `_managedBy: "claude-hooks"`
    so re-runs are idempotent)

Flags:

    --dry-run         show what would happen without writing anything
    --non-interactive fail if any prompt would be needed
    --uninstall       remove claude-hooks entries from settings.json
    --probe           force tool-probe detection even if name match found
    --config <path>   alternate claude-hooks.json path
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

# Make claude_hooks importable when running from a checkout.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from claude_hooks.config import (
    DEFAULT_CONFIG,
    default_config_path,
    load_config,
    save_config,
)
from claude_hooks.detect import (
    DetectionReport,
    claude_config_path,
    detect_all,
    load_claude_config,
    probe_unmatched,
)
from claude_hooks.providers import REGISTRY, ServerCandidate

MANAGED_BY = "claude-hooks"

# The conda env Python that bin/claude-hook prefers at runtime. Resolved
# by ``find_conda_env_python`` which probes a list of common layouts and
# (as a last resort) asks conda directly via ``conda env list --json``.
# Kept as fallback constants for tests / dry-run paths that don't want
# to spawn conda.
CONDA_ENV_NAME = "claude-hooks"
CONDA_PY_LINUX = Path.home() / "anaconda3" / "envs" / CONDA_ENV_NAME / "bin" / "python"
CONDA_PY_WIN = Path.home() / "anaconda3" / "envs" / CONDA_ENV_NAME / "python.exe"

# Resolved env path is cached so repeated calls during a single install
# run don't re-spawn ``conda env list``.
_CONDA_PY_CACHE: Optional[Path] = None


def find_conda_env_pythonw(env_name: str = CONDA_ENV_NAME) -> Optional[Path]:
    """Return ``pythonw.exe`` from the named conda env, or None if missing.

    ``pythonw.exe`` runs without a console window -- used by the Windows
    daemon scheduled task so it doesn't flash a permanent cmd.exe box on
    the user's desktop. Sits alongside ``python.exe`` in the same env;
    we just swap the filename rather than re-running the env-list probe.
    Returns None when no pythonw.exe exists alongside a discovered
    ``python.exe`` (very old Python builds, custom installs).
    """
    py = find_conda_env_python(env_name)
    if not py.exists():
        return None
    # Layout 1: ``...\envs\<name>\python.exe`` (Anaconda/Miniconda Win)
    pyw = py.parent / "pythonw.exe"
    if pyw.exists():
        return pyw
    # Layout 2: ``...\envs\<name>\Scripts\python.exe`` (some venvs)
    if py.parent.name.lower() == "scripts":
        pyw_alt = py.parent / "pythonw.exe"
        if pyw_alt.exists():
            return pyw_alt
    # Layout 3: ``...\envs\<name>\bin\python`` (POSIX) -- no pythonw on POSIX.
    return None


def find_conda_env_python(env_name: str = CONDA_ENV_NAME) -> Path:
    """Locate the Python interpreter inside the named conda env.

    Probes hardcoded common layouts first (fast -- no subprocess), then
    falls back to ``conda env list --json`` and walks the prefixes it
    reports. Returns the platform-default fallback path when nothing is
    found, so callers can still ``.exists()``-check on it.

    Cached after first successful probe per process.
    """
    global _CONDA_PY_CACHE
    if _CONDA_PY_CACHE is not None and _CONDA_PY_CACHE.exists():
        return _CONDA_PY_CACHE

    # Step 1 -- try common install paths without spawning conda. Covers:
    #   - Linux:   ~/anaconda3, ~/miniconda3, /opt/conda
    #   - Windows: ~/Anaconda3, ~/Miniconda3 (capitalised), ~/anaconda3
    #   - Both:    bin/python (POSIX) and python.exe / Scripts/python.exe (Win)
    home = Path.home()
    candidates: list[Path] = []
    roots = [
        home / "anaconda3", home / "miniconda3",
        home / "Anaconda3", home / "Miniconda3",
        Path("/opt/conda"), Path("/opt/miniconda3"),
        Path("/opt/anaconda3"), Path("C:/ProgramData/Anaconda3"),
        Path("C:/ProgramData/Miniconda3"),
    ]
    for root in roots:
        env = root / "envs" / env_name
        candidates += [
            env / "bin" / "python",
            env / "bin" / "python.exe",
            env / "Scripts" / "python.exe",
            env / "python.exe",
        ]
    for c in candidates:
        if c.exists():
            _CONDA_PY_CACHE = c
            return c

    # Step 2 -- ask conda where it thinks the env lives.
    conda_bin = _find_conda()
    if conda_bin:
        try:
            rc = subprocess.run(
                [conda_bin, "env", "list", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if rc.returncode == 0:
                envs = json.loads(rc.stdout).get("envs") or []
                for prefix_str in envs:
                    prefix = Path(prefix_str)
                    if prefix.name != env_name:
                        continue
                    for layout in (
                        prefix / "bin" / "python",
                        prefix / "bin" / "python.exe",
                        prefix / "Scripts" / "python.exe",
                        prefix / "python.exe",
                    ):
                        if layout.exists():
                            _CONDA_PY_CACHE = layout
                            return layout
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass

    # Last resort: return the platform-default fallback. Caller will
    # ``.exists()`` it; if it doesn't, the install path proceeds with
    # system python3.
    return CONDA_PY_WIN if os.name == "nt" else CONDA_PY_LINUX

# Hook entries to install in ~/.claude/settings.json. Each event has its own
# matcher block; matchers are empty strings (= match everything) for events
# that don't carry a tool name, and "Bash|Edit|Write" for PreToolUse.
HOOK_TEMPLATE = {
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} UserPromptSubmit",
                    "timeout": 15,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} SessionStart",
                    "timeout": 5,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} Stop",
                    "timeout": 20,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
    "SessionEnd": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} SessionEnd",
                    "timeout": 10,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
}

# PreToolUse is opt-in -- added only if the user enabled it in config.
PRE_TOOL_USE_TEMPLATE = {
    "PreToolUse": [
        {
            "matcher": "Bash|Edit|Write|MultiEdit",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} PreToolUse",
                    "timeout": 8,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
}

# PostToolUse runs the ruff diagnostics handler after Edit/Write/
# MultiEdit. Matches only file-editing tools so we don't pay the
# subprocess cost on Read/Bash/Grep. Enabled by default — the hook
# itself early-exits on non-Python files (no ruff invocation), so
# the cost when nothing applies is sub-millisecond.
POST_TOOL_USE_TEMPLATE = {
    "PostToolUse": [
        {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} PostToolUse",
                    "timeout": 10,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
}

# PreCompact synthesises a /wrapup-shaped summary just before
# Claude Code auto-compacts the conversation. The handler self-gates
# on (1) hooks.pre_compact.enabled and (2) the wrapup skill being
# installed, so the wired entry is harmless when either condition
# is false. Timeout is generous because we read the full transcript.
PRE_COMPACT_TEMPLATE = {
    "PreCompact": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "{cmd} PreCompact",
                    "timeout": 20,
                    "_managedBy": MANAGED_BY,
                }
            ],
        }
    ],
}


def _find_conda() -> Optional[str]:
    """Find the conda executable, trying common locations.

    Handles the Windows variants (Miniconda3 capitalised, ``conda.bat``
    in ``condabin``) and the Linux/macOS variants (lowercase miniconda3,
    /opt/conda). ``shutil.which`` finds ``conda`` on PATH first when an
    env is active.
    """
    # Check if conda is already on PATH (e.g. env is active).
    found = shutil.which("conda")
    if found:
        return found
    # Windows often has only conda.bat on PATH.
    if os.name == "nt":
        found = shutil.which("conda.bat")
        if found:
            return found

    home = Path.home()
    candidates: list[Path] = []
    for root in (
        home / "anaconda3", home / "miniconda3",
        home / "Anaconda3", home / "Miniconda3",
        Path("/opt/conda"), Path("/opt/miniconda3"),
        Path("/opt/anaconda3"),
        Path("C:/ProgramData/Anaconda3"),
        Path("C:/ProgramData/Miniconda3"),
    ):
        cb = root / "condabin"
        # Windows: condabin/conda.bat. POSIX: condabin/conda.
        candidates += [cb / "conda", cb / "conda.bat", cb / "conda.exe"]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


_PROXY_STACK_UNITS = (
    # (src_file_under_systemd, install_name, is_timer)
    ("claude-hooks-proxy.service", "claude-hooks-proxy.service", False),
    ("claude-hooks-rollup.service", "claude-hooks-rollup.service", False),
    ("claude-hooks-rollup.timer", "claude-hooks-rollup.timer", True),
    ("claude-hooks-dashboard.service", "claude-hooks-dashboard.service", False),
)


def _install_proxy_stack_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the proxy + rollup-timer + dashboard systemd units
    when ``proxy.enabled`` is true.

    Linux-only (systemd). Idempotent -- skips units that already
    exist. Substitutes ``__REPO_PATH__`` / ``__HOME__`` into the
    template files under ``systemd/`` before writing to
    ``/etc/systemd/system/``.
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    proxy_cfg = (cfg.get("proxy") or {})
    if not proxy_cfg.get("enabled", False):
        return

    print("\n==> Proxy systemd units")
    src_dir = HERE / "systemd"
    missing = [
        name for (_, name, _) in _PROXY_STACK_UNITS
        if not (Path("/etc/systemd/system") / name).exists()
    ]
    if not missing:
        print("  All units already installed.")
        return

    print(f"  Missing: {', '.join(missing)}")
    print(f"  Will install to /etc/systemd/system/ with __REPO_PATH__ = {HERE}")
    if dry_run:
        print("  [dry-run] skipping write.")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input("  Install these systemd units? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    repo_path = str(HERE.resolve())
    home_path = str(Path.home())
    wrote: list[str] = []
    for src_name, install_name, is_timer in _PROXY_STACK_UNITS:
        dest = Path("/etc/systemd/system") / install_name
        if dest.exists():
            print(f"  · {install_name} already installed -- leaving as-is")
            continue
        src = src_dir / src_name
        if not src.exists():
            print(f"  [!!] {src} missing -- skipping")
            continue
        content = src.read_text(encoding="utf-8")
        content = content.replace("__REPO_PATH__", repo_path)
        content = content.replace("__HOME__", home_path)
        try:
            dest.write_text(content, encoding="utf-8")
        except OSError as e:
            print(f"  [!!] Failed to write {dest}: {e}")
            continue
        wrote.append(install_name)
        print(f"  + wrote {install_name}")

    if not wrote:
        return

    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    for name in wrote:
        rc = subprocess.run(
            ["systemctl", "enable", "--now", name],
            capture_output=True, text=True,
        )
        if rc.returncode == 0:
            print(f"  · enabled + started {name}")
        else:
            print(f"  [!!] {name} enable failed:\n{rc.stderr.strip()[-300:]}")


def _set_settings_env_vars(
    settings_path: Path, vars_to_set: dict, *,
    dry_run: bool = False, _print: bool = True,
) -> None:
    """Idempotently merge ``vars_to_set`` into the ``env`` block of
    ``~/.claude/settings.json`` (creating both file and section as
    needed). Backs up the prior file if one exists.

    Used by the proxy-orchestrator to write ``ANTHROPIC_BASE_URL``
    after the user picks local-or-remote proxy mode.
    """
    if dry_run:
        if _print:
            print(f"  [dry-run] would set in {settings_path}:")
            for k, v in vars_to_set.items():
                print(f"    {k}={v}")
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_json(settings_path) if settings_path.exists() else {}
    if settings_path.exists():
        try:
            shutil.copy(settings_path, backup_path(settings_path))
        except OSError as e:
            if _print:
                print(f"  [!!] Could not back up {settings_path}: {e}")
            return
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        settings["env"] = env = {}
    changed = False
    for k, v in vars_to_set.items():
        if env.get(k) != v:
            env[k] = v
            changed = True
    if not changed:
        if _print:
            print(f"  · {settings_path}: env already set, no change")
        return
    _save_json(settings_path, settings)
    if _print:
        print(f"  + updated {settings_path}: " +
              ", ".join(f"{k}={v}" for k, v in vars_to_set.items()))


def _setup_update_check(cfg: dict, *, non_interactive: bool) -> None:
    """Ask the user whether to enable the daily release-check.

    Writes ``cfg["update_check"]["enabled"]`` in-place. The check
    itself runs on the long-lived ``claude-hooks-daemon``, so we
    warn the user when the daemon is disabled — without it the
    feature can't poll.
    """
    uc_cfg = cfg.setdefault("update_check", {})
    if non_interactive:
        # Default-off in non-interactive runs to preserve current behaviour.
        uc_cfg.setdefault("enabled", False)
        return

    print("\n==> Self-update check")
    current = uc_cfg.get("enabled", False)
    default_hint = "Y/n" if current else "y/N"
    ans = input(
        f"  Do you want to automatically check every 24 hours for a new "
        f"release? [{default_hint}]: "
    ).strip().lower()

    if ans in ("y", "yes"):
        enabled = True
    elif ans in ("n", "no"):
        enabled = False
    else:
        # Empty input = keep current value.
        enabled = bool(current)

    uc_cfg["enabled"] = enabled
    if enabled:
        print(
            "  Update check ENABLED. Polls "
            f"{uc_cfg.get('github_repo', 'mann1x/claude-hooks')} once per 24h. "
            "Disable at runtime: set update_check.enabled to false in "
            "config/claude-hooks.json."
        )
        daemon_cfg = (cfg.get("hooks") or {}).get("daemon") or {}
        if not daemon_cfg.get("enabled", True):
            print(
                "  WARNING: hooks.daemon.enabled is false. The update "
                "check runs on the daemon thread; without the daemon "
                "running, no checks will fire and the Stop hook will "
                "never surface a notice. Re-enable the daemon to use "
                "this feature."
            )
    else:
        print("  Update check disabled.")


def _setup_proxy_orchestrator(
    cfg: dict, settings_path: Path, *,
    non_interactive: bool, dry_run: bool,
) -> None:
    """Top-level proxy decision: do you want the API proxy at all?
    If yes, install locally on this host or point at an existing
    proxy already running on the network?

    Mutates ``cfg["proxy"]["enabled"]`` in-place; the per-OS install
    helpers downstream see the updated value. For "remote" mode it
    also writes ``ANTHROPIC_BASE_URL`` directly into settings.json so
    Claude Code routes through the existing proxy without a local
    service install.

    Non-interactive mode preserves the existing config -- nothing is
    asked or changed.
    """
    if non_interactive:
        return

    print("\n==> claude-hooks API proxy")
    print(
        "    Optional local HTTP proxy in front of api.anthropic.com.\n"
        "    Adds: real weekly-limit %, Warmup token-drain block,\n"
        "          rate-limit header capture, structured request logs.\n"
        "    Can be installed locally on this host, or you can point\n"
        "    this host at an existing proxy already running on the LAN.\n"
        "    See docs/proxy.md."
    )

    proxy_cfg = cfg.setdefault("proxy", {})
    currently_enabled = bool(proxy_cfg.get("enabled", False))

    ans = input(
        f"  Use the API proxy? (current: {'yes' if currently_enabled else 'no'}) [y/N]: "
    ).strip().lower()
    if ans not in ("y", "yes"):
        # Don't flip an already-true value to false silently -- if the
        # user has it on, they probably want to keep it. Only set the
        # default when it wasn't already enabled.
        if not currently_enabled:
            proxy_cfg["enabled"] = False
        return

    print("\n  Two options:")
    print("    [1] Install the proxy on THIS host.")
    print("        Service runs locally; Claude Code points at 127.0.0.1.")
    print("    [2] Use an existing proxy already on the network.")
    print("        Skip local install -- just set ANTHROPIC_BASE_URL on")
    print("        this host to the URL you supply.")
    while True:
        choice = input("  Choose [1/2] (default 1): ").strip() or "1"
        if choice in ("1", "2"):
            break
        print("  Please enter 1 or 2.")

    if choice == "1":
        proxy_cfg["enabled"] = True
        print("  · proxy.enabled = true; per-OS service installer will run.")
        host = proxy_cfg.get("listen_host", "127.0.0.1")
        port = proxy_cfg.get("listen_port", 38080)
        advertise = "127.0.0.1" if host == "0.0.0.0" else host
        url = f"http://{advertise}:{port}"
        ans = input(
            f"  Also set ANTHROPIC_BASE_URL={url} in {settings_path} now? [Y/n]: "
        ).strip().lower()
        if ans in ("", "y", "yes"):
            _set_settings_env_vars(
                settings_path, {"ANTHROPIC_BASE_URL": url}, dry_run=dry_run,
            )
        else:
            print(f"  · Set it manually later: {url}")
    else:
        # Remote -- don't install locally.
        proxy_cfg["enabled"] = False
        while True:
            url = input(
                "  URL of the existing proxy (e.g. http://192.168.178.2:38080): "
            ).strip().rstrip("/")
            if url.startswith(("http://", "https://")):
                break
            print("  Please enter a URL beginning with http:// or https://")
        _set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": url}, dry_run=dry_run,
        )
        print(f"  · Local proxy NOT installed. ANTHROPIC_BASE_URL={url}")


_PROXY_LAUNCHD_LABEL = "com.claude-hooks.proxy"
_PROXY_LAUNCHD_FILENAME = f"{_PROXY_LAUNCHD_LABEL}.plist"

# Mirrors _LAUNCHD_PLIST for the daemon, but points at the proxy entry
# point. KeepAlive=true so launchd respawns it if it crashes; logs land
# next to the daemon's so `claude-hooks-daemon-ctl tail` style commands
# can find them by convention.
_LAUNCHD_PROXY_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>__LABEL__</string>
  <key>ProgramArguments</key>
  <array>
    <string>__REPO_PATH__/bin/claude-hooks-proxy</string>
  </array>
  <key>WorkingDirectory</key><string>__REPO_PATH__</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>__HOME__/.claude/claude-hooks-proxy.log</string>
  <key>StandardErrorPath</key><string>__HOME__/.claude/claude-hooks-proxy.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAUDE_HOOKS_REPO</key><string>__REPO_PATH__</string>
  </dict>
</dict>
</plist>
"""


def _install_proxy_launchd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the proxy as a macOS LaunchAgent when ``proxy.enabled``
    is true. macOS-only; idempotent.

    Outer entry only checks the OS gate; the actual work lives in
    ``_install_proxy_launchd_steps`` so tests can drive the install
    flow on non-macOS hosts.
    """
    if sys.platform != "darwin":
        return
    _install_proxy_launchd_steps(
        cfg, non_interactive=non_interactive, dry_run=dry_run,
    )


def _install_proxy_launchd_steps(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """The body of ``_install_proxy_launchd`` -- assumes we're already
    on macOS. Mirrors ``_install_daemon_launchd`` but for the proxy.
    Loads immediately via ``launchctl load -w`` so the proxy is
    responding before the installer returns.
    """
    proxy_cfg = (cfg.get("proxy") or {})
    if not proxy_cfg.get("enabled", False):
        return

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    dest = plist_dir / _PROXY_LAUNCHD_FILENAME

    print("\n==> Proxy launchd agent (macOS)")
    if dest.exists():
        print(f"  · {dest.name} already installed -- leaving as-is")
        return
    if dry_run:
        print(f"  [dry-run] would write {dest}")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input(
            f"  Install proxy LaunchAgent at {dest}? [Y/n]: "
        ).strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    plist_dir.mkdir(parents=True, exist_ok=True)
    content = (
        _LAUNCHD_PROXY_PLIST
        .replace("__LABEL__", _PROXY_LAUNCHD_LABEL)
        .replace("__REPO_PATH__", str(HERE.resolve()))
        .replace("__HOME__", str(Path.home()))
    )
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {dest}")
    rc = subprocess.run(
        ["launchctl", "load", "-w", str(dest)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  [!!] launchctl load failed:\n{rc.stderr.strip()[-300:]}")
        return
    print(f"  · loaded {_PROXY_LAUNCHD_LABEL} into launchd")
    _print_proxy_post_install_hint(proxy_cfg)


_PROXY_TASK_NAME = "claude-hooks-proxy"

# Mirrors _DAEMON_TASK_XML for the daemon. The proxy runs forever, so
# ExecutionTimeLimit=PT0S; MultipleInstancesPolicy=IgnoreNew because
# port-bind would fail anyway; StartWhenAvailable=true catches the
# logon-trigger miss case.
_PROXY_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>claude-hooks-proxy -- local HTTP proxy in front of api.anthropic.com</Description>
    <Author>claude-hooks installer</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _write_proxy_task_xml(
    *, command: str, arguments: str, workdir: str,
) -> Path:
    """Render the proxy task XML to a UTF-16 temp file and return its
    path. Caller is responsible for cleanup.
    """
    user_id = _windows_user_id()
    xml = _PROXY_TASK_XML.format(
        user_id=_xml_escape(user_id),
        command=_xml_escape(command),
        arguments=_xml_escape(arguments),
        workdir=_xml_escape(workdir),
    )
    fd, path = tempfile.mkstemp(prefix="claude-hooks-proxy-", suffix=".xml")
    os.close(fd)
    p = Path(path)
    # schtasks /XML wants UTF-16
    p.write_bytes(xml.encode("utf-16"))
    return p


def _install_proxy_windows(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Register the proxy as a Windows logon-triggered scheduled task
    when ``proxy.enabled`` is true. Windows-only; idempotent.

    Outer entry only checks the OS gate; the actual work lives in
    ``_install_proxy_windows_steps`` so tests can drive the install
    flow on Linux without globally patching ``os.name`` (which would
    poison ``pathlib``'s class selector).
    """
    if os.name != "nt":
        return
    _install_proxy_windows_steps(
        cfg, non_interactive=non_interactive, dry_run=dry_run,
    )


def _install_proxy_windows_steps(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """The body of ``_install_proxy_windows`` -- assumes we're already
    on Windows. Mirrors ``_install_daemon_windows_steps`` (UAC-elevated
    /Create + /Run + verify). The proxy is launched via pythonw.exe +
    run_proxy.py to avoid a permanent cmd.exe console window.
    """
    proxy_cfg = (cfg.get("proxy") or {})
    if not proxy_cfg.get("enabled", False):
        return

    print("\n==> Proxy scheduled task (Windows)")
    if dry_run:
        print("  [dry-run] would register schtasks task "
              f"{_PROXY_TASK_NAME}")
        return

    task_name = _PROXY_TASK_NAME
    runner = (HERE / "run_proxy.py").resolve()
    workdir = str(HERE.resolve())

    pyw = find_conda_env_pythonw()
    if pyw is not None:
        exec_command = str(pyw)
        exec_arguments = f'"{runner}"'
    else:
        cmd_path = (HERE / "bin" / "claude-hooks-proxy.cmd").resolve()
        print(
            "  [!] pythonw.exe not found -- falling back to the .cmd shim. "
            "A console window will be visible while the proxy runs."
        )
        exec_command = str(cmd_path)
        exec_arguments = ""

    if _windows_task_exists(task_name):
        if non_interactive:
            ans = "n"
        else:
            ans = input(
                f"  Task {task_name} already exists. Re-install? [y/N]: "
            ).strip().lower()
        if ans not in ("y", "yes"):
            print(f"  · leaving {task_name} as-is")
            _print_proxy_post_install_hint(proxy_cfg)
            return
        # Delete first so the /Create below wins cleanly.
        if not _run_schtasks_elevated(
            f'/Delete /TN "{task_name}" /F',
            ["/Delete", "/TN", task_name, "/F"],
        ):
            print("  [!!] /Delete failed -- aborting reinstall")
            return

    if not non_interactive:
        ans = input(
            f"  Install scheduled task {task_name}? "
            "(one UAC prompt for /Create and /Run) [Y/n]: "
        ).strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    xml_path = _write_proxy_task_xml(
        command=exec_command, arguments=exec_arguments, workdir=workdir,
    )
    try:
        ok = _run_schtasks_elevated(
            f'/Create /XML "{xml_path}" /TN "{task_name}" /F',
            ["/Create", "/XML", str(xml_path), "/TN", task_name, "/F"],
        )
        if not ok:
            print("  [!!] /Create failed -- skipping /Run")
            return
        print(f"  · registered {task_name}")
        ok = _run_schtasks_elevated(
            f'/Run /TN "{task_name}"',
            ["/Run", "/TN", task_name],
        )
        if ok:
            print(f"  · started {task_name}")
        else:
            print(f"  [!!] /Run failed -- task is registered but not started; "
                  f"use `schtasks /Run /TN {task_name}` manually")
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass

    _print_proxy_post_install_hint(proxy_cfg)


def _print_proxy_post_install_hint(proxy_cfg: dict) -> None:
    """Remind the user that the proxy is only useful once Claude Code
    points at it via ``ANTHROPIC_BASE_URL``. Same message regardless of
    OS so installs end with a consistent pointer.
    """
    host = proxy_cfg.get("listen_host", "127.0.0.1")
    port = proxy_cfg.get("listen_port", 38080)
    # ``0.0.0.0`` means "all interfaces" -- the *client* should use a
    # routable address (loopback for same-host, LAN IP for shared).
    advertise_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(
        "\n  Next step: route Claude Code through the proxy by setting\n"
        f'      "env": {{"ANTHROPIC_BASE_URL": "http://{advertise_host}:{port}"}}\n'
        "  in ~/.claude/settings.json on every host that should use it.\n"
        "  See docs/proxy.md for the full setup."
    )


_CALIBER_PROXY_UNIT = "caliber-grounding-proxy.service"


def _install_caliber_proxy_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the caliber-grounding-proxy systemd unit when
    ``caliber_proxy.enabled`` is true in config. Linux-only;
    idempotent -- skips if the unit is already installed.

    The proxy binds 127.0.0.1:38090 by default and routes caliber's
    OpenAI-compatible calls to a local Ollama upstream, injecting
    project grounding and tools along the way.
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    proxy_cfg = (cfg.get("caliber_proxy") or {})
    if not proxy_cfg.get("enabled", False):
        return

    src = HERE / "systemd" / _CALIBER_PROXY_UNIT
    dest = Path("/etc/systemd/system") / _CALIBER_PROXY_UNIT
    if dest.exists():
        return  # idempotent -- leave existing unit alone

    print("\n==> caliber-grounding-proxy systemd unit")
    if not src.exists():
        print(f"  [!!] {src} missing -- skipping")
        return
    print(f"  Will install to {dest} with __REPO_PATH__ = {HERE}")
    if dry_run:
        print("  [dry-run] skipping write.")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input("  Install caliber-grounding-proxy unit? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    content = src.read_text(encoding="utf-8")
    content = content.replace("__REPO_PATH__", str(HERE.resolve()))
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {_CALIBER_PROXY_UNIT}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    rc = subprocess.run(
        ["systemctl", "enable", "--now", _CALIBER_PROXY_UNIT],
        capture_output=True, text=True,
    )
    if rc.returncode == 0:
        print(f"  · enabled + started {_CALIBER_PROXY_UNIT}")
    else:
        print(f"  [!!] enable failed:\n{rc.stderr.strip()[-300:]}")


_PGVECTOR_MCP_UNIT = "claude-hooks-pgvector-mcp.service"


def _install_pgvector_mcp_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the pgvector MCP HTTP-frontend systemd unit when the
    pgvector provider is enabled. Linux-only; idempotent -- skips if
    the unit is already installed.

    The HTTP server binds 0.0.0.0:32775 and exposes the same JSON-RPC
    surface as the stdio launcher (``bin/claude-hook-pgvector-mcp``)
    so Claude Desktop and any other remote MCP client can reach the
    pgvector store at ``http://<host>:32775/mcp``. Stdio remains the
    default for local Claude Code sessions; this unit only adds the
    HTTP frontend on top, it does not replace the stdio launcher.
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    pgvector_cfg = (cfg.get("providers") or {}).get("pgvector") or {}
    if not pgvector_cfg.get("enabled", False):
        return

    src = HERE / "systemd" / _PGVECTOR_MCP_UNIT
    dest = Path("/etc/systemd/system") / _PGVECTOR_MCP_UNIT
    if dest.exists():
        return  # idempotent -- leave existing unit alone

    print("\n==> claude-hooks-pgvector-mcp systemd unit")
    if not src.exists():
        print(f"  [!!] {src} missing -- skipping")
        return
    print(f"  Will install to {dest} with __REPO_PATH__ = {HERE}")
    if dry_run:
        print("  [dry-run] skipping write.")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input(
            "  Install pgvector MCP HTTP unit (port 32775)? [Y/n]: ",
        ).strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    content = src.read_text(encoding="utf-8")
    content = content.replace("__REPO_PATH__", str(HERE.resolve()))
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {_PGVECTOR_MCP_UNIT}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    rc = subprocess.run(
        ["systemctl", "enable", "--now", _PGVECTOR_MCP_UNIT],
        capture_output=True, text=True,
    )
    if rc.returncode == 0:
        print(f"  · enabled + started {_PGVECTOR_MCP_UNIT}")
        print("    URL: http://<host>:32775/mcp  (Streamable-HTTP MCP)")
    else:
        print(f"  [!!] enable failed:\n{rc.stderr.strip()[-300:]}")


_AXON_HOST_UNIT = "axon-host.service"
_AXON_HOST_CWD = Path("/var/lib/axon-host")
_AXON_PLACEHOLDER = (
    "# Placeholder. axon host indexes its cwd at startup and crashes\n"
    "# when the cwd has no parseable files (ThreadPoolExecutor(\n"
    "# max_workers=0) upstream bug). This single tiny module satisfies\n"
    "# the parser without adding meaningful content. Do not delete.\n"
    "EMPTY = None\n"
)


def _install_axon_host_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the axon shared-host systemd unit when
    ``companions.axon_host.enabled`` is true. Linux-only; idempotent.

    The axon shared host runs at http://127.0.0.1:8420/mcp and serves
    every repo registered under ~/.axon/repos/ to all Claude Code
    sessions over a single HTTP MCP endpoint - replacing the legacy
    ``axon serve --watch`` per-session stdio MCP, which auto-indexes
    whatever cwd Claude Code launched in (and on 2026-04-27 burned
    64 GB of resident memory trying to index a model directory).

    The unit ships with three workarounds for upstream quirks:
      * ``WorkingDirectory=/var/lib/axon-host`` so axon can't scan the
        user's home or another real tree.
      * A 1-line ``_placeholder.py`` dropped in that cwd, because an
        empty cwd trips ``ThreadPoolExecutor(max_workers=0)`` in the
        import resolver.
      * ``MemoryMax=8G`` / ``MemoryHigh=6G`` so a future runaway can
        not OOM the host.
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    companions_cfg = (cfg.get("companions") or cfg.get("hooks", {}).get("companions") or {})
    axon_host_cfg = (companions_cfg.get("axon_host") or {})
    if not axon_host_cfg.get("enabled", False):
        return

    axon_bin = shutil.which("axon")
    if axon_bin is None:
        # Fall back to the conda env claude-hooks itself uses, mirroring
        # the bin/claude-hook shim's lookup.
        candidate = Path.home() / "anaconda3" / "envs" / "claude-hooks" / "bin" / "axon"
        if candidate.is_file():
            axon_bin = str(candidate)
    if axon_bin is None:
        print("\n==> axon-host systemd unit")
        print("  axon binary not found on PATH or in ~/anaconda3/envs/claude-hooks/bin/")
        print("  Skipped. Run `pip install axoniq` and re-run install.py.")
        return

    src = HERE / "systemd" / _AXON_HOST_UNIT
    dest = Path("/etc/systemd/system") / _AXON_HOST_UNIT
    if dest.exists():
        return  # idempotent

    print("\n==> axon-host systemd unit")
    if not src.exists():
        print(f"  [!!] {src} missing -- skipping")
        return
    print(f"  Will install to {dest} with __AXON_BIN__ = {axon_bin}")
    print(f"  Will create cwd {_AXON_HOST_CWD} with placeholder file")
    if dry_run:
        print("  [dry-run] skipping write.")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input("  Install axon-host unit? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    try:
        _AXON_HOST_CWD.mkdir(parents=True, exist_ok=True)
        (_AXON_HOST_CWD / "_placeholder.py").write_text(
            _AXON_PLACEHOLDER, encoding="utf-8",
        )
    except OSError as e:
        print(f"  [!!] Failed to prepare {_AXON_HOST_CWD}: {e}")
        return

    axon_bin_dir = str(Path(axon_bin).parent)
    content = src.read_text(encoding="utf-8")
    content = content.replace("__AXON_BIN__", axon_bin)
    content = content.replace("__AXON_BIN_DIR__", axon_bin_dir)
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {_AXON_HOST_UNIT}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    rc = subprocess.run(
        ["systemctl", "enable", "--now", _AXON_HOST_UNIT],
        capture_output=True, text=True,
    )
    if rc.returncode == 0:
        print(f"  · enabled + started {_AXON_HOST_UNIT}")
        print("  · point ~/.claude.json axon MCP entry at:")
        print('      {"axon": {"type":"http","url":"http://127.0.0.1:8420/mcp"}}')
    else:
        print(f"  [!!] enable failed:\n{rc.stderr.strip()[-300:]}")


_DAEMON_UNIT = "claude-hooks-daemon.service"


def _install_claude_hooks_daemon(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the long-lived hook executor (Tier 3.8 latency reduction).

    The daemon owns the Python interpreter, providers, HyDE cache, and
    other per-process state across hook invocations -- saves ~150-300 ms
    per hook compared with the per-invocation interpreter spawn the
    bin/claude-hook shim does without it.

    Cross-platform:

    - **Linux (systemd)**: writes ``claude-hooks-daemon.service`` with
      ``__REPO_PATH__`` / ``__HOME__`` substituted, then ``systemctl
      enable --now``.

    - **macOS (launchd)**: writes ``~/Library/LaunchAgents/
      com.claude-hooks.daemon.plist`` and ``launchctl load``.

    - **Windows**: prints the ``schtasks`` command the user can run
      to register the daemon as a logon-triggered scheduled task. We
      don't run it automatically because it needs an elevated prompt.

    The daemon itself is OPTIONAL -- installs that skip this step still
    work because the client falls back to in-process dispatch when the
    daemon isn't running. So this prompt always defaults to "yes" but
    a "no" is harmless.
    """
    cfg_section = (cfg.get("hooks") or {}).get("daemon") or {}
    if cfg_section.get("enabled") is False:
        # Explicit opt-out via config -- respect it without prompting.
        return

    print("\n==> claude-hooks-daemon (long-lived hook executor)")
    print("    Owns providers, HyDE cache, and Python interpreter across")
    print("    hook calls -- saves ~150-300 ms per hook.")
    print("    OPTIONAL: hooks fall back to in-process dispatch when the")
    print("    daemon isn't running, so skipping this is safe.")

    if dry_run:
        print("  [dry-run] skipping daemon install.")
        return

    # Detect existing autostart entry BEFORE the install prompt so we
    # can show the right question. Without this, a re-run of install.py
    # always asks "Install + enable...?" even when the task / unit / plist
    # is already in place -- confusing because the install has already
    # happened.
    already = _detect_existing_daemon_entry()
    if already:
        print(f"  Already installed: {already}")
        if non_interactive:
            print("  --non-interactive: leaving as-is and verifying the daemon.")
            _verify_and_start_daemon()
            return
        ans = input(
            "  Re-install (delete + recreate + verify), verify-only, "
            "or skip? [r/V/s]: "
        ).strip().lower()
        if ans in ("s", "skip", "n", "no"):
            print("  Skipped.")
            return
        if ans in ("r", "reinstall", "re-install"):
            # Fall through to platform installer; its already-exists
            # branch will do the delete + recreate + verify.
            pass
        else:
            # Default ("", "v", "verify") -> ping; start if not running.
            _verify_and_start_daemon()
            return
    else:
        if not non_interactive:
            ans = input(
                "  Install + enable claude-hooks-daemon? [Y/n]: "
            ).strip().lower()
            if ans not in ("", "y", "yes"):
                print("  Skipped.")
                return

    force_reinstall = bool(already)

    if os.name == "nt":
        _install_daemon_windows(
            non_interactive=non_interactive,
            force_reinstall=force_reinstall,
        )
        return

    # POSIX path: try systemd first, then launchd.
    if Path("/etc/systemd/system").is_dir():
        _install_daemon_systemd(
            non_interactive=non_interactive,
            force_reinstall=force_reinstall,
        )
        return
    if sys.platform == "darwin":
        _install_daemon_launchd(
            non_interactive=non_interactive,
            force_reinstall=force_reinstall,
        )
        return
    print("  [!!] No supported autostart manager (systemd / launchd) detected.")
    print("       Run manually:  bin/claude-hooks-daemon")


def _detect_existing_daemon_entry() -> Optional[str]:
    """Return a human-readable description of the existing daemon
    autostart entry, or None when nothing is installed yet."""
    if os.name == "nt":
        if _windows_task_exists(_DAEMON_TASK_NAME):
            return f"Windows scheduled task '{_DAEMON_TASK_NAME}'"
        return None
    # systemd unit file is the strongest signal on Linux -- even if the
    # service is currently stopped, the autostart is "installed".
    if (Path("/etc/systemd/system") / _DAEMON_UNIT).exists():
        return f"systemd unit /etc/systemd/system/{_DAEMON_UNIT}"
    plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-hooks.daemon.plist"
    if plist.exists():
        return f"launchd plist {plist}"
    return None


def _start_daemon_via_platform() -> None:
    """Trigger the daemon via the platform's autostart manager.

    Used by the verify-only path when the autostart entry exists but
    the daemon isn't currently listening -- e.g. the task is registered
    but hasn't fired since the last logon, or the systemd service was
    manually stopped. Best-effort: failures are reported, never raised.
    """
    if os.name == "nt":
        run_argstr = f'/Run /TN "{_DAEMON_TASK_NAME}"'
        run_argv = ["/Run", "/TN", _DAEMON_TASK_NAME]
        _run_schtasks_elevated(run_argstr, run_argv)
        return

    # systemd: start the service.
    if (Path("/etc/systemd/system") / _DAEMON_UNIT).exists():
        rc = subprocess.run(
            ["systemctl", "start", _DAEMON_UNIT],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            print(
                f"  [!!] systemctl start {_DAEMON_UNIT} failed: "
                f"{rc.stderr.strip()[-200:]}"
            )
        return

    # launchd: kickstart the agent.
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
            ["launchctl", "kickstart", "-k",
             f"gui/{uid}/com.claude-hooks.daemon"],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            print(
                f"  [!!] launchctl kickstart failed: "
                f"{rc.stderr.strip()[-200:]}"
            )


def _verify_and_start_daemon() -> bool:
    """Ping the daemon; if it isn't running, try to start it via the
    platform manager and ping again. Returns True iff the daemon ends
    up responding. Prints progress so the user can see what happened.
    """
    print("  Verifying the daemon is responding...")
    if _wait_for_daemon(timeout=5.0):
        print("  · daemon responding on 127.0.0.1:47018")
        return True

    print("  · daemon not running -- attempting to start it...")
    _start_daemon_via_platform()
    if _wait_for_daemon(timeout=15.0):
        print("  · daemon started and responding on 127.0.0.1:47018")
        return True

    print(
        "  [!!] daemon still not responding after a start attempt."
    )
    if os.name == "nt":
        print(
            f"       Inspect the task: schtasks /Query /TN "
            f"\"{_DAEMON_TASK_NAME}\" /V /FO LIST"
        )
        print(
            "       Run the daemon directly to see its stderr: "
            f"{(HERE / 'bin' / 'claude-hooks-daemon.cmd').resolve()}"
        )
    elif (Path("/etc/systemd/system") / _DAEMON_UNIT).exists():
        print(f"       systemctl status {_DAEMON_UNIT} -l")
        print(f"       journalctl -u {_DAEMON_UNIT} -e --no-pager")
    else:
        print(f"       launchctl print gui/$(id -u)/com.claude-hooks.daemon")
    return False


def _install_daemon_systemd(
    *, non_interactive: bool = False, force_reinstall: bool = False,
) -> None:
    src = HERE / "systemd" / _DAEMON_UNIT
    dest = Path("/etc/systemd/system") / _DAEMON_UNIT

    if dest.exists():
        if force_reinstall:
            ans = "y"
        elif non_interactive:
            ans = "n"
        else:
            ans = input(
                f"  {_DAEMON_UNIT} already installed. Re-install + re-verify? [y/N]: "
            ).strip().lower()
        if ans in ("y", "yes"):
            subprocess.run(
                ["systemctl", "disable", "--now", _DAEMON_UNIT],
                capture_output=True,
            )
            try:
                dest.unlink()
            except OSError as e:
                print(f"  [!!] could not remove {dest}: {e} -- leaving as-is")
                return
        else:
            print(f"  · leaving {_DAEMON_UNIT} as-is -- verifying it's responding")
            if _wait_for_daemon(timeout=5.0):
                print(f"  · daemon responding on 127.0.0.1:47018")
            else:
                print(
                    "  [!!] daemon not responding. Try: "
                    f"systemctl restart {_DAEMON_UNIT}"
                )
            return

    if not src.exists():
        print(f"  [!!] {src} missing -- skipping")
        return
    repo_path = str(HERE.resolve())
    home_path = str(Path.home())
    content = src.read_text(encoding="utf-8")
    content = content.replace("__REPO_PATH__", repo_path)
    content = content.replace("__HOME__", home_path)
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {_DAEMON_UNIT}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    rc = subprocess.run(
        ["systemctl", "enable", "--now", _DAEMON_UNIT],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  [!!] enable failed:\n{rc.stderr.strip()[-300:]}")
        return
    print(f"  · enabled + started {_DAEMON_UNIT}")
    if _wait_for_daemon():
        print("  · daemon responding on 127.0.0.1:47018")
    else:
        print(
            "  [!!] daemon not responding within 15 s. Check: "
            f"systemctl status {_DAEMON_UNIT}"
        )


_LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-hooks.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>__REPO_PATH__/bin/claude-hooks-daemon</string>
  </array>
  <key>WorkingDirectory</key><string>__REPO_PATH__</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>__HOME__/.claude/claude-hooks-daemon.log</string>
  <key>StandardErrorPath</key><string>__HOME__/.claude/claude-hooks-daemon.log</string>
</dict>
</plist>
"""


def _install_daemon_launchd(
    *, non_interactive: bool = False, force_reinstall: bool = False,
) -> None:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    dest = plist_dir / "com.claude-hooks.daemon.plist"

    if dest.exists():
        if force_reinstall:
            ans = "y"
        elif non_interactive:
            ans = "n"
        else:
            ans = input(
                f"  {dest.name} already installed. Re-install + re-verify? [y/N]: "
            ).strip().lower()
        if ans in ("y", "yes"):
            subprocess.run(
                ["launchctl", "unload", "-w", str(dest)],
                capture_output=True,
            )
            try:
                dest.unlink()
            except OSError as e:
                print(f"  [!!] could not remove {dest}: {e} -- leaving as-is")
                return
        else:
            print(f"  · leaving {dest.name} as-is -- verifying daemon")
            if _wait_for_daemon(timeout=5.0):
                print("  · daemon responding on 127.0.0.1:47018")
            else:
                print(
                    "  [!!] daemon not responding. Try: launchctl kickstart "
                    "-k gui/$(id -u)/com.claude-hooks.daemon"
                )
            return

    content = _LAUNCHD_PLIST
    content = content.replace("__REPO_PATH__", str(HERE.resolve()))
    content = content.replace("__HOME__", str(Path.home()))
    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"  [!!] Failed to write {dest}: {e}")
        return
    print(f"  + wrote {dest}")
    rc = subprocess.run(
        ["launchctl", "load", "-w", str(dest)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  [!!] launchctl load failed:\n{rc.stderr.strip()[-300:]}")
        return
    print("  · loaded into launchd")
    if _wait_for_daemon():
        print("  · daemon responding on 127.0.0.1:47018")
    else:
        print("  [!!] daemon not responding within 15 s.")


_DAEMON_TASK_NAME = "claude-hooks-daemon"

# Windows scheduled-task XML. Imported via ``schtasks /Create /XML`` so we
# can override defaults that the CLI form can't reach:
#
#   * ExecutionTimeLimit = PT0S -- the default 72 h cap stops a long-lived
#     daemon mid-session. PT0S means "no limit".
#   * DisallowStartIfOnBatteries / StopIfGoingOnBatteries = false -- laptops
#     should keep the daemon running on battery; the user explicitly asked
#     for this.
#   * StartWhenAvailable = true -- if the user wasn't logged in at logon
#     time, fire as soon as we are.
#   * MultipleInstancesPolicy = IgnoreNew -- duplicate /Run requests don't
#     spawn a second daemon (port-bind would fail anyway).
#
# UTF-16 encoded on disk because that's what schtasks /XML expects.
_DAEMON_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>claude-hooks-daemon -- long-lived hook executor (Tier 3.8)</Description>
    <Author>claude-hooks installer</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user_id() -> str:
    """Return the current user's SID (or DOMAIN\\username fallback).

    The Task XML's ``<UserId>`` field accepts either form, but a SID is
    the only one that's reliably valid in every case. ``USERDOMAIN`` is
    set to ``"WORKGROUP"`` on non-domain-joined machines, and schtasks
    /XML rejects ``WORKGROUP\\<user>`` with::

        ERROR: No mapping between account names and security IDs was done.

    so we can't trust the env-var form. Try ``whoami /user`` first to
    get the SID; fall back to ``COMPUTERNAME\\username`` for local
    accounts (always valid because the local computer IS the principal
    authority for its accounts), then to the bare username.
    """
    try:
        rc = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=5,
        )
        if rc.returncode == 0:
            # Output: "DOMAIN\user","S-1-5-21-..."
            line = rc.stdout.strip().splitlines()[-1] if rc.stdout.strip() else ""
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2 and parts[1].startswith("S-"):
                return parts[1]
    except (OSError, subprocess.SubprocessError):
        pass
    user = os.environ.get("USERNAME") or ""
    if not user:
        return "."
    # Local-account form: prefer COMPUTERNAME (always valid for local
    # users) over USERDOMAIN (== "WORKGROUP" on non-domain machines).
    computer = os.environ.get("COMPUTERNAME") or ""
    if computer and computer.upper() != "WORKGROUP":
        return f"{computer}\\{user}"
    return user


def _xml_escape(s: str) -> str:
    """Escape characters that would break the XML we write to disk."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def _write_daemon_task_xml(
    *, command: str, arguments: str, workdir: str,
) -> Path:
    """Write the task XML to a temp file and return its path.

    Writes UTF-16 LE with BOM (Python's default for "utf-16") because
    that's what schtasks /XML expects on Windows. Caller is responsible
    for cleaning the file up after schtasks consumes it.
    """
    xml = _DAEMON_TASK_XML.format(
        user_id=_xml_escape(_windows_user_id()),
        command=_xml_escape(command),
        arguments=_xml_escape(arguments),
        workdir=_xml_escape(workdir),
    )
    import tempfile  # noqa: PLC0415 -- Windows-only path
    fd, path = tempfile.mkstemp(prefix="claude-hooks-daemon-", suffix=".xml")
    os.close(fd)
    Path(path).write_bytes(xml.encode("utf-16"))
    return Path(path)


def _wait_for_daemon(*, timeout: float = 15.0) -> bool:
    """Poll the daemon until ping succeeds or the deadline elapses.

    Used by every platform's daemon-install path to confirm the
    autostart entry actually launched the daemon. Returns True on
    first successful ping, False on timeout. Never raises.

    The first ping after install can take a few seconds because:
      - systemd / launchd may delay the spawn behind dependencies
      - Windows Task Scheduler /Run is async
      - the daemon's first action is ``ensure_secret`` which creates
        ``~/.claude/claude-hooks-daemon-secret`` -- only after that
        does it bind the listener
    """
    try:
        from claude_hooks.daemon_client import ping  # noqa: PLC0415
    except ImportError:
        return False
    import time as _time  # noqa: PLC0415
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            if ping(timeout=1.0):
                return True
        except Exception:  # pragma: no cover -- defensive
            pass
        _time.sleep(0.5)
    return False


def _is_windows_admin() -> bool:
    """Return True iff the current process has admin rights on Windows."""
    try:
        import ctypes  # noqa: PLC0415 -- Windows-only path
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _windows_task_exists(task_name: str) -> bool:
    """Return True iff ``schtasks /Query /TN <name>`` succeeds."""
    try:
        rc = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True, text=True,
        )
        return rc.returncode == 0
    except OSError:
        return False


def _run_schtasks_elevated(argstr: str, argv: list) -> bool:
    """Invoke ``schtasks <args>`` elevated, returning True on rc=0.

    Direct call when already admin; PowerShell ``Start-Process -Verb
    RunAs -Wait`` (one UAC prompt) otherwise. ``argstr`` is the
    arguments as a single PowerShell-safe string; ``argv`` is the
    pre-tokenised list used in the admin shortcut path.
    """
    if _is_windows_admin():
        try:
            rc = subprocess.run(
                ["schtasks", *argv],
                capture_output=True, text=True,
            )
            if rc.returncode != 0:
                print(f"  [!!] schtasks failed:\n{rc.stderr.strip()[-300:]}")
                return False
            return True
        except OSError as e:
            print(f"  [!!] schtasks invocation failed: {e}")
            return False

    ps_cmd = (
        "Start-Process -FilePath schtasks "
        f"-ArgumentList '{argstr}' "
        "-Verb RunAs -Wait"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True,
        )
    except OSError as e:
        print(f"  [!!] Failed to launch elevated process: {e}")
        return False
    # Start-Process -Wait returns once the elevated child exits but
    # hides its rc -- the caller verifies via /Query (or the calling
    # context's own check, e.g. _wait_for_daemon).
    return True


def _install_daemon_windows(
    *, non_interactive: bool, force_reinstall: bool = False,
) -> None:
    """Register the daemon as a Windows logon-triggered scheduled task,
    start it now, and verify it's responding.

    Three failure / re-entry modes:

    1. Task already exists -- ask whether to delete + reinstall +
       re-verify, or just leave as-is (and verify ping). When the
       outer caller passes ``force_reinstall=True`` (because it
       already collected that decision), skip the inner prompt.
    2. /Create succeeded but daemon didn't come up -- retry /Run + ping.
    3. UAC declined -- retry the elevation or skip.

    Mirrors clink's self-update flow: one UAC prompt per elevated
    operation, installer itself stays unprivileged.
    """
    task_name = _DAEMON_TASK_NAME
    runner = (HERE / "run_daemon.py").resolve()
    workdir = str(HERE.resolve())

    # Prefer pythonw.exe (no console window) over the .cmd shim. Falls
    # back to the .cmd only if pythonw isn't available -- at the cost of
    # a visible cmd window flash, which is the historical behaviour.
    pyw = find_conda_env_pythonw()
    if pyw is not None:
        exec_command = str(pyw)
        exec_arguments = f'"{runner}"'
    else:
        cmd_path = (HERE / "bin" / "claude-hooks-daemon.cmd").resolve()
        print(
            "  [!] pythonw.exe not found -- falling back to the .cmd shim. "
            "A console window will be visible while the daemon runs."
        )
        exec_command = str(cmd_path)
        exec_arguments = ""

    # XML generation is deferred until we're actually about to /Create --
    # see _write_xml_now() inside _install_daemon_windows_inner. The
    # verify-only / declined-skip / already-exists-leave-alone paths
    # don't need XML at all and would otherwise pay the whoami cost.
    return _install_daemon_windows_inner(
        non_interactive=non_interactive,
        force_reinstall=force_reinstall,
        task_name=task_name,
        runner=runner,
        pyw=pyw,
        exec_command=exec_command,
        exec_arguments=exec_arguments,
        workdir=workdir,
    )


def _install_daemon_windows_inner(
    *, non_interactive: bool, force_reinstall: bool,
    task_name: str, runner: Path, pyw: Optional[Path],
    exec_command: str, exec_arguments: str, workdir: str,
) -> None:
    """Body of ``_install_daemon_windows``. The XML is generated lazily
    via ``_ensure_xml`` and cleaned up via ``_cleanup_xml`` so we only
    pay the cost (and the whoami subprocess) on paths that actually
    register a task."""
    xml_holder: dict = {}

    def _ensure_xml() -> Path:
        if "path" not in xml_holder:
            xml_holder["path"] = _write_daemon_task_xml(
                command=exec_command, arguments=exec_arguments, workdir=workdir,
            )
        return xml_holder["path"]

    def _cleanup_xml() -> None:
        p = xml_holder.get("path")
        if p is None:
            return
        try:
            p.unlink()
        except OSError:
            pass

    try:
        return _install_daemon_windows_steps(
            non_interactive=non_interactive,
            force_reinstall=force_reinstall,
            task_name=task_name,
            runner=runner,
            pyw=pyw,
            ensure_xml=_ensure_xml,
        )
    finally:
        _cleanup_xml()


def _install_daemon_windows_steps(
    *, non_interactive: bool, force_reinstall: bool,
    task_name: str, runner: Path, pyw: Optional[Path],
    ensure_xml,
) -> None:
    """The actual install logic, separated so the XML lifetime can be
    managed by the caller (``_install_daemon_windows_inner``)."""
    def _create_argstr_and_argv():
        xml_path = ensure_xml()
        create_argstr = (
            f'/Create /XML "{xml_path}" /TN "{task_name}" /F'
        )
        create_argv = [
            "/Create", "/XML", str(xml_path), "/TN", task_name, "/F",
        ]
        return create_argstr, create_argv

    run_argstr = f'/Run /TN "{task_name}"'
    run_argv = ["/Run", "/TN", task_name]
    delete_argstr = f'/Delete /TN "{task_name}" /F'
    delete_argv = ["/Delete", "/TN", task_name, "/F"]

    # ---------- already-installed branch ----------
    if _windows_task_exists(task_name):
        if force_reinstall:
            ans = "y"
        elif non_interactive:
            print(
                f"  · task '{task_name}' already exists "
                f"(--non-interactive -- leaving as-is)"
            )
            ans = "n"
        else:
            print()
            print(f"  Scheduled task '{task_name}' is already registered.")
            ans = input(
                "  Re-install (delete + recreate + verify)? [y/N]: "
            ).strip().lower()

        if ans in ("y", "yes"):
            print(f"  Deleting existing task '{task_name}'...")
            if not _run_schtasks_elevated(delete_argstr, delete_argv):
                print("  [!!] could not delete existing task -- leaving as-is")
                return
            # Fall through to fresh-install loop.
        else:
            # Just verify the daemon is actually responding.
            print("  Verifying the daemon is responding...")
            if _wait_for_daemon(timeout=5.0):
                print("  · daemon responding on 127.0.0.1:47018")
            else:
                print(
                    "  [!!] task is registered but the daemon is not "
                    "responding. Start it now from an elevated cmd:"
                )
                print(f"  schtasks {run_argstr}")
            return

    # ---------- fresh install (or post-delete) branch ----------
    if non_interactive:
        print("  --non-interactive: cannot prompt for UAC. Run manually from")
        print("  an elevated cmd:")
        create_argstr, _ = _create_argstr_and_argv()
        print(f"  schtasks {create_argstr}")
        print(f"  schtasks {run_argstr}")
        return

    target_for_msg = pyw if pyw is not None else (HERE / "bin" / "claude-hooks-daemon.cmd").resolve()
    while True:
        print()
        print(f"  Will register '{task_name}' as a Windows logon-triggered")
        print(f"  scheduled task pointing at {target_for_msg},")
        if pyw is not None:
            print(f"  with launcher script {runner},")
        print("  start it now, and verify the daemon is responding.")
        print("  Each UAC prompt is scoped to one schtasks call.")
        ans = input("  Proceed? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped. Run manually later from an elevated cmd:")
            create_argstr, _ = _create_argstr_and_argv()
            print(f"  schtasks {create_argstr}")
            print(f"  schtasks {run_argstr}")
            return

        # Step 1: create the task if it doesn't exist yet.
        if not _windows_task_exists(task_name):
            create_argstr, create_argv = _create_argstr_and_argv()
            _run_schtasks_elevated(create_argstr, create_argv)
            if not _windows_task_exists(task_name):
                print(
                    "  [!!] task not detected after /Create -- UAC declined "
                    "or schtasks errored."
                )
                retry = input("  Retry? [Y/n]: ").strip().lower()
                if retry not in ("", "y", "yes"):
                    return
                continue
            print(f"  · task '{task_name}' registered")

        # Step 2: trigger the task now (ONLOGON only fires at next logon
        # otherwise -- and the user wants the daemon up immediately).
        _run_schtasks_elevated(run_argstr, run_argv)

        # Step 3: confirm the daemon is actually answering on its port.
        print("  Waiting for the daemon to come up...")
        if _wait_for_daemon():
            print("  · daemon responding on 127.0.0.1:47018")
            return

        print(
            "  [!!] daemon did not respond within 15 s. The task is "
            "registered but the daemon may have crashed at startup."
        )
        if pyw is not None:
            print(
                f"       Inspect the daemon's stderr by running it directly:"
                f"\n           \"{pyw.parent / 'python.exe'}\" \"{runner}\""
            )
        else:
            print(
                "       Inspect the daemon's stderr by running it directly: "
                f"{(HERE / 'bin' / 'claude-hooks-daemon.cmd').resolve()}"
            )
        retry = input("  Retry /Run + verify? [Y/n]: ").strip().lower()
        if retry not in ("", "y", "yes"):
            return


def _setup_pgvector_mcp(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Ask if pgvector is available and set up the system-wide MCP server.

    "System-wide" means we drop a launcher at ``~/.local/bin/pgvector-mcp``
    (POSIX) or ``%LOCALAPPDATA%/claude-hooks/bin/pgvector-mcp.cmd``
    (Windows) so any MCP-aware client -- Claude Code, Cursor, Codex,
    OpenWebUI, etc. -- can spawn the server independently of the
    claude-hooks repo location. The launcher just execs
    ``python -m claude_hooks.pgvector_mcp`` against the resolved
    interpreter and PYTHONPATH baked in at install time.

    Steps when the user answers "yes":

    1. Probe Postgres + pgvector reachability via the existing
       ``PgvectorProvider.verify`` (uses the DSN already in cfg, or
       prompts for a new one).
    2. Drop the launcher script with the resolved interpreter and
       PYTHONPATH baked in.
    3. Update ``cfg.providers.pgvector.enabled = true`` and the DSN.
    4. Register ``mcpServers.pgvector`` in ``~/.claude.json`` (root
       level -- visible to every project) pointing at the launcher.
    5. Backup the prior ``~/.claude.json`` before mutating.

    Skipped silently when the user answers "no" or in non-interactive
    mode without a DSN already configured. Idempotent -- re-running
    upgrades the launcher in place.
    """
    pcfg = (cfg.get("providers") or {}).get("pgvector") or {}
    existing_dsn = pcfg.get("dsn") or ""

    print("\n--- pgvector ---")
    print("  Optional: persistent memory + KG store backed by Postgres + pgvector.")
    print("  When enabled, claude-hooks installs a system-wide MCP server")
    print("  (`pgvector-mcp`) so other tools (Cursor, Codex, OpenWebUI, ...)")
    print("  can recall/store from the same memory.")

    if non_interactive:
        if not existing_dsn:
            print("  --non-interactive and no DSN configured -> skipping pgvector setup.")
            return
        ans = "y"
        print("  --non-interactive: assuming yes (DSN already in config).")
    else:
        default = "Y" if existing_dsn else "N"
        ans = input(f"  Set up pgvector? [{default}/{'n' if default == 'Y' else 'y'}]: ").strip().lower()
        if not ans:
            ans = default.lower()
        if ans not in ("y", "yes"):
            print("  Skipped.")
            return

    # 1. DSN.
    dsn = existing_dsn
    if not non_interactive:
        prompt_default = f" [{dsn}]" if dsn else " (e.g. postgresql://user:pass@127.0.0.1:5432/memory)"
        new_dsn = input(f"  Postgres DSN{prompt_default}: ").strip()
        if new_dsn:
            dsn = new_dsn
    if not dsn:
        print("  No DSN provided -> skipping pgvector setup.")
        return

    # 2. Verify the DSN reaches a Postgres with the pgvector extension.
    try:
        from claude_hooks.providers.pgvector import PgvectorProvider
        from claude_hooks.providers import ServerCandidate
    except ImportError as e:
        print(f"  Cannot import pgvector provider: {e}")
        return
    candidate = ServerCandidate(server_key="pgvector", url=dsn,
                                source="installer", confidence="manual")
    print("  Probing Postgres + pgvector extension...", end=" ", flush=True)
    ok = PgvectorProvider.verify(candidate)
    print("OK" if ok else "FAILED")
    if not ok:
        print("  Couldn't reach pgvector with that DSN.")
        print("  Fix the DSN and re-run install.py -- leaving pgvector disabled.")
        return

    # 2c. Ensure the embedder model is pulled. Talks to the Ollama
    # instance the embedder is configured against -- derives the base
    # URL from the embedder endpoint, so it works against a local
    # daemon AND a remote / proxied Ollama. Skipped silently when the
    # endpoint isn't an Ollama-shaped URL or when the request fails;
    # the user gets a clear "pull manually" breadcrumb either way.
    embedder_opts = cfg["providers"]["pgvector"].get("embedder_options") or {}
    embed_url = embedder_opts.get("url") or ""
    model = embedder_opts.get("model") or ""
    if model and embed_url:
        base = _ollama_base_from_embed_url(embed_url)
        print(f"  Probing Ollama at {base} for {model}...", end=" ", flush=True)
        present = _ollama_model_present(base, model)
        print("present" if present else "missing")
        if not present:
            if non_interactive:
                do_pull = True
                print(f"  --non-interactive: pulling {model}")
            else:
                ans = input(f"  Pull {model} now via {base}? [Y/n]: ").strip().lower()
                do_pull = ans in ("", "y", "yes")
            if do_pull and not dry_run:
                ok = _ollama_pull(base, model)
                if not ok:
                    print(f"  Pull manually: ollama pull {model}  "
                          f"(or POST {{\"name\":\"{model}\"}} to {base}/api/pull)")
            elif do_pull and dry_run:
                print(f"  [dry-run] Would POST /api/pull {{\"name\":\"{model}\"}} to {base}")
            else:
                print(f"  Skipped. Pull manually: ollama pull {model}")

    # 2b. Probe target tables; create the qwen3 + KG schema if missing.
    # The migration script (scripts/migrate_to_pgvector.py) is the source
    # of truth for the per-model DDL -- we reuse its
    # ``schema_sql_for_model`` so install.py and the migration stay in
    # sync. The shared kg_entities + kg_relations tables aren't in that
    # function (the migration assumes they exist), so we ship their DDL
    # inline here.
    table_name = (cfg.get("providers") or {}).get("pgvector", {}).get("table") or "memories_qwen3"
    if not _pgvector_tables_present(dsn, table_name):
        if non_interactive:
            print(f"  Tables missing -> auto-initializing qwen3 + KG schema...")
            do_init = True
        else:
            ans = input(
                f"  Schema {table_name!r} missing. Initialize qwen3 + KG schema now? [Y/n]: "
            ).strip().lower()
            do_init = ans in ("", "y", "yes")
        if do_init and not dry_run:
            _init_pgvector_schema(dsn)
            print("  Schema initialized.")
        elif do_init and dry_run:
            print("  [dry-run] Would CREATE EXTENSION vector + pg_trgm; "
                  "create kg_entities/kg_relations + memories_qwen3/kg_observations_qwen3.")
        else:
            print("  Skipped schema init. The provider auto-creates a "
                  "single-table fallback on first store(); KG tools will "
                  "fail until you run scripts/migrate_to_pgvector.py.")

    # 3. Drop the launcher script. PYTHONPATH baked in is the repo root
    # (HERE) so the launcher works without a pip install of claude-hooks.
    py_path = find_conda_env_python()
    py = str(py_path) if py_path.exists() else sys.executable
    launcher_path = _pgvector_launcher_path()
    if dry_run:
        print(f"  [dry-run] Would write launcher: {launcher_path}")
        print(f"  [dry-run] Would point ~/.claude.json mcpServers.pgvector -> {launcher_path}")
    else:
        _write_pgvector_launcher(launcher_path, py=py, repo=str(HERE))
        print(f"  Launcher: {launcher_path}")

    # 4. Update claude-hooks config.
    cfg.setdefault("providers", {}).setdefault("pgvector", {})
    cfg["providers"]["pgvector"]["enabled"] = True
    cfg["providers"]["pgvector"]["dsn"] = dsn
    # Preserve user's table / embedder config when present; only fill
    # defaults if nothing is set.
    cfg["providers"]["pgvector"].setdefault("table", "memories_qwen3")
    cfg["providers"]["pgvector"].setdefault("additional_tables", ["kg_observations_qwen3"])
    cfg["providers"]["pgvector"].setdefault("embedder", "ollama")
    cfg["providers"]["pgvector"].setdefault("embedder_options", {
        "url": "http://192.168.178.2:11433/api/embeddings",
        "model": "qwen3-embedding:0.6b",
        "timeout": 30.0,
        "num_ctx": 16384,
        "max_chars": 30000,
    })
    cfg["providers"]["pgvector"].setdefault("recall_k", 5)
    cfg["providers"]["pgvector"].setdefault("store_mode", "auto")

    # 5. Register in ~/.claude.json mcpServers (root level so it's
    # visible to every project -- this is the user-installed shape).
    if dry_run:
        print(f"  [dry-run] Would register mcpServers.pgvector in ~/.claude.json")
    else:
        _register_pgvector_mcp_in_claude_json(launcher_path)
        print(f"  ~/.claude.json: registered mcpServers.pgvector -> {launcher_path}")

    print(f"  Done. After Claude Code restart, tools surface as:")
    print(f"    mcp__pgvector__pgvector-find / -find-hybrid / -store / -count")
    print(f"    mcp__pgvector__pgvector-kg-search / -kg-create / -kg-observe / -kg-relate")


def _pgvector_launcher_path() -> Path:
    """Choose the system-wide install location for the launcher script.

    POSIX: ``~/.local/bin/pgvector-mcp``. Almost universally on PATH on
    modern desktops; users without it get a one-line warning.
    Windows: ``%LOCALAPPDATA%/claude-hooks/bin/pgvector-mcp.cmd``. Not
    on PATH by default but still discoverable as an absolute path --
    Claude Code's mcpServers entry uses the absolute path so PATH
    membership doesn't matter for the primary use case.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"))
        return base / "claude-hooks" / "bin" / "pgvector-mcp.cmd"
    return Path(os.path.expanduser("~/.local/bin/pgvector-mcp"))


def _write_pgvector_launcher(path: Path, *, py: str, repo: str) -> None:
    """Write the launcher script with interpreter + PYTHONPATH baked in.

    POSIX: a tiny POSIX sh that exports PYTHONPATH and execs the
    interpreter with ``-m claude_hooks.pgvector_mcp``. Windows: an
    equivalent .cmd that does the same with %ERRORLEVEL%-correct exit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        body = (
            "@echo off\r\n"
            "REM pgvector-mcp launcher (claude-hooks) -- generated by install.py\r\n"
            f'set PYTHONPATH={repo};%PYTHONPATH%\r\n'
            f'"{py}" -m claude_hooks.pgvector_mcp %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )
        path.write_text(body, encoding="utf-8")
    else:
        body = (
            "#!/usr/bin/env sh\n"
            "# pgvector-mcp launcher (claude-hooks) -- generated by install.py\n"
            f'PYTHONPATH="{repo}:${{PYTHONPATH:-}}" exec "{py}" -m claude_hooks.pgvector_mcp "$@"\n'
        )
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    # Warn if the dir isn't on PATH so the user sees `pgvector-mcp` from
    # other tools without an absolute path.
    if str(path.parent) not in (os.environ.get("PATH") or "").split(os.pathsep):
        print(f"  [!] {path.parent} is not in PATH -- only Claude Code can find it (absolute path).")
        print(f"      Add to PATH if you want Cursor/Codex/etc. to spawn `pgvector-mcp` by name.")


def _register_pgvector_mcp_in_claude_json(launcher_path: Path) -> None:
    """Register ``mcpServers.pgvector`` at the root of ``~/.claude.json``.

    Root-level so the server is visible to every project. Per-project
    filtering (companion_integration's per_project_mcp_filter) decides
    which projects emit the SessionStart hint, but the server itself is
    always available. Backs up the existing config first.
    """
    p = Path(os.path.expanduser("~/.claude.json"))
    if not p.exists():
        # Fresh install -- write a minimal scaffold.
        p.write_text("{}", encoding="utf-8")
    raw = p.read_text(encoding="utf-8")
    ts = _now_ts()
    bak = p.with_suffix(f".json.bak-{ts}-pgvector-mcp")
    bak.write_text(raw, encoding="utf-8")
    cfg = json.loads(raw)
    mcps = cfg.setdefault("mcpServers", {})
    mcps["pgvector"] = {
        "type": "stdio",
        "command": str(launcher_path),
        "args": [],
        "env": {},
    }
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _now_ts() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ollama_base_from_embed_url(url: str) -> str:
    """Strip the path off a configured embedder URL to get the daemon root.

    The provider config points at ``http://host:port/api/embeddings``;
    Ollama's ``/api/tags`` and ``/api/pull`` live at the same host on
    the same port, so we just chop the path.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def _ollama_model_present(base: str, model: str) -> bool:
    """Return True iff the model appears in ``/api/tags``.

    Match is exact on the full ``name`` (e.g. ``qwen3-embedding:0.6b``).
    Returns False on any HTTP / decode error so the caller offers to
    pull -- failing safe is the right default here; an extra ollama
    pull on an already-present model is a near-instant no-op.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return False
    for m in (data.get("models") or []):
        if m.get("name") == model or m.get("model") == model:
            return True
    return False


def _ollama_pull(base: str, model: str) -> bool:
    """POST ``/api/pull`` and stream progress to stdout.

    Ollama responds with a stream of NDJSON status lines; we print
    each new ``status`` value (one line per phase, e.g.
    ``pulling manifest`` -> ``pulling 5fa7e35e...`` -> ``verifying
    sha256 digest`` -> ``writing manifest`` -> ``success``). Pull is
    idempotent -- already-present models stream a one-shot
    ``status: success`` and exit immediately.

    Returns True on a clean ``success``; False on any error, network
    timeout, or non-success terminal status.
    """
    import urllib.error
    import urllib.request
    body = json.dumps({"name": model}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/pull", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_status = ""
    success = False
    try:
        # 30 min hard cap -- embed models are 100MB-1GB so even slow
        # connections finish well under this. Streaming is open-ended
        # so we lean on urlopen's per-read deadline rather than a wall
        # clock.
        with urllib.request.urlopen(req, timeout=1800) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if obj.get("error"):
                    print(f"    error: {obj['error']}")
                    return False
                status = obj.get("status") or ""
                if status and status != last_status:
                    print(f"    {status}")
                    last_status = status
                if status == "success":
                    success = True
    except urllib.error.URLError as e:
        print(f"    pull failed: {e.reason}")
        return False
    except Exception as e:
        print(f"    pull failed: {e}")
        return False
    return success


def _pgvector_tables_present(dsn: str, table_name: str) -> bool:
    """Return True iff ``table_name`` exists. Used to gate auto-init.

    Probing only the primary memories table is enough -- if it exists
    we treat the schema as initialized. The shared kg_entities /
    kg_relations / kg_observations_<model> get audited inside
    ``_init_pgvector_schema`` (every CREATE is idempotent).
    """
    try:
        import psycopg  # type: ignore
    except ImportError:
        return False
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    (table_name,),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


# Shared (model-agnostic) DDL -- kg_entities + kg_relations + the
# trigger function kg_entities uses to keep updated_at honest. Matches
# the live solidpc schema we built earlier; idempotent on every line.
_PGVECTOR_SHARED_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS kg_entities (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT kg_entities_name_unique UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS kg_entities_type_idx ON kg_entities (entity_type);
CREATE INDEX IF NOT EXISTS kg_entities_name_trgm
    ON kg_entities USING gin (name gin_trgm_ops);

DROP TRIGGER IF EXISTS kg_entities_touch_updated_at ON kg_entities;
CREATE TRIGGER kg_entities_touch_updated_at
    BEFORE UPDATE ON kg_entities
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE IF NOT EXISTS kg_relations (
    id              BIGSERIAL PRIMARY KEY,
    from_entity_id  BIGINT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    to_entity_id    BIGINT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT kg_relations_unique UNIQUE (from_entity_id, to_entity_id, relation_type)
);
CREATE INDEX IF NOT EXISTS kg_relations_from_idx ON kg_relations (from_entity_id);
CREATE INDEX IF NOT EXISTS kg_relations_to_idx ON kg_relations (to_entity_id);
CREATE INDEX IF NOT EXISTS kg_relations_type_idx ON kg_relations (relation_type);
"""


def _init_pgvector_schema(dsn: str, *, model: str = "qwen3") -> None:
    """Create the shared KG tables + per-model memories/kg_observations.

    Idempotent: every CREATE has IF NOT EXISTS, the trigger uses
    DROP+CREATE, and CREATE OR REPLACE on the touch_updated_at function.
    Re-running on a populated DB is a no-op.

    The per-model DDL is delegated to
    ``scripts.migrate_to_pgvector.schema_sql_for_model`` so install.py
    and the bulk migration stay in lock-step on table layout, indexes,
    and constraints -- drift between them silently breaks recall.
    """
    import psycopg  # type: ignore
    sys.path.insert(0, str(HERE / "scripts"))
    try:
        from migrate_to_pgvector import MODELS, schema_sql_for_model  # type: ignore
    finally:
        sys.path.pop(0)
    spec = MODELS[model]
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_PGVECTOR_SHARED_DDL)
            cur.execute(schema_sql_for_model(spec))
        conn.commit()


def _ensure_proxy_deps(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Verify httpx + h2 are available when the proxy is enabled.

    The proxy forwarder requires HTTP/2 (via httpx[http2]) to match
    native Claude Code's connection profile. HTTP/1.1-per-request
    trips Anthropic's edge 429 gate.

    Runs after save_config so it sees the just-written state. No-op
    when proxy.enabled is false.
    """
    proxy_cfg = (cfg.get("proxy") or {})
    if not proxy_cfg.get("enabled", False):
        return

    conda_py = find_conda_env_python()
    # Use conda env's python when available, else system python.
    py = str(conda_py) if conda_py.exists() else sys.executable

    probe = subprocess.run(
        [py, "-c", "import httpx, h2; print(httpx.__version__, h2.__version__)"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        print(f"\nProxy deps:     httpx + h2 OK ({probe.stdout.strip()})")
        return

    print("\nProxy deps:     httpx / h2 MISSING")
    print("  The proxy forwarder needs httpx[http2] to pass Anthropic's")
    print("  HTTP/2 edge gate. Without it the proxy will import-error.")

    if dry_run:
        print(f"  [dry-run] Would: {py} -m pip install 'httpx[http2]>=0.27'")
        return

    if non_interactive:
        print("  --non-interactive: installing httpx[http2]...")
    else:
        ans = input("  Install httpx[http2] now? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped. Proxy will fail to start until installed manually:")
            print(f"    {py} -m pip install 'httpx[http2]>=0.27'")
            return

    pip_bin = str(Path(py).parent / ("pip.exe" if os.name == "nt" else "pip"))
    pip_cmd = [pip_bin, "install", "httpx[http2]>=0.27"] if Path(pip_bin).exists() \
        else [py, "-m", "pip", "install", "httpx[http2]>=0.27"]
    rc = subprocess.run(pip_cmd, capture_output=True, text=True)
    if rc.returncode == 0:
        print("  Installed.")
    else:
        print(f"  pip install failed:\n{rc.stderr[-500:]}")
        print(f"  Run manually: {' '.join(pip_cmd)}")


CODE_GRAPH_EXTRAS = (
    {
        "name": "tree-sitter (multi-language code-graph)",
        "probe": "import tree_sitter_language_pack",
        "pkgs": ["tree-sitter-language-pack>=0.13"],
        "feature": (
            "Without this, code_graph parses Python only (via stdlib ast). "
            "With it, it also parses JS/TS/Go/Rust/Java/Ruby."
        ),
        "config_extra": "code-graph",
    },
    {
        "name": "Louvain clustering",
        "probe": "import community, networkx",
        "pkgs": ["python-louvain>=0.16", "networkx>=3.0"],
        "feature": (
            "Replaces the file-based fallback in `code_graph clusters` with "
            "modularity-based community detection (cohesion scores + cross-"
            "file groupings)."
        ),
        "config_extra": "clustering",
    },
    {
        "name": "MCP server (code_graph as live tools)",
        "probe": "from mcp.server.fastmcp import FastMCP",
        "pkgs": ["mcp[cli]>=1.0"],
        "feature": (
            "Lets the model call lookup/impact/changes/trace/mermaid/companions "
            "as MCP tools instead of via Grep + report. Adds a stdio entry "
            "to ~/.claude.json's mcpServers when wired."
        ),
        "config_extra": "mcp-server",
    },
)


def _ensure_code_graph_extras(*, non_interactive: bool, dry_run: bool) -> None:
    """Probe the conda env for each code_graph optional extra; offer to install.

    Mirrors :func:`_ensure_proxy_deps`. Each extra is gated by an import
    probe -- if it imports cleanly we move on; otherwise we describe what
    the user gains by installing and ask. Defaults to ``Y`` to keep the
    installer feeling forward.
    """
    conda_py = find_conda_env_python()
    py = str(conda_py) if conda_py.exists() else sys.executable

    print("\n==> code_graph optional extras")
    print("    code_graph runs without these -- they unlock additional features.")
    print(f"    Target Python: {py}")

    for extra in CODE_GRAPH_EXTRAS:
        probe = subprocess.run(
            [py, "-c", extra["probe"]],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            print(f"  {extra['name']:48} OK")
            continue

        print(f"  {extra['name']:48} MISSING")
        print(f"    {extra['feature']}")
        pkg_list = " ".join(repr(p) for p in extra["pkgs"])

        if dry_run:
            print(f"    [dry-run] Would: {py} -m pip install {pkg_list}")
            continue

        if non_interactive:
            print("    --non-interactive: installing...")
        else:
            ans = input(f"    Install? [Y/n]: ").strip().lower()
            if ans not in ("", "y", "yes"):
                print(f"    Skipped. To install later:")
                print(f"      {py} -m pip install {pkg_list}")
                print(f"      # or via the extra: pip install 'claude-hooks[{extra['config_extra']}]'")
                continue

        pip_bin = str(Path(py).parent / ("pip.exe" if os.name == "nt" else "pip"))
        pip_cmd = ([pip_bin, "install", *extra["pkgs"]]
                   if Path(pip_bin).exists()
                   else [py, "-m", "pip", "install", *extra["pkgs"]])
        rc = subprocess.run(pip_cmd, capture_output=True, text=True)
        if rc.returncode == 0:
            print("    Installed.")
        else:
            print(f"    pip install failed:\n{rc.stderr[-500:]}")
            print(f"    Run manually: {' '.join(pip_cmd)}")


def _check_conda_env(*, non_interactive: bool, dry_run: bool) -> None:
    """Check the conda env, offer to create it + install deps if missing."""
    conda_py = find_conda_env_python()
    in_conda = os.environ.get("CONDA_DEFAULT_ENV") == "claude-hooks"

    if conda_py.exists():
        if in_conda:
            print(f"Conda env:      claude-hooks (active)")
        else:
            print(f"Conda env:      claude-hooks (exists, not active)")
        print(f"Hook runtime:   {conda_py}")
        return

    # Env doesn't exist -- offer to create it.
    print("Conda env:      NOT FOUND")
    conda_bin = _find_conda()
    if not conda_bin:
        print("  conda not found on this system -- skipping env setup.")
        print("  Hooks will fall back to system python3.\n")
        print(f"Hook runtime:   system python3")
        return

    if non_interactive:
        print("  --non-interactive: skipping env creation.")
        print(f"Hook runtime:   system python3")
        return

    ans = input("  Create conda env 'claude-hooks' (Python 3.11) and install deps? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes"):
        print(f"Hook runtime:   system python3")
        return

    if dry_run:
        print("  [dry-run] Would create conda env and install requirements.")
        print(f"Hook runtime:   system python3")
        return

    print("  Creating conda env 'claude-hooks'...")
    rc = subprocess.run(
        [conda_bin, "create", "-n", "claude-hooks", "python=3.11", "-y"],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  conda create failed:\n{rc.stderr[-300:]}")
        print(f"Hook runtime:   system python3")
        return

    # Install requirements into the new env.
    env_pip = str(conda_py.parent / "pip") if os.name != "nt" else str(conda_py.parent / "pip.exe")
    req_dev = HERE / "requirements-dev.txt"
    req_main = HERE / "requirements.txt"
    for req in [req_dev, req_main]:
        if req.exists():
            print(f"  Installing {req.name}...")
            subprocess.run(
                [env_pip, "install", "-r", str(req)],
                capture_output=True, text=True,
            )

    if conda_py.exists():
        print(f"  Done -- conda env ready.")
        print(f"Hook runtime:   {conda_py}")
    else:
        print(f"  Warning: env created but python not found at {conda_py}")
        print(f"Hook runtime:   system python3")


def main() -> int:
    ap = argparse.ArgumentParser(prog="install.py", description="claude-hooks installer")
    ap.add_argument("--dry-run", action="store_true", help="don't write any files")
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt -- fail if a decision is needed",
    )
    ap.add_argument("--uninstall", action="store_true", help="remove claude-hooks from settings.json")
    ap.add_argument("--probe", action="store_true", help="force tool-probe detection")
    ap.add_argument("--config", type=str, default=None, help="alternate claude-hooks.json path")
    ap.add_argument(
        "--episodic-server",
        action="store_true",
        help="configure this host as episodic-memory server (runs the HTTP API)",
    )
    ap.add_argument(
        "--episodic-client",
        type=str,
        metavar="URL",
        help="configure as episodic client, pushing transcripts to URL (e.g. http://192.168.178.2:11435)",
    )
    args = ap.parse_args()

    if args.uninstall:
        return uninstall(dry_run=args.dry_run)

    print("==> claude-hooks installer\n")

    _check_conda_env(non_interactive=args.non_interactive, dry_run=args.dry_run)

    cfg_path = Path(args.config) if args.config else default_config_path()
    print(f"Repo:           {HERE}")
    print(f"Config target:  {cfg_path}")

    claude_cfg_path = claude_config_path()
    if not claude_cfg_path.exists():
        print(f"\nWarning: {claude_cfg_path} does not exist. MCP servers cannot be auto-detected.")
        print("You can still configure claude-hooks manually after install.\n")
    else:
        print(f"Claude config:  {claude_cfg_path}\n")

    cfg = load_config(cfg_path)
    claude_cfg = load_claude_config(claude_cfg_path)

    # Detect MCP servers per provider.
    report = detect_all(claude_cfg, config_path=claude_cfg_path)
    if args.probe or any(not report.candidates_for(c.name) for c in REGISTRY):
        print("Probing unmatched servers for tool signatures...")
        probed = probe_unmatched(report)
        for pname, cands in probed.items():
            if cands:
                report.by_provider.setdefault(pname, []).extend(cands)

    # For each provider, ask the user to pick (or skip).
    chosen: dict[str, Optional[ServerCandidate]] = {}
    for cls in REGISTRY:
        # pgvector has bespoke setup (we own its MCP server, install
        # the launcher system-wide, configure DSN+embedder). Handled
        # by ``_setup_pgvector_mcp`` after the standard pick loop.
        if cls.name == "pgvector":
            continue
        chosen[cls.name] = pick_provider(cls, report, args.non_interactive)

    # Verify each chosen provider.
    print("\n==> Verifying chosen servers...")
    for cls in REGISTRY:
        if cls.name == "pgvector":
            continue
        candidate = chosen.get(cls.name)
        pcfg = (cfg.get("providers") or {}).get(cls.name) or {}
        if not candidate:
            if pcfg.get("enabled"):
                pcfg["enabled"] = False
                print(f"  {cls.display_name:24} disabled (no candidate)")
            continue
        ok = cls.verify(candidate)
        status = "OK" if ok else "UNREACHABLE"
        print(f"  {cls.display_name:24} {status}  ({candidate.url})")
        if ok:
            pcfg["enabled"] = True
            pcfg["mcp_url"] = candidate.url
            if candidate.headers:
                pcfg["headers"] = candidate.headers
            cfg.setdefault("providers", {})[cls.name] = pcfg

    # pgvector: ask if available, install system-wide launcher, register
    # in mcpServers. Self-contained -- no detect/verify path through the
    # generic loop above.
    _setup_pgvector_mcp(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # API proxy orchestrator: ask whether to use the proxy at all,
    # then choose local install vs existing remote URL. Mutates
    # ``cfg["proxy"]["enabled"]`` in-place so the per-OS installers
    # downstream see the right value, and writes
    # ``ANTHROPIC_BASE_URL`` directly into settings.json for the
    # remote-URL case (no local service install needed).
    _setup_proxy_orchestrator(
        cfg,
        user_settings_path(),
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # Self-update check: opt-in. The check itself runs on the
    # long-lived claude-hooks-daemon thread, so the Stop hook never
    # blocks on network I/O.
    _setup_update_check(cfg, non_interactive=args.non_interactive)

    # Save config.
    if args.dry_run:
        print(f"\n[dry-run] Would write config to {cfg_path}:")
        print(json.dumps(cfg, indent=2))
    else:
        save_config(cfg, cfg_path)
        print(f"\nConfig written: {cfg_path}")

    # Ensure proxy deps (httpx + h2) are installed when the proxy
    # is enabled. The httpx[http2] profile is what lets the proxy
    # pass Anthropic's edge gate that 429s HTTP/1.1-per-request.
    _ensure_proxy_deps(cfg, non_interactive=args.non_interactive, dry_run=args.dry_run)

    # Offer to install the code_graph optional extras (multi-language
    # tree-sitter, Louvain clustering, MCP server). Each is opt-in; the
    # installer probes the conda env first and only asks for missing ones.
    _ensure_code_graph_extras(
        non_interactive=args.non_interactive, dry_run=args.dry_run,
    )

    # Offer to install the proxy + rollup-timer + dashboard systemd
    # units (Linux only; idempotent -- skips units already installed).
    _install_proxy_stack_systemd(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    # macOS and Windows analogues -- each silent no-op when its OS
    # doesn't match. Linux ships proxy + rollup + dashboard via the
    # call above; macOS/Windows install just the proxy itself
    # (rollup/dashboard are Linux conveniences and run fine standalone
    # as periodic ``python -m claude_hooks.proxy.dashboard`` etc. when
    # the user wants them on those OSes).
    _install_proxy_launchd(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    _install_proxy_windows(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    # Offer to install the caliber grounding proxy systemd unit (opt-in
    # under caliber_proxy.enabled in config). Runs a local OpenAI-
    # compat proxy that adds project grounding + tools to caliber calls
    # routed at Ollama. See claude_hooks/caliber_proxy/.
    _install_caliber_proxy_systemd(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    # Offer to install the pgvector MCP HTTP frontend (opt-in under
    # providers.pgvector.enabled). Exposes the same JSON-RPC surface
    # as the stdio launcher at http://<host>:32775/mcp so Claude
    # Desktop / any remote MCP client can reach the memory store.
    # Stdio remains the default for local Claude Code sessions.
    _install_pgvector_mcp_systemd(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    # Offer to install the axon shared-host systemd unit (opt-in under
    # companions.axon_host.enabled in config). Runs a singleton axon
    # daemon at http://127.0.0.1:8420/mcp so users can drop the legacy
    # `axon serve --watch` per-session stdio MCP - the per-session form
    # auto-indexes whatever cwd Claude Code launched in, which on
    # 2026-04-27 ate 64 GB of RAM on a model directory.
    _install_axon_host_systemd(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # Offer to install the long-lived hook executor (Tier 3.8). When
    # enabled, the bin/claude-hook shim sends events to the running
    # daemon over an HMAC-authenticated TCP localhost socket instead of
    # spinning up a fresh interpreter -- saves 150-300 ms per hook. The
    # client falls back to in-process dispatch automatically when the
    # daemon isn't running, so this step is strictly optional.
    _install_claude_hooks_daemon(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # Merge hooks into settings.json.
    settings_path = user_settings_path()
    print(f"\n==> Updating {settings_path}")
    install_hooks(
        settings_path,
        repo_path=HERE,
        include_pre_tool_use=bool(((cfg.get("hooks") or {}).get("pre_tool_use") or {}).get("enabled")),
        # PostToolUse defaults to ON (the ruff hook is the user-facing
        # quality-of-life feature; ``hooks.post_tool_use.enabled`` is
        # true by default in the example config). Honour an explicit
        # false in the user's config.
        include_post_tool_use=bool(((cfg.get("hooks") or {}).get("post_tool_use") or {}).get("enabled", True)),
        # PreCompact also defaults to ON. The handler self-gates on
        # the wrapup skill being installed, so a wired entry is a
        # cheap no-op when the skill is absent.
        include_pre_compact=bool(((cfg.get("hooks") or {}).get("pre_compact") or {}).get("enabled", True)),
        dry_run=args.dry_run,
    )

    # Detect companion tools and install skills.
    print("\n==> Companion tools")
    installed_tools = _detect_companion_tools()
    _install_skills(installed_tools, non_interactive=args.non_interactive, dry_run=args.dry_run)

    # Episodic memory setup.
    _setup_episodic(cfg, cfg_path, args, dry_run=args.dry_run)

    # Optional: Claude Code env-var recommendations.
    _prompt_env_vars(
        settings_path,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    conda_py = find_conda_env_python()
    print("\n==> Done.")
    print("    Open a new Claude Code session and the hooks will fire on the next prompt.")
    print(f"    Runtime: {conda_py if conda_py.exists() else 'system python3'}")
    print("    Logs:    ~/.claude/claude-hooks.log")
    print("    Config:  ", cfg_path)
    return 0


# ---------------------------------------------------------------------- #
# Provider picking
# ---------------------------------------------------------------------- #
def pick_provider(cls, report: DetectionReport, non_interactive: bool) -> Optional[ServerCandidate]:
    cands = report.candidates_for(cls.name)
    label = cls.display_name
    print(f"\n--- {label} ---")
    if not cands:
        print(f"  No candidates detected.")
        if non_interactive:
            return None
        url = input(f"  Enter MCP URL for {label} (or empty to skip): ").strip()
        if not url:
            return None
        return ServerCandidate(
            server_key=cls.name, url=url, source="manual", confidence="manual"
        )
    if len(cands) == 1:
        c = cands[0]
        print(f"  Found: '{c.server_key}' -> {c.url}  ({c.notes})")
        if non_interactive:
            return c
        ans = input(f"  Use this? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            return c
        return None
    print(f"  Multiple candidates:")
    for i, c in enumerate(cands, 1):
        print(f"    [{i}] '{c.server_key}' -> {c.url}  ({c.source}, {c.confidence})")
    if non_interactive:
        print(f"  --non-interactive set; picking the first.")
        return cands[0]
    while True:
        ans = input(f"  Pick one [1-{len(cands)}] or 0 to skip: ").strip()
        if not ans:
            return cands[0]
        try:
            idx = int(ans)
        except ValueError:
            continue
        if idx == 0:
            return None
        if 1 <= idx <= len(cands):
            return cands[idx - 1]


# ---------------------------------------------------------------------- #
# settings.json wiring
# ---------------------------------------------------------------------- #
def user_settings_path() -> Path:
    """Return the path to ~/.claude/settings.json (works on both OSes)."""
    return Path(os.path.expanduser("~/.claude/settings.json"))


def install_hooks(
    settings_path: Path,
    *,
    repo_path: Path,
    include_pre_tool_use: bool,
    include_post_tool_use: bool,
    include_pre_compact: bool = True,
    dry_run: bool,
) -> None:
    settings = _load_json(settings_path)
    backup = backup_path(settings_path)
    if settings_path.exists() and not dry_run:
        shutil.copy2(settings_path, backup)
        print(f"  Backup written: {backup}")

    cmd = build_command(repo_path)
    print(f"  Hook command:   {cmd}")

    template = deepcopy(HOOK_TEMPLATE)
    if include_pre_tool_use:
        template.update(deepcopy(PRE_TOOL_USE_TEMPLATE))
    if include_post_tool_use:
        template.update(deepcopy(POST_TOOL_USE_TEMPLATE))
    if include_pre_compact:
        template.update(deepcopy(PRE_COMPACT_TEMPLATE))

    # Substitute the {cmd} placeholder.
    for event, blocks in template.items():
        for block in blocks:
            for h in block["hooks"]:
                h["command"] = h["command"].format(cmd=cmd)

    settings.setdefault("hooks", {})
    for event, blocks in template.items():
        existing = settings["hooks"].get(event) or []
        # Drop ALL previous claude-hooks entries -- by _managedBy tag OR
        # by command containing "claude-hook" (catches manually installed ones).
        cleaned: list[dict] = []
        for blk in existing:
            if not isinstance(blk, dict):
                continue
            kept_hooks = [
                h
                for h in (blk.get("hooks") or [])
                if not _is_our_hook(h)
            ]
            if kept_hooks:
                blk = dict(blk)
                blk["hooks"] = kept_hooks
                cleaned.append(blk)
        cleaned.extend(blocks)
        settings["hooks"][event] = cleaned

    if dry_run:
        print(f"\n[dry-run] Would write to {settings_path}:")
        print(json.dumps(settings, indent=2))
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _save_json(settings_path, settings)
    print(f"  Settings updated: {settings_path}")


def uninstall(*, dry_run: bool) -> int:
    print("==> claude-hooks uninstall")
    settings_path = user_settings_path()
    if not settings_path.exists():
        print(f"  No settings at {settings_path} -- nothing to do.")
        return 0
    settings = _load_json(settings_path)
    hooks = settings.get("hooks") or {}
    removed = 0
    for event, blocks in list(hooks.items()):
        if not isinstance(blocks, list):
            continue
        cleaned: list[dict] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            kept = [
                h
                for h in (blk.get("hooks") or [])
                if not _is_our_hook(h)
            ]
            removed += len(blk.get("hooks") or []) - len(kept)
            if kept:
                blk = dict(blk)
                blk["hooks"] = kept
                cleaned.append(blk)
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]
    print(f"  Removed {removed} claude-hooks entries from {settings_path}")
    if dry_run:
        print("[dry-run] Not writing.")
        return 0
    backup = backup_path(settings_path)
    shutil.copy2(settings_path, backup)
    print(f"  Backup written: {backup}")
    _save_json(settings_path, settings)
    return 0


def _is_our_hook(h: dict) -> bool:
    """Check if a hook entry belongs to claude-hooks (by tag or command pattern)."""
    if not isinstance(h, dict):
        return False
    if h.get("_managedBy") == MANAGED_BY:
        return True
    cmd = h.get("command", "")
    return "claude-hook" in cmd and ("claude-hook " in cmd or "claude-hook.cmd" in cmd)


def build_command(repo_path: Path) -> str:
    """Return the literal hook command string for the current OS.

    Claude Code runs hooks via /usr/bin/bash on ALL platforms (including
    Windows), so we always use the extensionless POSIX shim with forward
    slashes. The .cmd shim is kept for manual use but not wired into hooks.
    """
    repo_path = repo_path.resolve()
    cmd = str(repo_path / "bin" / "claude-hook")
    # Windows paths use backslashes -- convert to forward slashes so bash
    # can parse the path correctly.
    return cmd.replace("\\", "/")


def backup_path(p: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return p.with_suffix(p.suffix + f".bak-{ts}")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------- #
# Companion tool detection + skill installation
# ---------------------------------------------------------------------- #

# Each companion tool: (binary name, npm package, importance, description)
COMPANION_TOOLS = [
    ("mnemex",          "mnemex",                   "HIGH",   "semantic code search (AST-aware, embedding-based)"),
    ("caliber",         "@rely-ai/caliber",         "MEDIUM", "config quality scoring and drift detection"),
    ("claudekit",       "claudekit",                "MEDIUM", "git checkpoints and hook profiling"),
    ("episodic-memory", None,                       "HIGH",   "transcript search across past sessions (build from source)"),
]

# Skills shipped with the repo and what they require.
# requirement: None = always install, or a tool binary name.
SKILLS = [
    ("reflect",       None),         # built-in: uses claude-hooks reflect module
    ("consolidate",   None),         # built-in: uses claude-hooks consolidate module
    ("save-learning", None),         # standalone
    ("find-skills",   None),         # standalone
    ("setup-caliber", "caliber"),    # needs caliber installed
    ("episodic",      None),         # queries remote episodic-server API
    ("wrapup",        None),         # session state summary for hand-off / compact
]


def _detect_companion_tools() -> dict[str, bool]:
    """Check which companion tools are installed. Returns {name: bool}."""
    result: dict[str, bool] = {}
    for bin_name, npm_pkg, importance, description in COMPANION_TOOLS:
        found = shutil.which(bin_name) is not None
        status = "installed" if found else "MISSING"
        marker = "  [ok]" if found else "  [!!]"
        print(f"{marker} {bin_name:24} {status:12} [{importance}] {description}")
        result[bin_name] = found

    missing = [(n, pkg, imp, desc) for n, pkg, imp, desc in COMPANION_TOOLS
               if not result[n] and pkg is not None]
    if missing:
        print(f"\n  {len(missing)} tool(s) can be installed via npm:")
        for bin_name, npm_pkg, importance, _ in missing:
            print(f"    npm install -g {npm_pkg}")

    # Check and configure the MadAppGang marketplace for code-analysis plugin.
    _ensure_marketplace()

    return result


MARKETPLACE_KEY = "mag-claude-plugins"
MARKETPLACE_VALUE = {"source": {"source": "github", "repo": "MadAppGang/claude-code"}}


def _ensure_marketplace() -> None:
    """Ensure the MadAppGang plugin marketplace is registered in settings.json."""
    settings_path = user_settings_path()
    if not settings_path.exists():
        return
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return

    markets = settings.get("extraKnownMarketplaces") or {}
    if MARKETPLACE_KEY not in markets:
        print(f"\n  [!!] Plugin marketplace: {MARKETPLACE_KEY} not registered")
        markets[MARKETPLACE_KEY] = MARKETPLACE_VALUE
        settings["extraKnownMarketplaces"] = markets
        _save_json(settings_path, settings)
        print(f"  [ok] Registered {MARKETPLACE_KEY} in {settings_path}")
    else:
        print(f"\n  [ok] Plugin marketplace: {MARKETPLACE_KEY} (registered)")

    # Enable recommended plugins.
    enabled = settings.setdefault("enabledPlugins", {})
    recommended = {
        "code-analysis@mag-claude-plugins": "deep codebase investigation (needs mnemex)",
        "frontend-design@claude-plugins-official": "production-grade frontend UI generation",
    }
    changed = False
    for plugin_id, desc in recommended.items():
        if plugin_id not in enabled:
            enabled[plugin_id] = True
            print(f"  [ok] Enabled plugin: {plugin_id} ({desc})")
            changed = True
        else:
            print(f"  [ok] Plugin: {plugin_id} (already enabled)")
    if changed:
        _save_json(settings_path, settings)

    # Fix stale plugin install paths (e.g. Linux paths on Windows or vice versa).
    _fix_plugin_paths()

    print(f"\n       To add marketplace in Claude Code: /plugin marketplace add MadAppGang/claude-code")


def _install_skills(
    installed_tools: dict[str, bool],
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Copy skills from the repo to ~/.claude/skills/, respecting deps."""
    user_skills_dir = Path(os.path.expanduser("~/.claude/skills"))
    repo_skills_dir = HERE / ".claude" / "skills"

    if not repo_skills_dir.exists():
        return

    print(f"\n==> Skills (target: {user_skills_dir})")

    to_install: list[str] = []
    skipped: list[tuple[str, str]] = []

    for skill_name, requires_tool in SKILLS:
        src = repo_skills_dir / skill_name
        if not src.exists():
            continue
        dst = user_skills_dir / skill_name
        already = dst.exists() and (dst / "SKILL.md").exists()

        if requires_tool and not installed_tools.get(requires_tool, False):
            if already:
                skipped.append((skill_name, f"keeping existing, but {requires_tool} not found"))
            else:
                skipped.append((skill_name, f"requires {requires_tool}"))
            continue

        if already:
            # Check if repo version is newer (compare content).
            src_content = (src / "SKILL.md").read_text(encoding="utf-8")
            dst_content = (dst / "SKILL.md").read_text(encoding="utf-8")
            if src_content == dst_content:
                print(f"  [ok] /{skill_name:20} up to date")
                continue
            else:
                to_install.append(skill_name)
                print(f"  [up] /{skill_name:20} will update")
        else:
            to_install.append(skill_name)
            print(f"  + /{skill_name:20} will install")

    for skill_name, reason in skipped:
        print(f"  [--] /{skill_name:20} skipped ({reason})")

    if not to_install:
        if not skipped:
            print("  All skills up to date.")
        return

    if not non_interactive:
        ans = input(f"\n  Install/update {len(to_install)} skill(s)? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    if dry_run:
        print(f"  [dry-run] Would install: {', '.join(to_install)}")
        return

    for skill_name in to_install:
        src = repo_skills_dir / skill_name
        dst = user_skills_dir / skill_name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "SKILL.md", dst / "SKILL.md")
        print(f"  [ok] /{skill_name} installed")


def _setup_episodic(cfg: dict, cfg_path: Path, args, *, dry_run: bool) -> None:
    """Configure episodic memory server or client mode."""
    ep_cfg = cfg.setdefault("episodic", {})
    current_mode = ep_cfg.get("mode", "off")

    if args.episodic_server:
        print("\n==> Episodic memory: SERVER mode")
        if not shutil.which("episodic-memory"):
            print("  [!!] episodic-memory not found. Install it first:")
            print("       git clone https://github.com/obra/episodic-memory")
            print("       cd episodic-memory && npm install && npm link")
            return
        ep_cfg["mode"] = "server"

        # Ask for bind address and port.
        default_host = ep_cfg.get("server_host", "0.0.0.0")
        default_port = int(ep_cfg.get("server_port", 11435))
        if not args.non_interactive:
            host_input = input(f"  Bind address [{default_host}]: ").strip()
            port_input = input(f"  Port [{default_port}]: ").strip()
            if host_input:
                default_host = host_input
            if port_input:
                default_port = int(port_input)
        ep_cfg["server_host"] = default_host
        ep_cfg["server_port"] = default_port

        print(f"  Mode:   server")
        print(f"  Bind:   {default_host}:{default_port}")
        print(f"  Binary: {shutil.which('episodic-memory')}")
        if not dry_run:
            save_config(cfg, cfg_path)
            print(f"  Config updated: episodic.mode = server")
        # Offer systemd service install (Linux only).
        if os.name != "nt":
            _install_episodic_systemd(
                default_host, default_port,
                non_interactive=args.non_interactive, dry_run=dry_run,
            )

    elif args.episodic_client:
        print("\n==> Episodic memory: CLIENT mode")
        server_url = args.episodic_client.rstrip("/")
        ep_cfg["mode"] = "client"
        ep_cfg["server_url"] = server_url
        print(f"  Mode:       client")
        print(f"  Server URL: {server_url}")
        # Test connectivity.
        print(f"  Testing connection...", end=" ")
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{server_url}/health",
                headers={"Connection": "close"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                print(f"OK (archive: {data.get('archive', '?')})")
        except Exception as e:
            print(f"UNREACHABLE ({e})")
            print(f"  Warning: server not reachable. Transcripts will be pushed when it's up.")
        print(f"  SessionEnd hook will push transcripts to {server_url}/ingest")
        if not dry_run:
            save_config(cfg, cfg_path)
            print(f"  Config updated: episodic.mode = client")

    elif current_mode != "off":
        print(f"\n==> Episodic memory: {current_mode.upper()} mode (already configured)")
        if current_mode == "client":
            print(f"  Server URL: {ep_cfg.get('server_url', '?')}")
    else:
        # Not configured -- mention availability.
        print(f"\n  Episodic memory: not configured (use --episodic-server or --episodic-client URL)")


def _fix_plugin_paths() -> None:
    """Fix stale paths in plugin JSON files.

    If files were copied from another machine (e.g. Linux paths on Windows),
    rewrite paths to use local directories. Handles both installed_plugins.json
    (installPath) and known_marketplaces.json (installLocation).
    """
    plugins_dir = Path(os.path.expanduser("~/.claude/plugins"))
    total_fixed = 0

    # Fix installed_plugins.json -- installPath entries.
    installed_json = plugins_dir / "installed_plugins.json"
    if installed_json.exists():
        try:
            with open(installed_json, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            cache_dir = str(plugins_dir / "cache")
            fixed = 0
            for plugin_id, entries in data.get("plugins", {}).items():
                for entry in entries:
                    old_path = entry.get("installPath", "")
                    if not old_path:
                        continue
                    if Path(old_path).exists():
                        continue
                    for sep in ["/cache/", "\\cache\\"]:
                        if sep in old_path:
                            rel = old_path.split(sep, 1)[1]
                            new_path = os.path.join(cache_dir, rel).replace("\\", "/")
                            if Path(new_path).exists():
                                entry["installPath"] = new_path
                                fixed += 1
                            break
            if fixed:
                _save_json(installed_json, data)
                total_fixed += fixed
        except (json.JSONDecodeError, OSError):
            pass

    # Fix known_marketplaces.json -- installLocation entries.
    markets_json = plugins_dir / "known_marketplaces.json"
    if markets_json.exists():
        try:
            with open(markets_json, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            markets_dir = str(plugins_dir / "marketplaces")
            fixed = 0
            for market_id, info in data.items():
                old_path = info.get("installLocation", "")
                if not old_path:
                    continue
                if Path(old_path).exists():
                    continue
                for sep in ["/marketplaces/", "\\marketplaces\\"]:
                    if sep in old_path:
                        rel = old_path.split(sep, 1)[1]
                        new_path = os.path.join(markets_dir, rel).replace("\\", "/")
                        if Path(new_path).exists():
                            info["installLocation"] = new_path
                            fixed += 1
                        break
            if fixed:
                _save_json(markets_json, data)
                total_fixed += fixed
        except (json.JSONDecodeError, OSError):
            pass

    if total_fixed:
        print(f"  [ok] Fixed {total_fixed} stale path(s) in plugin config files")


SYSTEMD_UNIT = "episodic-server.service"
SYSTEMD_PATH = Path("/etc/systemd/system") / SYSTEMD_UNIT


def _install_episodic_systemd(host: str, port: int, *, non_interactive: bool, dry_run: bool) -> None:
    """Install the episodic-server as a systemd service."""
    template_path = HERE / "episodic_server" / "episodic-server.service"
    if not template_path.exists():
        print("  [!!] Service template not found")
        return

    already_installed = SYSTEMD_PATH.exists()
    if already_installed:
        # Check if it's running.
        rc = subprocess.run(
            ["systemctl", "is-active", "--quiet", SYSTEMD_UNIT],
            capture_output=True,
        )
        status = "running" if rc.returncode == 0 else "stopped"
        print(f"  Systemd service: already installed ({status})")
        if status == "running":
            return
        # Offer to start it.
        if not non_interactive:
            ans = input("  Start the service now? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes") and not dry_run:
                subprocess.run(["systemctl", "start", SYSTEMD_UNIT])
                print(f"  Service started.")
        return

    print(f"\n  Install as systemd service?")
    print(f"    - Starts on boot (after network)")
    print(f"    - Restarts on failure (30s delay, max 5 in 5min)")
    print(f"    - Logs via journalctl -u {SYSTEMD_UNIT}")
    if non_interactive:
        print("  --non-interactive: skipping service install.")
        print(f"  To start manually: python3 {HERE}/episodic_server/server.py --host {host} --port {port}")
        return

    ans = input("  Install systemd service? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes"):
        print(f"  Skipped. Start manually: python3 {HERE}/episodic_server/server.py --port {port}")
        return

    if dry_run:
        print(f"  [dry-run] Would install {SYSTEMD_PATH}")
        return

    # Read template, substitute placeholders.
    content = template_path.read_text(encoding="utf-8")
    content = content.replace("__REPO_PATH__", str(HERE.resolve()))
    content = content.replace("__HOST__", host)
    content = content.replace("__PORT__", str(port))

    # Expand ReadWritePaths for the actual user.
    home = str(Path.home())
    content = content.replace("/root/.config/superpowers", f"{home}/.config/superpowers")
    content = content.replace("/root/.claude", f"{home}/.claude")

    SYSTEMD_PATH.write_text(content, encoding="utf-8")
    print(f"  Installed: {SYSTEMD_PATH}")

    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "enable", SYSTEMD_UNIT], capture_output=True)
    print(f"  Enabled at boot.")

    subprocess.run(["systemctl", "start", SYSTEMD_UNIT], capture_output=True)
    time.sleep(1)
    rc = subprocess.run(
        ["systemctl", "is-active", "--quiet", SYSTEMD_UNIT],
        capture_output=True,
    )
    if rc.returncode == 0:
        print(f"  Service started successfully.")
        print(f"  Logs: journalctl -u {SYSTEMD_UNIT} -f")
    else:
        print(f"  [!!] Service failed to start. Check: journalctl -u {SYSTEMD_UNIT}")


def _prompt_env_vars(
    settings_path: Path,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Offer to inject opt-in Claude Code env-var recommendations into
    ~/.claude/settings.json. Defaults to No -- nothing is applied without
    explicit user consent.

    Covers:
      - CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1  (kills subagent Warmup
        drain; see docs/issue-warmup-token-drain.md + #47922).
      - The "bcherny stack" (DISABLE_ADAPTIVE_THINKING +
        MAX_THINKING_TOKENS + AUTO_COMPACT_WINDOW +
        AUTOCOMPACT_PCT_OVERRIDE). Default No -- per our field test it
        introduced more trivial mistakes on this project. Presented so
        users can opt in if they saw it recommended elsewhere.

    See docs/env-vars.md for the per-var verdict.
    """
    print("\n==> Optional Claude Code env-var recommendations")
    print("    (See docs/env-vars.md for full rationale and verdicts.)")

    if non_interactive:
        print("  --non-interactive: skipping (nothing applied).")
        return

    ans = input(
        "  Apply CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 to stop the\n"
        "  subagent Warmup token drain (issue #47922)?\n"
        "  Side-effect: also disables Ctrl+B and Bash run_in_background.\n"
        "  [y/N]: "
    ).strip().lower()
    warmup_fix = ans == "y"

    ans = input(
        "\n  Apply the bcherny stack (DISABLE_ADAPTIVE_THINKING=1,\n"
        "  MAX_THINKING_TOKENS=63999, AUTO_COMPACT_WINDOW=400000,\n"
        "  AUTOCOMPACT_PCT_OVERRIDE=75)?\n"
        "  NOTE: our field test found this INCREASED trivial mistakes\n"
        "  on heavy-refactor workflows. Recommended only if you already\n"
        "  tested it successfully. [y/N]: "
    ).strip().lower()
    bcherny_stack = ans == "y"

    if not (warmup_fix or bcherny_stack):
        print("  Nothing to apply.")
        return

    to_set: dict[str, str] = {}
    if warmup_fix:
        to_set["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    if bcherny_stack:
        to_set["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        to_set["MAX_THINKING_TOKENS"] = "63999"
        to_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "400000"
        to_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "75"

    if dry_run:
        print(f"  [dry-run] would set in {settings_path}:")
        for k, v in to_set.items():
            print(f"    {k}={v}")
        return

    # Load / create settings.json, merge env block, back up first.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_json(settings_path) if settings_path.exists() else {}

    bak = backup_path(settings_path)
    if settings_path.exists():
        try:
            shutil.copy(settings_path, bak)
            print(f"  Backup: {bak}")
        except OSError as e:
            print(f"  [!!] Could not back up {settings_path}: {e}")
            return

    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        print(f"  [!!] Existing settings.json 'env' is not an object -- aborting.")
        return
    for k, v in to_set.items():
        env[k] = v
    _save_json(settings_path, settings)
    print(f"  Updated: {settings_path}")
    for k, v in to_set.items():
        print(f"    {k}={v}")


if __name__ == "__main__":
    raise SystemExit(main())
