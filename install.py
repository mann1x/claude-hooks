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
import platform
import shutil
import socket
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
_CONDA_PY_CACHE: dict[str, Path] = {}


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

    Cached per env_name after first successful probe — the cache used
    to be a single global, which broke
    ``find_conda_env_python('claude-hooks-consultants')`` after a prior
    call with the default ``'claude-hooks'`` had already filled the
    cache (it returned the wrong env's python). The bug shipped in
    v1.0.5-dev and silently routed the consultants pip install into
    the main env on pandorum on 2026-05-06; fix is per-env-name keying.
    """
    global _CONDA_PY_CACHE
    if not isinstance(_CONDA_PY_CACHE, dict):
        # Migrate the legacy single-Path cache to a dict keyed by env.
        _CONDA_PY_CACHE = {}
    cached = _CONDA_PY_CACHE.get(env_name)
    if cached is not None and cached.exists():
        return cached

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
            _CONDA_PY_CACHE[env_name] = c
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
                            _CONDA_PY_CACHE[env_name] = layout
                            return layout
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass

    # Last resort: return a canonical path **for the requested
    # env_name**, NOT the hardcoded main-env constant. Caller will
    # ``.exists()`` it; with env_name baked into the path, that check
    # tells the truth instead of lying when a different env exists.
    #
    # The hardcoded ``CONDA_PY_LINUX`` / ``CONDA_PY_WIN`` constants
    # caused a subtle bug on solidpc 2026-05-06: calling
    # ``find_conda_env_python("claude-hooks-consultants")`` against a
    # host that had ``claude-hooks`` (but no consultants env) returned
    # the main env's python via the fallback. ``.exists()`` was True
    # (because the main env IS installed), so ``_install_consultants``
    # decided the consultants env was already there and pip-installed
    # the heavy LangChain stack into the WRONG env. Constructing the
    # fallback from env_name fixes it: a missing env produces a
    # missing path, and the caller's exists() check correctly returns
    # False.
    home = Path.home()
    if os.name == "nt":
        return home / "anaconda3" / "envs" / env_name / "python.exe"
    return home / "anaconda3" / "envs" / env_name / "bin" / "python"

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


def _proxy_locally_installed() -> tuple[bool, str]:
    """Detect whether the claude-hooks-proxy service is installed on
    this host. Returns ``(installed, kind)`` where ``kind`` is one of
    ``"systemd"``, ``"launchd"``, ``"task"`` or ``""``.

    Checks the same install destinations the proxy stack writes to
    (systemd unit file on Linux, LaunchAgent plist on macOS, scheduled
    task on Windows). Drives the v1.6.1 ``[V]erify / [R]e-install /
    [S]kip`` re-run path in ``_setup_proxy``.
    """
    if os.name == "nt":
        if _windows_task_exists(_PROXY_TASK_NAME):
            return True, "task"
        return False, ""
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / _PROXY_LAUNCHD_FILENAME
        if plist.exists():
            return True, "launchd"
        # fall through; an admin could still ship the proxy via systemd
        # on macOS, but that's nonstandard.
    if Path("/etc/systemd/system/claude-hooks-proxy.service").exists():
        return True, "systemd"
    return False, ""


def _read_current_anthropic_base_url(settings_path: Path) -> str:
    """Return ``ANTHROPIC_BASE_URL`` currently set in settings.json's
    top-level ``env`` block (the same place
    ``_set_settings_env_vars`` writes), or '' if not set / missing /
    unreadable.
    """
    if not settings_path.exists():
        return ""
    try:
        data = _load_json(settings_path)
    except (json.JSONDecodeError, OSError):
        return ""
    env = (data.get("env") or {})
    return (env.get("ANTHROPIC_BASE_URL") or "").strip()


def _classify_proxy_url(url: str) -> str:
    """Return ``"local"`` / ``"remote"`` / ``"official"`` / ``"none"``
    for the API-proxy dialog label. ``"official"`` means the user is
    pointing straight at Anthropic with no proxy in between.
    """
    if not url:
        return "none"
    low = url.lower().rstrip("/")
    if low in ("https://api.anthropic.com", "http://api.anthropic.com"):
        return "official"
    if "127.0.0.1" in low or "localhost" in low or low.startswith("http://0.0.0.0"):
        return "local"
    return "remote"


def _verify_proxy_health(url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    """GET ``<url>/health`` (or ``<url>`` if no /health endpoint
    exists) and report status. Used by the V/r/s re-run path so users
    can confirm an installed proxy is responding without a full
    re-install.
    """
    if not url:
        return False, "no URL configured"
    from urllib.request import Request, urlopen
    health = url.rstrip("/") + "/health"
    try:
        req = Request(health, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            return resp.status < 400, f"HTTP {resp.status} — {body[:160].strip()}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


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
            shutil.copy(settings_path, backup_path(settings_path, reason="env-vars"))
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
    listen_host = proxy_cfg.get("listen_host", "127.0.0.1")
    listen_port = proxy_cfg.get("listen_port", 38080)
    local_advertise = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
    local_url = f"http://{local_advertise}:{listen_port}"

    installed_locally, kind = _proxy_locally_installed()
    current_url = _read_current_anthropic_base_url(settings_path)
    current_class = _classify_proxy_url(current_url)

    # ─── Question 1: install the proxy locally on this host? ──────────
    #
    # Two shapes, picked off the live install state:
    #   - installed already → [V]erify / [R]e-install / [S]kip (V default)
    #   - not installed     → Install the API proxy locally? [y/N]
    #
    # "Install" is the right verb here — this question is ONLY about
    # the local service. Whether the host actually routes traffic
    # through any proxy is decided by question 2 below.
    install_locally = False
    if installed_locally:
        print(f"\n  Local proxy: installed ({kind})")
        choice = input(
            "  [V]erify / [R]e-install / [S]kip? [V/r/s]: "
        ).strip().lower() or "v"
        if choice in ("v", "verify", "y", "yes"):
            ok, msg = _verify_proxy_health(local_url)
            print(f"  · health probe ({local_url}/health): "
                  f"{'OK' if ok else 'FAIL'} — {msg}")
            install_locally = False
            proxy_cfg["enabled"] = True  # service is on disk; reflect it
        elif choice in ("r", "reinstall", "re-install"):
            install_locally = True
            proxy_cfg["enabled"] = True
            print("  · marking for re-install (per-OS installer runs below).")
        else:  # skip
            install_locally = False
            # Don't lie about the on-disk reality: leave enabled=true
            # since the service file is there, even on skip.
            proxy_cfg["enabled"] = True
            print("  · leaving local install untouched.")
    else:
        ans = input("\n  Install the API proxy locally? [y/N]: ").strip().lower()
        install_locally = ans in ("y", "yes")
        proxy_cfg["enabled"] = install_locally
        if install_locally:
            print("  · marking for install (per-OS installer runs below).")

    # ─── Question 2: route Claude Code through a proxy on this host? ──
    #
    # Independent of Q1 — pandorum points at solidpc's proxy without
    # installing one locally. Label reflects what's already in
    # settings.json:
    #   - remote @ http://192.168.178.2:38080  (anything non-local,
    #                                           non-anthropic)
    #   - local  @ http://127.0.0.1:38080      (loopback / localhost)
    #   - no                                   (unset or official URL)
    if current_class == "remote":
        label = f"remote @ {current_url}"
        default = "Y"
    elif current_class == "local":
        label = f"local @ {current_url}"
        default = "Y"
    elif install_locally:
        # Just installed; offer to wire it through.
        label = f"will install local @ {local_url}, not wired yet"
        default = "Y"
    else:
        label = "no"
        default = "N"

    suffix = "[Y/n]" if default == "Y" else "[y/N]"
    ans = input(
        f"\n  Use the API proxy? (current: {label}) {suffix}: "
    ).strip().lower() or default.lower()
    if ans not in ("y", "yes"):
        # User explicitly said no. If they had something configured
        # before, leave it — don't silently strip ANTHROPIC_BASE_URL.
        # If they want to unset it, that's a separate manual edit.
        return

    # Yes → ask for the endpoint, with a sensible default.
    #
    # Priority:
    #   1. The currently-configured URL (if any) — re-runs keep working.
    #   2. The local URL if we just installed locally and no URL set yet.
    #   3. The local URL as a safe fallback offer.
    if current_url:
        proposed = current_url
    elif install_locally or installed_locally:
        proposed = local_url
    else:
        proposed = local_url  # last-resort hint; user can paste a LAN URL
    while True:
        url = (input(f"  Endpoint URL [{proposed}]: ").strip().rstrip("/")
               or proposed)
        if url.startswith(("http://", "https://")):
            break
        print("  Please enter a URL beginning with http:// or https://")
    _set_settings_env_vars(
        settings_path, {"ANTHROPIC_BASE_URL": url}, dry_run=dry_run,
    )
    print(f"  · ANTHROPIC_BASE_URL = {url}")


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
_PGVECTOR_BACKUP_UNITS = (
    "claude-hooks-pgvector-backup.service",
    "claude-hooks-pgvector-backup.timer",
    "claude-hooks-pgvector-backup-check.service",
    "claude-hooks-pgvector-backup-check.timer",
)


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


def _install_pgvector_backup_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the pgvector daily-backup timer when the pgvector
    provider is enabled.

    Linux + Docker only. Idempotent. The script and timer assume the
    container is named ``mcp-pgvector`` (override via
    ``systemctl edit`` after install). Backups land in
    ``/shared/config/mcp-pgvector/backups/`` (resolved through any
    symlinks at install time).
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    pgvector_cfg = (cfg.get("providers") or {}).get("pgvector") or {}
    if not pgvector_cfg.get("enabled", False):
        return

    missing = [
        u for u in _PGVECTOR_BACKUP_UNITS
        if not (Path("/etc/systemd/system") / u).exists()
    ]
    if not missing:
        return

    print("\n==> claude-hooks-pgvector-backup systemd units")
    print(f"  Missing: {', '.join(missing)}")
    print(f"  Will install to /etc/systemd/system/ with __REPO_PATH__ = {HERE}")
    print("  Default schedule: daily at 01:17 local; retain 7 daily / 4 weekly / 3 monthly.")
    print("  Includes weekly canary (Mon 02:43) that runs pg_restore -l + full-read on each tier.")
    if dry_run:
        print("  [dry-run] skipping write.")
        return
    if non_interactive:
        print("  --non-interactive: proceeding.")
    else:
        ans = input(
            "  Install pgvector backup timer? [Y/n]: ",
        ).strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    repo_path = str(HERE.resolve())
    home_path = str(Path.home())
    wrote: list[str] = []
    for unit in _PGVECTOR_BACKUP_UNITS:
        src = HERE / "systemd" / unit
        dest = Path("/etc/systemd/system") / unit
        if dest.exists():
            print(f"  · {unit} already installed -- leaving as-is")
            continue
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
        wrote.append(unit)
        print(f"  + wrote {unit}")

    if not wrote:
        return
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    for u in wrote:
        if not u.endswith(".timer"):
            continue
        rc = subprocess.run(
            ["systemctl", "enable", "--now", u],
            capture_output=True, text=True,
        )
        if rc.returncode == 0:
            print(f"  · enabled + started {u}")
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


def _ensure_axon_registry_dir(*, dry_run: bool) -> bool:
    """Ensure ``~/.axon/repos/`` exists.

    The axon-host unit declares ``ReadWritePaths=/root/.axon`` so
    systemd can bind-mount it into the service's namespace. If the
    directory is missing at unit start, systemd fails namespace
    setup with ``status=226/NAMESPACE`` and the unit crash-loops
    every 5s (the ``Restart=on-failure`` cadence). Caught
    2026-05-07 after a host restart left ``/root/.axon`` missing.

    The ``repos/`` subdir is the registry path axon's host walks at
    startup to enumerate served projects.

    Returns True on success, False if creation failed (e.g.
    permission). dry-run prints the would-do without writing.
    """
    base = Path.home() / ".axon"
    repos = base / "repos"
    if repos.is_dir():
        return True
    if dry_run:
        print(f"  [dry-run] Would: mkdir -p {repos}")
        return True
    try:
        repos.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  [!!] Failed to create {repos}: {e}")
        return False
    print(f"  + ensured {repos} exists (axon registry path)")
    return True


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

    Two pre-flight checks added 2026-05-07 (post-loop incident):
      * ``_ensure_axon_registry_dir`` mkdir's ``/root/.axon/repos/``
        before enable; missing dir blocks systemd's namespace
        bind-mount.
      * ``_ensure_axon_deps`` probes the env's axon import surface
        and pip-installs ``requirements-axon.txt`` when anything
        (uvicorn / sse-starlette / pydantic-settings / httpx-sse)
        has drifted out of the env.
    """
    if os.name == "nt":
        return
    if not Path("/etc/systemd/system").is_dir():
        return
    companions_cfg = (cfg.get("companions") or cfg.get("hooks", {}).get("companions") or {})
    axon_host_cfg = (companions_cfg.get("axon_host") or {})
    if not axon_host_cfg.get("enabled", False):
        return

    print("\n==> axon-host systemd unit")

    # Pre-flight 1 — ensure the registry dir exists. If we skip this
    # and the dir is missing, systemd fails namespace setup
    # (status=226/NAMESPACE) on every restart attempt.
    if not _ensure_axon_registry_dir(dry_run=dry_run):
        print("  Skipped: registry dir setup failed.")
        return

    # Pre-flight 2 — ensure the env has axon's runtime deps. Without
    # this, the unit boots, ModuleNotFoundError-crashes on import,
    # and Restart=on-failure loops the service forever.
    if not _ensure_axon_deps(
        non_interactive=non_interactive, dry_run=dry_run,
    ):
        print("  Skipped: axon dep check failed. Fix env then re-run.")
        return

    axon_bin = shutil.which("axon")
    if axon_bin is None:
        # Fall back to the conda env claude-hooks itself uses, mirroring
        # the bin/claude-hook shim's lookup.
        candidate = Path.home() / "anaconda3" / "envs" / "claude-hooks" / "bin" / "axon"
        if candidate.is_file():
            axon_bin = str(candidate)
    if axon_bin is None:
        print("  axon binary not found on PATH or in ~/anaconda3/envs/claude-hooks/bin/")
        print("  Skipped. Run `pip install -r requirements-axon.txt` and re-run install.py.")
        return

    src = HERE / "systemd" / _AXON_HOST_UNIT
    dest = Path("/etc/systemd/system") / _AXON_HOST_UNIT
    if dest.exists():
        # Pre-flight checks above already ran (registry dir + deps),
        # so an existing unit is now safe to leave alone. Idempotent.
        print(f"  · unit already at {dest} — pre-flight ran, leaving as-is.")
        return

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


# --------------------------------------------------------------- #
# #222 robustness helpers — port waits, orphan detection,
# stale-task pruning, __pycache__ cleanup, final state report.
#
# Each helper is platform-conditional but the public surface stays
# uniform so callers don't have to branch. Tested via
# tests/test_install_robustness.py with the platform paths mocked.
# --------------------------------------------------------------- #


def _wait_for_port_free(port: int, *,
                          host: str = "127.0.0.1",
                          timeout: float = 15.0) -> bool:
    """Poll until the local TCP port is free (no LISTEN binding).

    Bridges the gap between ``schtasks /End`` (which kills the process
    but leaves the socket in TIME_WAIT for 30-60 s on Windows) and the
    follow-up ``/Run`` that tries to bind the same port. Returns True
    once the port is observed free, False on timeout.

    Implementation: rely on the same ``_addr_in_use`` semantic the
    daemon's __main__ uses — try a TCP connect; refusal means free.
    """
    import time as _time  # noqa: PLC0415
    deadline = _time.monotonic() + max(0.1, timeout)
    while _time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                pass  # something accepted → still busy
        except (ConnectionRefusedError, socket.timeout, OSError):
            return True
        _time.sleep(0.4)
    return False


def _find_claude_hooks_pythonw_processes() -> list[tuple[int, str]]:
    """Return [(pid, command_line), ...] for every running pythonw
    process whose command line touches a claude-hooks entry point.

    Windows-only. POSIX returns ``[]``. The detection is intentionally
    permissive — any claude_hooks / consultants.server / run_daemon
    match counts. The caller decides what's expected vs orphan.
    """
    if os.name != "nt":
        return []
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match 'pythonw' } | "
             "Select-Object ProcessId, CommandLine | "
             "ConvertTo-Json -Depth 2"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if ps.returncode != 0 or not ps.stdout.strip():
        return []
    try:
        data = json.loads(ps.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[tuple[int, str]] = []
    keywords = ("claude_hooks", "claude-hooks", "consultants.server",
                "run_daemon.py", "consultants_forwarder")
    for entry in data:
        cmd = (entry.get("CommandLine") or "") if isinstance(entry, dict) else ""
        pid = int(entry.get("ProcessId") or 0) if isinstance(entry, dict) else 0
        if not pid or not cmd:
            continue
        if any(k in cmd for k in keywords):
            out.append((pid, cmd))
    return out


def _kill_pids_windows(pids: list[int]) -> int:
    """Best-effort Stop-Process for a list of PIDs. Returns count killed.

    No-op on POSIX (caller checks os.name first). Errors swallowed —
    a missing PID just means the process already exited.
    """
    if os.name != "nt" or not pids:
        return 0
    killed = 0
    for pid in pids:
        rc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Stop-Process -Id {int(pid)} -Force "
             "-ErrorAction SilentlyContinue"],
            capture_output=True, text=True,
        )
        if rc.returncode == 0:
            killed += 1
    return killed


# Argv-substring keywords used by ``_force_kill_task_processes`` to
# find orphaned pythonw children of a given scheduled task. Each task
# binds one specific subprocess module / script that we can grep for
# in ``CommandLine``. Forwarder also matches engine children — the
# smart-start forwarder spawns ``consultants.server`` as a child, and
# killing the forwarder without the child leaks the engine.
_TASK_ARGV_KEYWORDS: dict[str, tuple[str, ...]] = {
    "claude-hooks-daemon": ("run_daemon.py",),
    "claude-hooks-consultants": ("consultants.server",),
    "claude-hooks-consultants-forwarder": (
        "consultants_forwarder",
        # An engine the forwarder spawned: parent (forwarder) is
        # about to die, so the engine becomes an orphan. Sweep it
        # too — otherwise the next /Run leaks (every restart adds
        # another stranded engine).
        "consultants.server",
    ),
}


def _force_kill_task_processes(task_name: str, port: int) -> None:
    """Belt-and-braces shutdown for a Windows scheduled task whose
    payload is a **windowless** ``pythonw.exe`` script.

    ``schtasks /End`` sends ``WM_CLOSE`` to the foreground window of
    the task's tracked process. Windowless pythonw has no window —
    the message has nowhere to land — so ``/End`` is a no-op against
    our daemon / forwarder / always-on tasks. Pre-fix this left
    orphans whenever the installer restarted a service:

    * ``/End`` returns 0 (schtasks thinks it worked)
    * ``_wait_for_port_free`` warns but installer continues
    * ``/Run`` spawns a fresh process — the old one keeps running

    Symptom: the 2026-05-21 v1.9.0 pandorum install left a
    duplicate ``consultants_forwarder`` PID with no port bound,
    living until reboot. Same class for the daemon — pre-#222
    surfaced the symptom but the fix only added a port-wait, not a
    hard kill.

    Sequence here:

    1. ``/End`` — gentle, in case some future payload regains a window.
    2. Brief port-free wait (up to 5 s).
    3. Find every pythonw process whose ``CommandLine`` contains any
       of the task's distinguishing keywords (see
       :data:`_TASK_ARGV_KEYWORDS`) and ``Stop-Process -Force`` them.
       This catches BOTH the port-bound primary AND any orphans.
    4. Final port-free wait so the next ``/Run`` doesn't hit
       ``EADDRINUSE``.

    POSIX no-op (Linux / macOS use systemd / launchctl which handle
    process trees natively).
    """
    if os.name != "nt":
        return

    # Step 1: gentle End. Always best-effort.
    subprocess.run(
        ["schtasks", "/End", "/TN", task_name],
        capture_output=True, text=True,
    )

    # Step 2: brief port-free wait.
    _wait_for_port_free(port, timeout=5.0)

    keywords = _TASK_ARGV_KEYWORDS.get(task_name, ())
    if not keywords:
        return  # unknown task — refuse to sweep blindly

    # Step 3: hard-kill any matched pythonw.
    procs = _find_claude_hooks_pythonw_processes()
    targets = [pid for pid, cmd in procs if any(k in cmd for k in keywords)]
    if targets:
        _kill_pids_windows(targets)
        # Step 4: another short wait so /Run doesn't race the
        # OS releasing the bound socket from the killed process.
        _wait_for_port_free(port, timeout=5.0)


# Map of opt-in service-mode → the OPPOSITE schtasks task name that
# install.py should offer to prune. Pre-#222 a host that flipped
# service modes (always-on ↔ smart-start) ended up with both tasks
# registered and both engines running.
_OPPOSITE_CONSULTANTS_TASK: dict[str, str] = {
    "always-on":   "claude-hooks-consultants-forwarder",
    "smart-start": "claude-hooks-consultants",
}


def _prune_stale_consultants_task(*, service_mode: str,
                                     non_interactive: bool,
                                     dry_run: bool) -> Optional[str]:
    """If the OPPOSITE service mode's task is still registered, offer
    to delete it. Returns the task name deleted, or None if nothing
    was done.

    Conservative-by-default: non-interactive runs report-only.
    """
    if os.name != "nt":
        return None
    other = _OPPOSITE_CONSULTANTS_TASK.get(service_mode)
    if not other or not _windows_task_exists(other):
        return None
    if dry_run:
        print(f"  [dry-run] would offer to delete stale task '{other}' "
              f"(this host now uses service_mode={service_mode!r}).")
        return None
    if non_interactive:
        print(f"  [warn] stale task '{other}' still registered (this "
              f"host now uses service_mode={service_mode!r}). Re-run "
              "interactively to clean it up, or delete manually: "
              f"schtasks /Delete /TN \"{other}\" /F")
        return None
    ans = input(
        f"  Stale task '{other}' is still registered but this host "
        f"now uses service_mode={service_mode!r}. Delete it? [Y/n]: "
    ).strip().lower()
    if ans and ans not in ("y", "yes"):
        return None
    rc = subprocess.run(
        ["schtasks", "/Delete", "/TN", other, "/F"],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  [!] failed to delete '{other}': "
              f"{rc.stderr.strip()[-200:]}")
        return None
    print(f"  · deleted stale task '{other}'")
    return other


def _clear_pycache(root: Path) -> int:
    """Walk ``root`` and ``rmtree`` every ``__pycache__`` directory.

    The 2026-05-19 pandorum diagnosis revealed that a v1.7.0 → v1.8.1
    git pull left stale ``.pyc`` files whose source mtimes matched
    pre-pull bytecode timestamps; the daemon-side ping handshake
    failed because the bytecode that was actually executed was the
    older protocol version. Clearing __pycache__ before service
    restart guarantees the post-pull bytecode is what runs.

    Returns the count of directories removed. Errors are swallowed
    (a permission-denied __pycache__ doesn't fail the install).
    """
    import shutil  # noqa: PLC0415
    removed = 0
    try:
        for d in root.rglob("__pycache__"):
            if not d.is_dir():
                continue
            try:
                shutil.rmtree(d)
                removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def _service_state_report(*, dry_run: bool,
                              cfg: Optional[dict] = None) -> None:
    """Print a final summary of what's running + what's listening.

    Intentionally non-actionable — operator gets a single glance at
    end-state to confirm everything is wired up. The accompanying
    ``[!]`` flags surface anomalies that warrant a manual followup
    (e.g. duplicate processes, missing ports, mode/runtime drift).

    #223 (2026-05-19) hardening:
    - Daemon ping uses 5 s timeout with one retry (was 1.5 s, no
      retry). Pre-#223 the daemon-restart-then-consultants-restart
      sequence sometimes left the daemon busy for >1.5 s when the
      ping fired, producing a false-negative "not responding"
      warning even though the daemon was healthy 30 s later.
    - Probe order matches the EXPECTED service mode from cfg —
      surfaces mode-vs-runtime drift (e.g. cfg says always-on but
      forwarder is running, the v1.8.2 pandorum symptom).
    """
    print("\n==> Service state")
    if dry_run:
        print("  [dry-run] would inspect running processes + listening ports.")
        return
    # Daemon ping — 5 s timeout, single retry to absorb a busy moment.
    daemon_ok = False
    try:
        from claude_hooks.daemon_client import ping  # noqa: PLC0415
        if not ping(timeout=5.0):
            time.sleep(1.0)
            daemon_ok = bool(ping(timeout=5.0))
        else:
            daemon_ok = True
    except Exception:
        daemon_ok = False
    if daemon_ok:
        print("  · claude-hooks-daemon:        responding on 127.0.0.1:47018")
    else:
        print("  [!] claude-hooks-daemon:        not responding "
              "(check ~/.claude/claude-hooks-daemon.log)")
    # Consultants engine — probe the EXPECTED port first based on cfg,
    # then fall back to the other one. If the responding port differs
    # from the expected mode, surface drift.
    expected_mode = _detect_consultants_service_mode(cfg or {})
    expected_port = (38096 if expected_mode == "smart-start" else 38095)
    other_port = 38095 if expected_port == 38096 else 38096
    expected_label = ("smart-start forwarder" if expected_mode == "smart-start"
                      else "always-on engine")
    found_port: Optional[int] = None
    try:
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415
        for port in (expected_port, other_port):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/v1/health",
                        timeout=5.0) as resp:
                    if 200 <= resp.status < 300:
                        found_port = port
                        break
            except (urllib.error.URLError, OSError):
                continue
    except Exception:
        pass
    if found_port is None:
        print(f"  [!] claude-hooks-consultants:   no service responding on "
              f"127.0.0.1:{{{expected_port},{other_port}}}")
    elif found_port == expected_port:
        print(f"  · claude-hooks-consultants:   {expected_label} "
              f"on 127.0.0.1:{found_port}")
    else:
        # Drift: cfg says one mode, a different mode answered.
        actual_label = ("smart-start forwarder" if found_port == 38096
                        else "always-on engine")
        print(f"  [!] claude-hooks-consultants:   {actual_label} "
              f"on 127.0.0.1:{found_port}, but claude-hooks.json "
              f"is configured for {expected_label}.")
        print(f"        Re-run install.py to align the service mode "
              f"or run claude-consultants config set-service-mode.")
    # Orphan / duplicate scan
    if os.name == "nt":
        procs = _find_claude_hooks_pythonw_processes()
        if procs:
            # Group by what they look like to make duplicates visible.
            from collections import Counter
            buckets = Counter()
            for _pid, cmd in procs:
                key = (
                    "consultants engine" if "consultants.server" in cmd
                    else "forwarder" if "consultants_forwarder" in cmd
                    else "daemon" if "run_daemon" in cmd
                    else "other"
                )
                buckets[key] += 1
            extra = {k: v for k, v in buckets.items() if v > 1}
            if extra:
                print("  [!] duplicate processes detected:")
                for k, n in extra.items():
                    print(f"        {k}: {n} instances")
                print("        re-run install.py interactively to "
                      "clean them up, or kill manually via Task Manager.")


def _restart_managed_services(*, dry_run: bool, skip: bool,
                                  cfg: Optional[dict] = None) -> None:
    """End-of-install hook (v1.5.2+): restart the long-lived
    ``claude-hooks-daemon`` and the always-on
    ``claude-hooks-consultants`` engine so they pick up code that was
    pulled in this install pass.

    Without this, a ``git pull && python install.py`` cycle leaves
    the running pythonw / python process executing whatever bytecode
    was loaded at its original spawn — newly-pulled modules sit on
    disk but are never imported until the host reboots or someone
    runs ``claude-hooks-daemon-ctl restart`` manually. The 2026-05-15
    v1.5.0 deploy exposed this gap when the daemon kept answering
    pings on the v1.4 protocol after install.py declared success.

    Skips silently when:
      - ``dry_run`` is set (planning mode)
      - ``skip`` is set (the ``--skip-daemon-restart`` escape hatch)
      - the service isn't installed on this host
      - the service isn't currently running
    """
    if dry_run or skip:
        if skip and not dry_run:
            print("\n==> Service restart")
            print("  --skip-daemon-restart: leaving claude-hooks-daemon "
                  "and claude-hooks-consultants alone.")
            print("  Restart manually to pick up new code: "
                  "claude-hooks-daemon-ctl restart")
        return
    print("\n==> Restarting managed services to load new code")
    _restart_claude_hooks_daemon()
    _restart_consultants_service(cfg=cfg)


def _restart_claude_hooks_daemon() -> None:
    """Restart the claude-hooks-daemon process on whichever platform
    manages it. No-op when the service isn't installed.

    #222 (2026-05-19) hardening:
    - Wait for port 47018 to actually release after ``schtasks /End``
      before ``/Run``. Pre-#222 the End→Run sequence raced the
      OS TIME_WAIT window and the new daemon hit "already in use",
      exited 1, then the task's RestartOnFailure retried 60 s later —
      well past install.py's 20 s health-wait, producing a confusing
      "restarted but not responding" message.
    - Bump health-wait from 20 s to 60 s on Windows so the retry path
      lands within the install.py session, not after the user has
      walked away wondering whether the install succeeded.
    """
    plat = sys.platform
    if plat == "win32":
        if not _windows_task_exists(_DAEMON_TASK_NAME):
            print("  claude-hooks-daemon: not installed (no scheduled task)"
                  " — skipping restart")
            return
        # Hard-kill existing instances. ``schtasks /End`` alone is a
        # no-op against windowless pythonw (no WM_CLOSE target), which
        # used to leave the new /Run racing the old process for port
        # 47018 — see :func:`_force_kill_task_processes`.
        #
        # We deliberately do NOT print a "port still held" warning
        # here even though ``_wait_for_port_free`` may return False:
        # the task's ``RestartOnFailure`` policy can re-spawn the
        # daemon faster than our wait window, so the LISTEN we
        # observe is the NEW daemon binding, not the old one. The
        # subsequent ``_wait_for_daemon`` call is the canonical "did
        # it come back up" gate — if that fails we surface it with a
        # specific message; if it succeeds, the early port-held
        # warning was a false alarm and would only confuse users.
        _force_kill_task_processes(_DAEMON_TASK_NAME, port=47018)
        rc = subprocess.run(["schtasks", "/Run", "/TN", _DAEMON_TASK_NAME],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            print(f"  claude-hooks-daemon: schtasks /Run failed:"
                  f" {rc.stderr.strip()[-200:]}")
            return
        # Generous wait — covers schtasks RestartOnFailure (60 s
        # interval) so a transient race still surfaces a clean OK.
        timeout = 60.0
    elif plat == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-hooks.daemon.plist"
        if not plist.exists():
            print("  claude-hooks-daemon: not installed (no LaunchAgent)"
                  " — skipping restart")
            return
        subprocess.run(["launchctl", "unload", str(plist)],
                       capture_output=True)
        # Same TIME_WAIT race exists on macOS, though shorter.
        _wait_for_port_free(47018, timeout=5.0)
        subprocess.run(["launchctl", "load", "-w", str(plist)],
                       capture_output=True)
        timeout = 20.0
    else:
        unit_path = Path("/etc/systemd/system") / _DAEMON_UNIT
        if not unit_path.exists():
            print("  claude-hooks-daemon: not installed (no systemd unit)"
                  " — skipping restart")
            return
        rc = subprocess.run(["systemctl", "restart", _DAEMON_UNIT],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            print(f"  claude-hooks-daemon: systemctl restart failed: "
                  f"{rc.stderr.strip()[-200:]}")
            return
        timeout = 20.0
    # Verify the daemon came back. Cold-start can take a few seconds
    # (secret-file creation, port bind, embedding-manager init).
    if _wait_for_daemon(timeout=timeout):
        print(f"  claude-hooks-daemon: restarted + responding on 127.0.0.1:47018")
    else:
        print(f"  [!!] claude-hooks-daemon: restarted but not responding "
              f"within {timeout:.0f} s. Check logs at "
              f"~/.claude/claude-hooks-daemon.log")


def _detect_consultants_service_mode(cfg: dict) -> str:
    """Determine the currently-configured consultants service mode.

    Reads ``hooks.consultants.smart_start.enabled`` from
    ``config/claude-hooks.json`` (the canonical installer-side
    record). Returns ``"smart-start"`` if enabled, ``"always-on"``
    otherwise — matching the default service-mode prompt branch in
    ``_setup_consultants_engine``.

    Used by the post-install restart logic so it talks to the RIGHT
    schtasks task + port, not the always-on defaults. Pre-#223 the
    restart code was always-on-only and produced a 15 s health-check
    timeout on smart-start hosts.
    """
    if not isinstance(cfg, dict):
        return "always-on"
    smart = (cfg.get("hooks", {})
                .get("consultants", {})
                .get("smart_start", {}))
    return "smart-start" if smart.get("enabled") else "always-on"


def _consultants_restart_target(service_mode: str) -> tuple[str, int, str]:
    """Return ``(schtasks_task_name, health_port, friendly_label)``
    for the given service mode.

    The forwarder lives on 38096 and lazily spawns the engine on an
    ephemeral port; its own health endpoint serves through the same
    /v1/health proxy. Always-on engine listens on 38095 directly.
    """
    if service_mode == "smart-start":
        return (_CONSULTANTS_FORWARDER_TASK_NAME, 38096,
                "smart-start forwarder")
    return (_CONSULTANTS_TASK_NAME, 38095, "always-on engine")


def _restart_consultants_service(*, cfg: Optional[dict] = None) -> None:
    """Restart the claude-hooks-consultants engine if installed.

    The consultants engine has two service modes (``always-on`` and
    ``smart-start``); install.py registers ONE task per host based on
    the user's choice. #223 (2026-05-19) makes this function read
    the chosen mode from ``cfg`` and target the right task + port —
    pre-#223 it was always-on-only and hit a 15 s timeout on every
    smart-start host's restart cycle.

    Timeouts also bumped (60 s health-check) so a cold LangGraph
    import doesn't false-negative. Pass ``cfg=None`` to fall back to
    reading the file directly (used by main() and by tests).
    """
    # Resolve service mode + target task + port from cfg.
    if cfg is None:
        try:
            cfg_path = HERE / "config" / "claude-hooks.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    service_mode = _detect_consultants_service_mode(cfg)
    task_name, port, label = _consultants_restart_target(service_mode)

    plat = sys.platform
    if plat == "win32":
        if not _windows_task_exists(task_name):
            print(f"  claude-hooks-consultants: not installed "
                  f"({label}, task '{task_name}' missing) — "
                  "skipping restart")
            return
        # Hard-kill prior instances (+ any orphaned engine children
        # the forwarder spawned). schtasks /End alone is a no-op
        # against windowless pythonw — see
        # :func:`_force_kill_task_processes`.
        _force_kill_task_processes(task_name, port=port)
        rc = subprocess.run(["schtasks", "/Run", "/TN", task_name],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            print(f"  claude-hooks-consultants: schtasks /Run failed:"
                  f" {rc.stderr.strip()[-200:]}")
            return
    else:
        # Linux + macOS: systemd --user unit (always-on only on POSIX).
        unit = "claude-hooks-consultants.service"
        rc = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            print("  claude-hooks-consultants: not installed (no systemd "
                  "--user unit) — skipping restart")
            return
        rc = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            print(f"  claude-hooks-consultants: systemctl --user restart "
                  f"failed: {rc.stderr.strip()[-200:]}")
            return
    # #223: bumped 15 s → 60 s to cover the consultants engine's
    # LangGraph cold-import cost (10-30 s on Windows).
    if _wait_for_consultants_health(port, timeout=60.0):
        print(f"  claude-hooks-consultants: restarted + responding on "
              f"127.0.0.1:{port} ({label})")
    else:
        print(f"  [!!] claude-hooks-consultants: restarted but not "
              f"responding within 60 s on 127.0.0.1:{port}/v1/health "
              f"({label}). Check ~/.claude/claude-hooks-consultants.log")


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


_PGVECTOR_VERIFY_SCRIPT = r"""
import json, sys
try:
    import psycopg
except ImportError as e:
    print(json.dumps({"ok": False,
                       "reason": "psycopg not importable: " + str(e)}))
    sys.exit(0)
try:
    with psycopg.connect(sys.argv[1], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            if cur.fetchone() is None:
                print(json.dumps({"ok": False,
                                   "reason": "pgvector extension not "
                                             "installed in target DB"}))
                sys.exit(0)
    print(json.dumps({"ok": True, "reason": "ok"}))
except Exception as e:
    print(json.dumps({"ok": False,
                       "reason": type(e).__name__ + ": " + str(e)}))
"""


def _verify_pgvector_dsn(dsn: str) -> tuple[bool, str]:
    """Probe Postgres for the pgvector extension. Returns (ok, reason).

    Uses the conda env's python via subprocess when psycopg isn't
    importable in the current interpreter. install.py is often
    invoked with whatever ``python`` is on PATH (system py3,
    sometimes a different env), and pinning the verify call to the
    claude-hooks env's python — where psycopg IS installed — gives
    accurate reachability info instead of a misleading "FAILED".
    """
    # Fast path: psycopg available in this interpreter.
    try:
        import psycopg  # type: ignore  # noqa: F401, PLC0415
        from claude_hooks.providers.pgvector import PgvectorProvider  # noqa: PLC0415
        from claude_hooks.providers import ServerCandidate  # noqa: PLC0415
        candidate = ServerCandidate(server_key="pgvector", url=dsn,
                                    source="installer", confidence="manual")
        if PgvectorProvider.verify(candidate):
            return True, "ok"
        return False, "verify returned false (see logs)"
    except ImportError:
        pass

    # Fallback: shell out to the conda env's python where psycopg
    # is expected to be installed.
    conda_py = find_conda_env_python()
    if not conda_py.exists():
        return False, ("psycopg not in current python AND "
                       f"conda env not found at {conda_py}")

    try:
        rc = subprocess.run(
            [str(conda_py), "-c", _PGVECTOR_VERIFY_SCRIPT, dsn],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"subprocess failed: {e}"

    out = (rc.stdout or "").strip().splitlines()
    if not out:
        return False, (f"verify subprocess produced no output "
                       f"(rc={rc.returncode}, stderr={rc.stderr.strip()[:200]})")
    try:
        result = json.loads(out[-1])
    except json.JSONDecodeError:
        return False, f"verify subprocess output not JSON: {out[-1][:200]}"
    return bool(result.get("ok")), str(result.get("reason") or "")


def _validate_pgvector_only(cfg: dict) -> None:
    """Validate-only path for pgvector (v1.5.4+): probe Postgres +
    pgvector extension + embedder reachability without touching the
    config, the launcher script, or ``~/.claude.json``.

    Mirrors the read-only diagnostic shape of
    :func:`_validate_qdrant_embedding` and
    :func:`_validate_memory_kg_embedding`. The point is to give the
    user a confidence check on an already-working install without the
    side effects of a full re-install pass (idempotent in theory, but
    every write is a chance for a transient failure or a partial
    re-write).
    """
    pcfg = (cfg.get("providers") or {}).get("pgvector") or {}
    dsn = pcfg.get("dsn") or ""
    if not dsn:
        print("  [!!] No DSN in config — can't validate.")
        return

    print("  Probing Postgres + pgvector extension...", end=" ", flush=True)
    ok, reason = _verify_pgvector_dsn(dsn)
    print("OK" if ok else f"FAILED ({reason})")
    if not ok:
        print(f"  Couldn't reach pgvector: {reason}")
        return

    embedder_opts = pcfg.get("embedder_options") or {}
    embed_url = embedder_opts.get("url") or ""
    model = embedder_opts.get("model") or ""
    embedder = pcfg.get("embedder") or "ollama"
    if embedder == "llamafile":
        # llamafile lives in one of two modes, exactly like
        # _validate_sqlite_vec_only does it: local (daemon-managed,
        # daemon_ensure=True) where this host's daemon spawns it on
        # demand, or remote (daemon_ensure=False) where some other
        # host serves /embedding on the LAN. Surface the difference
        # so the user can tell which one their config selected.
        ensure = embedder_opts.get("daemon_ensure", True)
        suffix = (" (daemon-managed)" if ensure
                  else " (remote, no local supervision)")
        print(f"  Embedder: llamafile{suffix} @ {embed_url}")
        if ensure:
            print("  Note: daemon spawns llamafile on demand; URL may be "
                  "unbound until first recall.")
    elif model and embed_url:
        base = _ollama_base_from_embed_url(embed_url)
        print(f"  Probing Ollama at {base} for {model}...", end=" ", flush=True)
        present = _ollama_model_present(base, model)
        print("present" if present else "missing")
        if not present:
            print(f"  Run `ollama pull {model}` against {base} to repair.")
    else:
        print("  Embedder options incomplete in config — skipping embedder probe.")

    launcher = _pgvector_launcher_path()
    print(f"  Launcher: {launcher} "
          f"({'present' if launcher.exists() else 'MISSING'})")
    print("  pgvector: validate-only complete. No writes performed.")


def _validate_sqlite_vec_only(cfg: dict) -> None:
    """Validate-only path for sqlite_vec (#237, 2026-05-21).

    Mirror of :func:`_validate_pgvector_only`: probes the db file +
    schema version + embedder reachability + launcher presence without
    touching the config, the launcher script, or ``~/.claude.json``.
    Lets a re-run against a healthy sqlite_vec install confirm
    everything is wired without the side effects of a full re-install
    (which would re-write the launcher, re-register the MCP entry,
    re-run the embedder dialog).

    Pre-v1.7 sqlite_vec had no schema bookkeeping, so the version
    probe is best-effort: a db that lacks the ``claude_hooks_schema``
    table is reported as ``pre-v1.7`` (the next ``store()`` / ``recall()``
    call will lazily migrate it). v1.7+ dbs report the integer version
    written into the bookkeeping row.
    """
    pcfg = (cfg.get("providers") or {}).get("sqlite_vec") or {}
    db_path = pcfg.get("db_path") or ""
    if not db_path:
        print("  [!!] No db_path in config — can't validate.")
        return
    expanded = os.path.expanduser(db_path)

    print(f"  db_path      : {expanded}")
    db_file = Path(expanded)
    if not db_file.exists():
        print(f"  [!!] db file does not exist; nothing to validate. "
              f"Run a full re-install or write the first recall/store "
              f"to lazily create it.")
        return
    print(f"  db file size : {db_file.stat().st_size:,} bytes")

    # Best-effort schema-version probe. Stays read-only — we never
    # open the file write-mode here so concurrent recall pipelines
    # aren't disturbed.
    try:
        import sqlite3  # noqa: PLC0415
        conn = sqlite3.connect(f"file:{expanded}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='claude_hooks_schema'"
            )
            if cur.fetchone() is None:
                print("  schema version: pre-v1.7 (lazy migration runs on "
                      "next store/recall)")
            else:
                row = conn.execute(
                    "SELECT version FROM claude_hooks_schema "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    print(f"  schema version: v{row[0]}")
                else:
                    print("  schema version: bookkeeping table empty (no rows)")
        finally:
            conn.close()
    except Exception as e:
        print(f"  [!] could not read schema version: {e}")

    # Embedder probe — same dispatch table as the pgvector validator
    # so the user sees consistent messages across both providers.
    embedder_opts = pcfg.get("embedder_options") or {}
    embed_url = embedder_opts.get("url") or ""
    model = embedder_opts.get("model") or ""
    embedder = pcfg.get("embedder") or "ollama"
    if embedder == "llamafile":
        ensure = embedder_opts.get("daemon_ensure", True)
        suffix = (" (daemon-managed)" if ensure
                  else " (remote, no local supervision)")
        print(f"  Embedder     : llamafile{suffix} @ {embed_url}")
        if ensure:
            print("  Note: daemon spawns llamafile on demand; URL may be "
                  "unbound until first recall.")
    elif model and embed_url:
        base = _ollama_base_from_embed_url(embed_url)
        print(f"  Probing Ollama at {base} for {model}...", end=" ", flush=True)
        present = _ollama_model_present(base, model)
        print("present" if present else "missing")
        if not present:
            print(f"  Run `ollama pull {model}` against {base} to repair.")
    else:
        print("  Embedder options incomplete in config — skipping embedder probe.")

    launcher = _sqlite_vec_launcher_path()
    print(f"  Launcher     : {launcher} "
          f"({'present' if launcher.exists() else 'MISSING'})")
    print("  sqlite_vec: validate-only complete. No writes performed.")


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
    existing_enabled = bool(pcfg.get("enabled", False))
    launcher_path = _pgvector_launcher_path()
    launcher_present = launcher_path.exists()
    fully_configured = bool(
        existing_dsn and existing_enabled and launcher_present
    )

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
    elif fully_configured:
        # v1.5.4+: when pgvector is already fully configured (DSN +
        # enabled + launcher dropped), offer a validate-only path
        # instead of forcing a full re-install. Matches the
        # _validate_qdrant_embedding / _validate_memory_kg_embedding
        # pattern from v1.4. Default V so the lowest-impact action
        # is the easy one.
        print(f"  Currently configured: enabled, DSN set, launcher at")
        print(f"  {launcher_path}")
        choice = input(
            "  [V]alidate only / [R]e-install / [S]kip? [V/r/s]: "
        ).strip().lower()
        if not choice:
            choice = "v"
        if choice in ("s", "skip", "n", "no"):
            print("  Skipped.")
            return
        if choice in ("v", "validate", "y", "yes"):
            # Yes/y maps to validate here because the most natural
            # "yes I want this" answer for an already-working install
            # is "yes, confirm it's working" — not "yes, redo it".
            _validate_pgvector_only(cfg)
            return
        # Anything else (r / re-install / "reinstall") falls through
        # to the full setup path below.
        ans = "y"
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
    print("  Probing Postgres + pgvector extension...", end=" ", flush=True)
    ok, reason = _verify_pgvector_dsn(dsn)
    print("OK" if ok else f"FAILED ({reason})")
    if not ok:
        print(f"  Couldn't reach pgvector with that DSN: {reason}")
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
    cfg["providers"]["pgvector"].setdefault("recall_k", 5)
    cfg["providers"]["pgvector"].setdefault("store_mode", "auto")
    # Embedder choice: drives the Ollama / OpenAI-compat / llamafile
    # dialog. v1.4 replaces the hard-coded ``embedder_options``
    # default with this interactive flow so users can pick a
    # llamafile fallback, run llamafile-only, or stick with Ollama.
    if not cfg["providers"]["pgvector"].get("embedder"):
        _setup_embedding_engine(
            cfg, provider="pgvector",
            non_interactive=non_interactive, dry_run=dry_run,
        )

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


# ===================================================================== #
# v1.4: embedding-engine setup (Ollama / OpenAI-compat / llamafile +
# composite-fallback). Drives both pgvector and sqlite_vec via the
# same dialog (parameterised by provider name); the underlying
# embedder lives in claude_hooks/embedders.py and the daemon-side
# llamafile lifecycle is in claude_hooks/embedding_manager.py.
# ===================================================================== #


# Default composite asset shipped with each v1.4+ release. The SHA is
# committed alongside install.py and verified at download time.
_DEFAULT_LLAMAFILE_RELEASE_TAG = "v1.4.0"
_DEFAULT_LLAMAFILE_ASSET = "qwen3-embedding-0.6b-16k.llamafile"
_DEFAULT_LLAMAFILE_MODEL = "qwen3-embedding:0.6b"
_DEFAULT_LLAMAFILE_CTX = 16384
_LLAMAFILE_SHA_FILE = HERE / "vendor" / "llamafile" / "dist" / "SHA256SUMS.composite"
_LLAMAFILE_DIST_DIR = HERE / "vendor" / "llamafile" / "dist"


def _read_committed_composite_sha() -> str:
    """Return the SHA256 committed for the default composite asset, or
    ``""`` if the file is absent (typical until the v1.4.0 release
    cut). Format mirrors ``sha256sum`` output: ``<hex>  <filename>``.
    """
    try:
        text = _LLAMAFILE_SHA_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith(_DEFAULT_LLAMAFILE_ASSET):
            return parts[0].lower()
        if len(parts) == 1:
            return parts[0].lower()
    return ""


def _gguf_magic_ok(path: str) -> bool:
    """Cheap sanity check: a GGUF file starts with the ASCII bytes
    ``GGUF``. Used at install time to fail fast on a typo'd custom
    GGUF path before we waste time wiring it into the config."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


def _verify_sha256(path: Path, expected_hex: str) -> bool:
    """Stream-verify ``path``'s SHA256 against ``expected_hex``. Used by
    the composite-asset downloader. Returns False on any read error so
    the caller can surface a clean reinstall-needed message."""
    import hashlib as _h
    h = _h.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest().lower() == expected_hex.lower()


def _download_composite_llamafile(
    target: Path,
    *,
    tag: str = _DEFAULT_LLAMAFILE_RELEASE_TAG,
    asset: str = _DEFAULT_LLAMAFILE_ASSET,
    sha256: str = "",
    repo: str = "mann1x/claude-hooks",
    dry_run: bool = False,
) -> bool:
    """Fetch the composite llamafile from a GitHub Release asset.

    Tries ``gh release download`` first (preserves SHA via the
    --output-clobber convention) and falls back to ``urllib.request``
    against the public release URL. After download, verifies SHA when
    one is provided; aborts (and removes the partial file) if it
    doesn't match.

    Returns True on success, False on any failure. The caller is
    responsible for surfacing the error to the user; this function
    only logs to stdout.
    """
    if dry_run:
        print(f"  [dry-run] Would fetch {asset} from {repo}@{tag} -> {target}")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    # Prefer ``gh`` when present — it handles auth + redirects cleanly.
    use_gh = shutil.which("gh") is not None if "shutil" in globals() else False
    if not use_gh:
        import shutil as _sh
        use_gh = _sh.which("gh") is not None

    tmp = target.with_suffix(target.suffix + ".part")
    if tmp.exists():
        tmp.unlink()

    ok = False
    if use_gh:
        try:
            rc = subprocess.run(
                ["gh", "release", "download", tag,
                 "--repo", repo,
                 "--pattern", asset,
                 "--dir", str(target.parent),
                 "--output", asset,
                 "--clobber"],
                check=False, capture_output=True, text=True,
            )
            ok = (rc.returncode == 0)
            if not ok:
                print(f"  gh release download failed: "
                      f"{rc.stderr.strip()[:200] or rc.stdout.strip()[:200]}")
        except OSError as e:
            print(f"  gh invocation failed: {e}")
    if not ok:
        # Fallback: direct HTTPS download.
        import urllib.request as _u
        url = (f"https://github.com/{repo}/releases/download/{tag}/{asset}")
        print(f"  Downloading {url}")
        try:
            with _u.urlopen(url, timeout=300) as r, open(tmp, "wb") as fout:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
            tmp.replace(target)
            ok = True
        except Exception as e:
            print(f"  download failed: {e}")
            if tmp.exists():
                tmp.unlink()
            ok = False

    if not ok:
        return False

    if sha256:
        print(f"  Verifying SHA256...", end=" ", flush=True)
        if not _verify_sha256(target, sha256):
            print("FAILED")
            print(f"  Removing corrupted artifact at {target}")
            try:
                target.unlink()
            except OSError:
                pass
            return False
        print("OK")
    try:
        os.chmod(target, 0o755)
    except OSError:
        pass
    return True


def _setup_llamafile_engine(
    cfg: dict, *,
    non_interactive: bool,
    dry_run: bool,
    propose_model: str = _DEFAULT_LLAMAFILE_MODEL,
    propose_ctx: int = _DEFAULT_LLAMAFILE_CTX,
) -> dict:
    """Walk the user through llamafile-specific knobs and return a
    dict ready to merge into ``cfg["embedding"]``.

    Dialog:
      1. Default settings (qwen3-embedding-0.6b, 16k ctx, auto GPU)
         or custom (GGUF path + ctx).
      2. Mode: auto (try GPU, transparent CPU fallback) vs cpu only.
      3. Composite-asset download / custom-GGUF compose (in
         non-dry-run mode).

    The returned dict has the EmbeddingConfig-compatible shape::

        {"enabled": True, "llamafile_path": "...",
         "model_gguf": "" | "/path/to/custom.gguf",
         "port": 38092, "ctx_size": N, "pooling": "last",
         "mode": "auto" | "cpu", "idle_timeout_seconds": 300}
    """
    existing = cfg.get("embedding") or {}
    target_default = _LLAMAFILE_DIST_DIR / _DEFAULT_LLAMAFILE_ASSET

    print("\n  llamafile engine settings:")
    if non_interactive:
        use_default = True
        print("    --non-interactive: keeping default model + 16k ctx.")
    else:
        ans = input(
            f"    Use default settings ({propose_model}, "
            f"{propose_ctx // 1024}k ctx)? [Y/n]: "
        ).strip().lower() or "y"
        use_default = ans in ("y", "yes")

    if use_default:
        llamafile_path = str(target_default)
        model_gguf = ""
        ctx_size = propose_ctx
        if not target_default.is_file():
            sha = _read_committed_composite_sha()
            if not sha:
                print(f"    No committed SHA at {_LLAMAFILE_SHA_FILE} — "
                      "skipping pre-fetch. The daemon will surface a clean "
                      "error on first embed; build the composite manually "
                      "with `make -C vendor/llamafile/dist`.")
            else:
                print(f"    Fetching composite from "
                      f"{_DEFAULT_LLAMAFILE_RELEASE_TAG}...")
                ok = _download_composite_llamafile(
                    target_default, sha256=sha, dry_run=dry_run,
                )
                if not ok:
                    print("    Composite fetch failed. Edit the config "
                          "manually or re-run install.py once the GH "
                          "Release is reachable.")
    else:
        gguf = ""
        while not gguf:
            existing_gguf = existing.get("model_gguf") or ""
            prompt = (f"    Path to custom embedding GGUF"
                      f"{' [' + existing_gguf + ']' if existing_gguf else ''}: ")
            gguf = input(prompt).strip() or existing_gguf
            if not gguf:
                print("    Path required.")
                continue
            gguf = os.path.expanduser(gguf)
            if not os.path.isfile(gguf):
                print(f"    Not a file: {gguf}")
                gguf = ""
                continue
            if not _gguf_magic_ok(gguf):
                print(f"    Not a GGUF (magic mismatch): {gguf}")
                gguf = ""
                continue
        # Custom-context prompt.
        try:
            raw = input(
                f"    Context size [{existing.get('ctx_size') or propose_ctx}]: "
            ).strip()
            ctx_size = int(raw) if raw else (existing.get("ctx_size") or propose_ctx)
        except ValueError:
            ctx_size = propose_ctx
        # The slim binary is the same fat binary as the composite —
        # we just don't bake a GGUF into it. For v1.4 we point at the
        # composite path as a fallback (it can still run an external
        # GGUF via -m). A future revision will fetch the slim binary
        # separately.
        llamafile_path = str(target_default)
        model_gguf = gguf

    # Mode (auto / cpu).
    if non_interactive:
        mode = existing.get("mode") or "auto"
        print(f"    --non-interactive: GPU mode = {mode}.")
    else:
        from claude_hooks import gpu_probe
        gpu = gpu_probe.probe()
        hint = (f"detected {gpu['vendor']} GPU"
                if gpu.get("vendor") != "none"
                else "no GPU detected; CPU recommended")
        default = "auto" if gpu.get("vendor") != "none" else "cpu"
        ans = input(
            f"    GPU mode [auto/cpu] ({hint}) [{default}]: "
        ).strip().lower() or default
        mode = "cpu" if ans in ("cpu", "c") else "auto"

    # LAN-exposure prompt (#242, 2026-05-21). Default loopback so the
    # embedder isn't exposed to the network without an explicit opt-in
    # — matches the EmbeddingConfig dataclass default and the original
    # "we don't want to expose it" guidance. Set to 0.0.0.0 to share
    # the embedder with other LAN hosts (then configure those hosts
    # with the remote-llamafile primary option in their install.py
    # runs). Existing 0.0.0.0 hosts are remembered across re-installs
    # so a re-run doesn't silently undo the LAN exposure.
    existing_host = existing.get("host") or "127.0.0.1"
    if non_interactive:
        host = existing_host
        if host != "127.0.0.1":
            print(f"    --non-interactive: keeping LAN exposure host={host}.")
        else:
            print("    --non-interactive: keeping loopback host=127.0.0.1.")
    else:
        default_lan = (existing_host != "127.0.0.1")
        default_label = "Y" if default_lan else "N"
        other_label = "n" if default_lan else "y"
        print("    Expose this llamafile on the LAN so other hosts can")
        print("    use it as a remote embedder?")
        print("    WARNING: the /embedding endpoint has NO authentication.")
        print("    Only opt in on a trusted LAN.")
        ans = input(
            f"    Bind LAN-wide (0.0.0.0)? [{default_label}/{other_label}]: "
        ).strip().lower() or default_label.lower()
        if ans in ("y", "yes"):
            host = "0.0.0.0"
            # Surface the LAN-reachable URL so the operator can paste it
            # straight into another host's config without hunting for
            # their own IP.
            try:
                import socket as _socket  # noqa: PLC0415
                _socket.setdefaulttimeout(1.0)
                hostname = _socket.gethostname()
                lan_ip = _socket.gethostbyname(hostname)
            except Exception:
                lan_ip = "<this-host-LAN-IP>"
            port = int(existing.get("port") or 38092)
            print(f"    LAN URL  : http://{lan_ip}:{port}/embedding")
            print(f"             Paste this URL into other hosts' "
                  f"`providers.<pg|sqlite_vec>.embedder_options.url`.")
        else:
            host = "127.0.0.1"

    block = {
        "enabled": True,
        "llamafile_path": llamafile_path,
        "model_gguf": model_gguf,
        "host": host,
        "port": int(existing.get("port") or 38092),
        "ctx_size": int(ctx_size),
        "pooling": "last",
        "mode": mode,
        "idle_timeout_seconds": float(existing.get("idle_timeout_seconds") or 300.0),
    }
    return block


def _setup_embedding_engine(
    cfg: dict, *,
    provider: str,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Drive the v1.4 embedding-engine dialog for ``provider`` (one of
    ``pgvector`` / ``sqlite_vec``) and write the resulting
    ``embedder`` + ``embedder_options`` keys into
    ``cfg["providers"][provider]``. Also writes the shared
    ``cfg["embedding"]`` block when llamafile is selected as primary
    or fallback.

    The dialog mirrors the user's stated structure:
      1. "Use Ollama for embeddings?" Y/n -> if Y, model + ctx,
         validate via /api/tags.
      2. If Ollama=no, offer OpenAI-compatible primary (e.g.
         text-embedding-3-small against an LM Studio endpoint).
      3. "Use llamafile as fallback?" Y/n (defaults to Y when
         Ollama=Y, mandatory when both Ollama+OpenAI=N).
      4. Llamafile sub-dialog (default vs custom GGUF, GPU mode).
    """
    cfg.setdefault("providers", {}).setdefault(provider, {})
    pcfg = cfg["providers"][provider]
    existing_options = pcfg.get("embedder_options") or {}
    existing_kind = pcfg.get("embedder") or ""

    print(f"\n  Embedding engine for {provider}:")

    # ----- 1. Ollama primary --------------------------------------
    ollama_default_url = (existing_options.get("url")
                          if existing_kind in ("ollama", "composite")
                          else "") or "http://localhost:11434/api/embeddings"
    ollama_default_model = (existing_options.get("model")
                            if existing_kind in ("ollama", "composite")
                            else "") or _DEFAULT_LLAMAFILE_MODEL
    ollama_default_ctx = int(existing_options.get("num_ctx") or 16384)

    if non_interactive:
        use_ollama = (existing_kind in ("ollama", "composite")
                      or not existing_kind)
        print(f"    --non-interactive: Ollama primary = {use_ollama}.")
    else:
        ans = input("    Use Ollama for embeddings? [Y/n]: ").strip().lower() or "y"
        use_ollama = ans in ("y", "yes")

    ollama_block: Optional[dict] = None
    openai_block: Optional[dict] = None

    if use_ollama:
        if non_interactive:
            url = ollama_default_url
            model = ollama_default_model
            num_ctx = ollama_default_ctx
        else:
            url = input(f"    Ollama URL [{ollama_default_url}]: ").strip() or ollama_default_url
            model = input(f"    Model [{ollama_default_model}]: ").strip() or ollama_default_model
            raw = input(f"    num_ctx [{ollama_default_ctx}]: ").strip()
            try:
                num_ctx = int(raw) if raw else ollama_default_ctx
            except ValueError:
                num_ctx = ollama_default_ctx
        # Best-effort validation.
        base = _ollama_base_from_embed_url(url)
        print(f"    Probing {base} for {model}...", end=" ", flush=True)
        present = _ollama_model_present(base, model)
        print("present" if present else "missing")
        if not present and not non_interactive:
            ans = input(f"    Pull {model} now? [Y/n]: ").strip().lower() or "y"
            if ans in ("y", "yes") and not dry_run:
                _ollama_pull(base, model)
        ollama_block = {
            "url": url, "model": model,
            "timeout": 30.0, "num_ctx": num_ctx, "max_chars": 30000,
        }
    else:
        # Offer remote-llamafile primary BEFORE OpenAI — #237 (2026-05-21).
        # The recall pipeline can point at a llamafile running on
        # another LAN host (the LAN-exposure prompt in
        # _setup_llamafile_engine is the producer side; this is the
        # consumer side). The embedder kind stays "llamafile" but
        # daemon_ensure is set to false so the local daemon never
        # tries to spawn a llamafile here.
        remote_url_existing = ""
        if (existing_kind == "llamafile"
                and not bool(existing_options.get("daemon_ensure", True))):
            remote_url_existing = existing_options.get("url") or ""
        if non_interactive:
            use_remote_llamafile = bool(remote_url_existing)
            if use_remote_llamafile:
                print(f"    --non-interactive: keeping remote llamafile @ "
                      f"{remote_url_existing}.")
        else:
            default_remote = "Y" if remote_url_existing else "N"
            other = "n" if remote_url_existing else "y"
            ans = input(
                f"    Use a remote llamafile endpoint as primary "
                f"(another LAN host)? [{default_remote}/{other}]: "
            ).strip().lower() or default_remote.lower()
            use_remote_llamafile = ans in ("y", "yes")

        remote_llamafile_block: Optional[dict] = None
        if use_remote_llamafile:
            default_url = remote_url_existing or "http://192.168.178.2:38092/embedding"
            if non_interactive:
                url = remote_url_existing or default_url
                timeout = float(existing_options.get("timeout") or 30.0)
            else:
                url = input(
                    f"    Endpoint URL [{default_url}]: "
                ).strip() or default_url
                raw_to = input("    Timeout seconds [30]: ").strip()
                try:
                    timeout = float(raw_to) if raw_to else 30.0
                except ValueError:
                    timeout = 30.0
            # Best-effort probe so misconfigured URLs surface here
            # rather than at the first recall. Failure is non-fatal —
            # the user may be wiring before the producer host is up.
            print(f"    Probing {url} ...", end=" ", flush=True)
            try:
                import urllib.request as _urlreq  # noqa: PLC0415
                import urllib.error as _urlerr  # noqa: PLC0415
                import json as _json  # noqa: PLC0415
                body = _json.dumps({"content": "probe"}).encode("utf-8")
                req = _urlreq.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _urlreq.urlopen(req, timeout=5.0) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                # llama.cpp emits either {"embedding":[...]} or
                # [{"embedding":[[...]]}] depending on build — both
                # carry a vector somewhere we can len() against.
                vec_len: Optional[int] = None
                if isinstance(data, list) and data:
                    em = data[0].get("embedding")
                    if isinstance(em, list):
                        if em and isinstance(em[0], list):
                            vec_len = len(em[0])
                        else:
                            vec_len = len(em)
                elif isinstance(data, dict):
                    em = data.get("embedding")
                    if isinstance(em, list):
                        vec_len = len(em)
                if vec_len:
                    print(f"OK, dim={vec_len}")
                else:
                    print("OK (vector shape not recognised; will validate at first recall)")
            except (_urlerr.URLError, OSError, ValueError) as e:
                print(f"unreachable ({e})")
                print("    Saving the URL anyway; recall will surface the "
                      "error if the endpoint is still down at runtime.")
            remote_llamafile_block = {
                "url": url,
                "timeout": timeout,
                "daemon_ensure": False,
            }
            # Skip the OpenAI branch entirely — the remote llamafile
            # IS the primary now. Also skip the local llamafile fallback
            # path further down (we don't want to spawn locally if the
            # whole point is to NOT have a local embedder).
            ollama_block = None
            openai_block = None
            # Stash the resolved block into the cfg directly so the
            # composition step below sees it as the primary.
            pcfg["embedder"] = "llamafile"
            pcfg["embedder_options"] = remote_llamafile_block
            print(f"    -> {provider}.embedder = llamafile (remote, "
                  f"daemon_ensure=false)")
            # No local embedding block — explicitly remove any stale
            # one so re-runs from a local-llamafile install converge
            # cleanly. The remote producer host owns that block.
            cfg.pop("embedding", None)
            return

        # Offer OpenAI-compatible primary as an alternative.
        if non_interactive:
            use_openai = existing_kind in ("openai", "openai_compatible")
        else:
            ans = input(
                "    Use OpenAI-compatible embeddings as primary? [y/N]: "
            ).strip().lower()
            use_openai = ans in ("y", "yes")
        if use_openai:
            existing_url = existing_options.get("url") or "https://api.openai.com/v1/embeddings"
            existing_model = existing_options.get("model") or "text-embedding-3-small"
            existing_key = existing_options.get("api_key") or "${OPENAI_API_KEY}"
            if non_interactive:
                url = existing_url
                model = existing_model
                api_key = existing_key
            else:
                url = input(f"    Endpoint URL [{existing_url}]: ").strip() or existing_url
                model = input(f"    Model [{existing_model}]: ").strip() or existing_model
                api_key = input(f"    API key (env-var ref OK) [{existing_key}]: ").strip() or existing_key
            openai_block = {
                "url": url, "model": model, "api_key": api_key, "timeout": 30.0,
            }

    # ----- 2. Llamafile fallback (or primary) ---------------------
    have_primary = bool(ollama_block or openai_block)
    if have_primary:
        if non_interactive:
            use_llamafile = True
            print("    --non-interactive: llamafile fallback = on.")
        else:
            ans = input(
                "    Use llamafile as embedding fallback? [Y/n]: "
            ).strip().lower() or "y"
            use_llamafile = ans in ("y", "yes")
    else:
        # No primary configured -> llamafile is mandatory.
        print("    No Ollama / OpenAI primary -> llamafile becomes primary.")
        use_llamafile = True

    llamafile_block: Optional[dict] = None
    if use_llamafile:
        if have_primary:
            print(
                "    Note: fallback model + ctx should match the primary "
                "to keep the recall vector space stable across failover."
            )
        embedding_block = _setup_llamafile_engine(
            cfg, non_interactive=non_interactive, dry_run=dry_run,
            propose_ctx=ollama_block["num_ctx"] if ollama_block else _DEFAULT_LLAMAFILE_CTX,
        )
        cfg["embedding"] = embedding_block
        llamafile_block = {
            "url": f"http://127.0.0.1:{embedding_block['port']}/embedding",
            "timeout": 30.0,
        }

    # ----- 3. Compose final embedder config -----------------------
    if have_primary and llamafile_block:
        # Composite: primary + llamafile fallback.
        primary_kind = "ollama" if ollama_block else "openai_compatible"
        primary_opts = ollama_block or openai_block
        pcfg["embedder"] = "composite"
        pcfg["embedder_options"] = {
            "primary": primary_kind,
            "primary_options": primary_opts,
            "fallback": "llamafile",
            "fallback_options": llamafile_block,
        }
    elif have_primary:
        # Primary-only (user declined llamafile fallback).
        if ollama_block:
            pcfg["embedder"] = "ollama"
            pcfg["embedder_options"] = ollama_block
        else:
            pcfg["embedder"] = "openai_compatible"
            pcfg["embedder_options"] = openai_block
    else:
        # llamafile-only.
        pcfg["embedder"] = "llamafile"
        pcfg["embedder_options"] = llamafile_block

    print(f"    -> {provider}.embedder = {pcfg['embedder']}")


def _validate_qdrant_embedding(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Probe the configured Qdrant MCP server (v1.4 validate-only).

    Qdrant embeds **server-side** (the FastEmbed bundle baked into
    ``mcp-server-qdrant``); claude-hooks doesn't override the model
    from the client side. All we do here is print a connectivity
    summary so the user knows the MCP is reachable and can spot a
    misconfigured URL early. Pure status — never writes to cfg,
    never prompts.

    No-op when the provider isn't enabled or has no ``mcp_url``.
    """
    pcfg = (cfg.get("providers") or {}).get("qdrant") or {}
    if not pcfg.get("enabled"):
        return
    url = pcfg.get("mcp_url") or ""
    if not url:
        return

    print("\n--- Qdrant (validate-only) ---")
    print(f"  Probing {url} ...", end=" ", flush=True)
    try:
        from claude_hooks.providers.qdrant import QdrantProvider
        candidate = ServerCandidate(
            server_key="qdrant", url=url,
            headers=pcfg.get("headers") or {},
            source="config", confidence="manual",
        )
        ok = QdrantProvider.verify(candidate, timeout=5.0)
    except Exception as e:
        print(f"FAILED ({e})")
        return
    print("OK" if ok else "no signature tools (qdrant-find/qdrant-store)")
    print("  Embedding is configured **inside** the MCP server (FastEmbed)."
          " To change the embedding model, edit your mcp-server-qdrant"
          " environment (e.g. EMBEDDING_MODEL=...) and restart the container.")


def _validate_memory_kg_embedding(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Probe the configured Memory KG MCP server (v1.4 validate-only).

    Same shape as :func:`_validate_qdrant_embedding`: the MCP server
    owns its own embedding model; this helper just surfaces
    connectivity so the user knows it's reachable. Never prompts,
    never mutates cfg.
    """
    pcfg = (cfg.get("providers") or {}).get("memory_kg") or {}
    if not pcfg.get("enabled"):
        return
    url = pcfg.get("mcp_url") or ""
    if not url:
        return

    print("\n--- Memory KG (validate-only) ---")
    print(f"  Probing {url} ...", end=" ", flush=True)
    try:
        from claude_hooks.providers.memory_kg import MemoryKgProvider
        candidate = ServerCandidate(
            server_key="memory_kg", url=url,
            headers=pcfg.get("headers") or {},
            source="config", confidence="manual",
        )
        ok = MemoryKgProvider.verify(candidate, timeout=5.0)
    except Exception as e:
        print(f"FAILED ({e})")
        return
    print("OK" if ok else "no signature tools (search_nodes / create_entities)")
    print("  Embedding lives inside the MCP server; claude-hooks doesn't"
          " override it. To change models, edit the MCP server's config"
          " and restart it.")


def _setup_ollama_chat(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Walk the user through Ollama chat-backend settings (v1.4).

    Covers the three Ollama-backed flows that previously had **no**
    interactive prompts (all hard-coded defaults in config.py):

    1. HyDE (``hooks.user_prompt_submit.hyde_*``) — fast hallucinated
       answer used as the retrieval query for memory recall.
    2. Reflect (``reflect.*``) — Stop-hook turn-summary skill.
    3. Consolidate (``consolidate.*``) — periodic memory dedup /
       prune skill.

    Explicitly **not** covered: the /get-advice advisor and the
    /consultants engine. Those have their own separate model
    configurations (different envs, different defaults, different
    sizes) and would muddy this dialog.

    Idempotent — re-running install.py preserves existing values as
    defaults; brand-new installs get the full prompt sequence.

    Skipped silently in non-interactive mode when no Ollama URL is
    already configured anywhere (so a fresh install in CI doesn't
    ask three model-name questions to no avail).
    """
    ups = (cfg.get("hooks") or {}).get("user_prompt_submit") or {}
    reflect = cfg.get("reflect") or {}
    consolidate = cfg.get("consolidate") or {}

    # Probe existing config — used for sensible defaults.
    existing_hyde_url = ups.get("hyde_url") or ""
    existing_hyde_model = ups.get("hyde_model") or ""
    existing_hyde_fallback = ups.get("hyde_fallback_model") or ""
    existing_hyde_ctx = ups.get("hyde_num_ctx") or 16384
    existing_reflect_model = reflect.get("ollama_model") or ""
    existing_reflect_ctx = reflect.get("num_ctx") or 16384
    existing_consolidate_model = consolidate.get("ollama_model") or ""
    existing_consolidate_ctx = consolidate.get("num_ctx") or 16384

    any_existing = any([
        existing_hyde_url, existing_hyde_model,
        existing_reflect_model, existing_consolidate_model,
    ])

    print("\n--- Ollama chat backend (HyDE + skills) ---")
    print("  Used for HyDE query expansion, the reflect Stop-hook")
    print("  summarizer, and the consolidate memory-cleanup skill.")
    print("  NOT used for /get-advice or /consultants (separate configs).")

    if non_interactive:
        if not any_existing:
            print("  --non-interactive and no Ollama URL in config -> skipping.")
            return
        ans = "y"
        print("  --non-interactive: keeping existing Ollama chat config.")
    else:
        default = "Y" if any_existing else "N"
        ans = input(
            f"  Use Ollama as a chat backend? [{default}/{'n' if default == 'Y' else 'y'}]: "
        ).strip().lower() or default.lower()
        if ans not in ("y", "yes"):
            print("  Skipped — HyDE / reflect / consolidate stay at hardcoded defaults.")
            return

    # ----- 1. Base URL ----------------------------------------------
    default_base_url = "http://localhost:11434/api/generate"
    if existing_hyde_url:
        proposed_url = existing_hyde_url
    else:
        proposed_url = default_base_url

    if non_interactive:
        chat_url = proposed_url
    else:
        chat_url = input(
            f"  Ollama base URL [{proposed_url}]: "
        ).strip() or proposed_url

    # Validate by hitting /api/tags (cheap, doesn't require model load).
    base = _ollama_base_from_embed_url(chat_url)
    print(f"  Probing {base}/api/tags ...", end=" ", flush=True)
    try:
        import urllib.request as _u
        with _u.urlopen(f"{base}/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8") or "{}")
        ok = isinstance(tags.get("models"), list)
        print("OK" if ok else "unexpected response")
    except Exception as e:
        print(f"FAILED ({e})")
        ok = False
    if not ok:
        if non_interactive:
            print("  --non-interactive: keeping URL in config; you can fix it later.")
        else:
            ans = input("  Keep URL anyway? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("  Skipped Ollama chat setup.")
                return

    # ----- 2. HyDE settings -----------------------------------------
    # Whether HyDE itself is enabled is orthogonal — the user might
    # have an Ollama URL but want HyDE off (slow models on low-end
    # hardware). Re-running install.py preserves the prior toggle
    # state.
    existing_hyde_enabled = ups.get("hyde_enabled", True)
    if non_interactive:
        hyde_enabled = existing_hyde_enabled
    else:
        default = "Y" if existing_hyde_enabled else "N"
        ans = input(
            f"  Enable HyDE query expansion? [{default}/{'n' if default == 'Y' else 'y'}]: "
        ).strip().lower() or default.lower()
        hyde_enabled = ans in ("y", "yes")

    proposed_hyde_model = existing_hyde_model or "gemma4:e2b"
    # #225 (2026-05-19): default the HyDE fallback to a *different*
    # model than the primary on fresh installs so the fallback is
    # meaningful (a primary-failure that lands on the same model has
    # no chance of succeeding). ``gemma4:31b-cloud`` is the chosen
    # default because Ollama's free tier currently includes free
    # inference on it — no quota cost for users who haven't paid
    # for Ollama Pro yet, and a strict capability bump from the
    # local ``gemma4:e2b`` primary. Existing installs that already
    # have a fallback set keep theirs (back-compat).
    proposed_hyde_fallback = existing_hyde_fallback or "gemma4:31b-cloud"
    if non_interactive:
        hyde_model = proposed_hyde_model
        hyde_fallback = proposed_hyde_fallback
        hyde_ctx = existing_hyde_ctx
    else:
        hyde_model = input(
            f"  HyDE model [{proposed_hyde_model}]: "
        ).strip() or proposed_hyde_model
        hyde_fallback = input(
            f"  HyDE fallback model [{proposed_hyde_fallback}]: "
        ).strip() or proposed_hyde_fallback
        raw = input(f"  HyDE num_ctx [{existing_hyde_ctx}]: ").strip()
        try:
            hyde_ctx = int(raw) if raw else existing_hyde_ctx
        except ValueError:
            hyde_ctx = existing_hyde_ctx

    # ----- 3. Skills (reflect + consolidate) ------------------------
    # The two skill models are typically the same — offer a shortcut
    # so the user doesn't answer four near-identical questions.
    proposed_skill_model = (
        existing_reflect_model
        or existing_consolidate_model
        or hyde_model
    )
    if non_interactive:
        share_skills = (
            (existing_reflect_model == existing_consolidate_model)
            or not (existing_reflect_model or existing_consolidate_model)
        )
    else:
        ans = input(
            "  Same model for reflect + consolidate skills? [Y/n]: "
        ).strip().lower() or "y"
        share_skills = ans in ("y", "yes")

    if share_skills:
        if non_interactive:
            skill_model = proposed_skill_model
            skill_ctx = max(existing_reflect_ctx, existing_consolidate_ctx)
        else:
            skill_model = input(
                f"  Skills model [{proposed_skill_model}]: "
            ).strip() or proposed_skill_model
            ctx_default = max(existing_reflect_ctx, existing_consolidate_ctx)
            raw = input(f"  Skills num_ctx [{ctx_default}]: ").strip()
            try:
                skill_ctx = int(raw) if raw else ctx_default
            except ValueError:
                skill_ctx = ctx_default
        reflect_model = consolidate_model = skill_model
        reflect_ctx = consolidate_ctx = skill_ctx
    else:
        if non_interactive:
            reflect_model = existing_reflect_model or proposed_skill_model
            consolidate_model = existing_consolidate_model or proposed_skill_model
            reflect_ctx = existing_reflect_ctx
            consolidate_ctx = existing_consolidate_ctx
        else:
            reflect_model = input(
                f"  Reflect model [{existing_reflect_model or proposed_skill_model}]: "
            ).strip() or (existing_reflect_model or proposed_skill_model)
            raw = input(f"  Reflect num_ctx [{existing_reflect_ctx}]: ").strip()
            try:
                reflect_ctx = int(raw) if raw else existing_reflect_ctx
            except ValueError:
                reflect_ctx = existing_reflect_ctx
            consolidate_model = input(
                f"  Consolidate model [{existing_consolidate_model or proposed_skill_model}]: "
            ).strip() or (existing_consolidate_model or proposed_skill_model)
            raw = input(f"  Consolidate num_ctx [{existing_consolidate_ctx}]: ").strip()
            try:
                consolidate_ctx = int(raw) if raw else existing_consolidate_ctx
            except ValueError:
                consolidate_ctx = existing_consolidate_ctx

    # ----- 4. Apply --------------------------------------------------
    cfg.setdefault("hooks", {}).setdefault("user_prompt_submit", {})
    cfg["hooks"]["user_prompt_submit"]["hyde_url"] = chat_url
    cfg["hooks"]["user_prompt_submit"]["hyde_enabled"] = hyde_enabled
    cfg["hooks"]["user_prompt_submit"]["hyde_model"] = hyde_model
    cfg["hooks"]["user_prompt_submit"]["hyde_fallback_model"] = hyde_fallback
    cfg["hooks"]["user_prompt_submit"]["hyde_num_ctx"] = hyde_ctx

    cfg.setdefault("reflect", {})
    cfg["reflect"]["ollama_url"] = chat_url
    cfg["reflect"]["ollama_model"] = reflect_model
    cfg["reflect"]["num_ctx"] = reflect_ctx

    cfg.setdefault("consolidate", {})
    cfg["consolidate"]["ollama_url"] = chat_url
    cfg["consolidate"]["ollama_model"] = consolidate_model
    cfg["consolidate"]["num_ctx"] = consolidate_ctx

    print(f"  HyDE: {hyde_model} (fallback {hyde_fallback}) "
          f"@ ctx={hyde_ctx} -> {chat_url}")
    if share_skills:
        print(f"  Skills (reflect + consolidate): {reflect_model} @ ctx={reflect_ctx}")
    else:
        print(f"  Reflect: {reflect_model} @ ctx={reflect_ctx}")
        print(f"  Consolidate: {consolidate_model} @ ctx={consolidate_ctx}")


def _setup_llamafile_chat_models(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Walk the user through registering one or more llamafile chat
    models for HyDE / reflect / consolidate (v1.5+).

    Independent of the Ollama dialog — both can be configured. The
    llamafile registry lives at ``~/.claude/llamafile-models.json``
    and the daemon supervises spawn / LRU evict / idle reap. Setting
    a ``model_ref`` of ``llamafile://<label>`` on any of HyDE / reflect
    / consolidate routes that flow through the daemon-ensured llamafile;
    bare Ollama identifiers keep the v1.4 path.

    Skipped silently in non-interactive mode (registry-state is too
    install-time-specific to assume defaults).
    """
    print("\n--- llamafile chat models (v1.5+) ---")
    print("  Optional. Lets HyDE / reflect / consolidate (and "
          "/get-advice / /consultants if you wire them up) talk to "
          "a local llamafile chat model instead of Ollama. Registry "
          "lives at ~/.claude/llamafile-models.json; the daemon "
          "supervises spawn + idle reap.")

    if non_interactive:
        print("  --non-interactive: skipping llamafile chat-model "
              "registration. Use `claude-hooks-models add` to register "
              "models after install.")
        return

    ans = input(
        "  Register a llamafile chat model now? [y/N]: "
    ).strip().lower()
    if ans not in ("y", "yes"):
        print("  Skipped — register models later with "
              "`claude-hooks-models add <label> <gguf-path>`.")
        return

    # Lazy import — keeps install.py importable on hosts that haven't
    # installed the package yet (the registry has no third-party deps,
    # but failing fast is unfriendly during initial setup).
    try:
        from claude_hooks.chat_model_registry import (
            Registry, LabelCollision, InvalidGguf, InvalidLabel,
            PortCollision, NoFreePort,
        )
    except ImportError as e:  # pragma: no cover
        print(f"  WARN: cannot import registry module ({e}); skipping.")
        return

    if dry_run:
        print("  --dry-run: would prompt for GGUF path + label.")
        return

    reg = Registry()
    existing = reg.list_labels()
    if existing:
        print(f"  Existing labels: {', '.join(existing)}")
        ans = input(
            "  Add another model? [y/N]: "
        ).strip().lower()
        if ans not in ("y", "yes"):
            return

    while True:
        gguf_path = input(
            "  GGUF path: "
        ).strip()
        if not gguf_path:
            print("  (empty; aborting llamafile setup)")
            return
        gguf_abs = str(Path(gguf_path).expanduser().resolve())
        if not Path(gguf_abs).is_file():
            print(f"  Not a file: {gguf_abs}")
            continue
        if not _gguf_magic_ok(gguf_abs):
            print(f"  {gguf_abs} doesn't start with GGUF magic; "
                  "is this really a GGUF?")
            continue
        break

    while True:
        label = input(
            "  Label for this model (lowercase, dots/dashes/underscores): "
        ).strip()
        if not label:
            print("  (empty; aborting llamafile setup)")
            return
        if label in existing:
            print(f"  Label {label!r} already registered; pick another.")
            continue
        break

    ctx_raw = input("  Context size [16384]: ").strip()
    try:
        ctx_size = int(ctx_raw) if ctx_raw else 16384
    except ValueError:
        ctx_size = 16384

    mode_raw = input("  GPU mode [auto/cpu] [auto]: ").strip().lower()
    mode = mode_raw if mode_raw in ("auto", "cpu") else "auto"

    port_raw = input(
        "  Port [auto-allocate from 38093-38099]: "
    ).strip()
    port: Optional[int]
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            print(f"  Invalid port {port_raw!r}; auto-allocating.")
            port = None
    else:
        port = None

    try:
        spec = reg.add(
            label, gguf_path=gguf_abs,
            ctx_size=ctx_size, port=port, mode=mode,
        )
    except (LabelCollision, InvalidGguf, InvalidLabel,
            PortCollision, NoFreePort) as e:
        print(f"  Registry add failed: {e}")
        return

    print(f"  Registered: {spec.label} -> {spec.gguf_path}")
    print(f"             port={spec.port} ctx={spec.ctx_size} "
          f"mode={spec.mode}")

    # Offer to wire HyDE / reflect / consolidate to this label.
    ans = input(
        "  Use this model for HyDE + reflect + consolidate? [Y/n]: "
    ).strip().lower() or "y"
    if ans in ("y", "yes"):
        ref = f"llamafile://{spec.label}"
        cfg.setdefault("hooks", {}).setdefault("user_prompt_submit", {})
        cfg["hooks"]["user_prompt_submit"]["hyde_model_ref"] = ref
        cfg.setdefault("reflect", {})["model_ref"] = ref
        cfg.setdefault("consolidate", {})["model_ref"] = ref
        print(f"  Wired hyde_model_ref / reflect.model_ref / "
              f"consolidate.model_ref to {ref}")
    else:
        print(f"  Skipped wiring. Set ``hyde_model_ref: "
              f"'llamafile://{spec.label}'`` manually in your "
              "config to enable.")


def _setup_chat_backends(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Top-level chat-backend dialog (v1.5+). Runs the Ollama half
    first (covers HyDE / reflect / consolidate base config), then the
    optional llamafile registry walk-through. Either or both can be
    used — Ollama-bare names and ``llamafile://<label>`` refs coexist
    in the same flows.
    """
    _setup_ollama_chat(cfg, non_interactive=non_interactive, dry_run=dry_run)
    _setup_llamafile_chat_models(
        cfg, non_interactive=non_interactive, dry_run=dry_run,
    )


def _sqlite_vec_extension_available() -> bool:
    """Return True iff the ``sqlite_vec`` Python package is importable.

    The provider lazy-loads it on first use; checking at install time
    lets us surface a clear "pip install sqlite-vec" breadcrumb
    instead of waiting for the first hook to fail.
    """
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _setup_sqlite_vec_mcp(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """Ask if sqlite_vec is wanted and wire its config block (v1.4+).

    Pre-v1.6 sqlite_vec was a strictly local provider; v1.6 added a
    system-wide MCP launcher (so Cursor / Codex / OpenWebUI / Claude
    Desktop can share the same .db file), and v1.7 added lazy schema
    migration (``sqlite_vec_schema.py``, ``LATEST_VERSION = 2`` covers
    the M14 ``expires_at`` column). The launcher drops at
    ``~/.local/bin/sqlite-vec-mcp`` (POSIX) or
    ``%LOCALAPPDATA%/claude-hooks/bin/sqlite-vec-mcp.cmd`` (Windows)
    and registers under ``mcpServers.sqlite_vec`` in ``~/.claude.json``.

    Dialog steps:

    1. Probe whether sqlite_vec is fully configured already (DSN +
       enabled + launcher present); if so, offer a
       ``[V]alidate-only / [R]e-install / [S]kip`` shortcut via
       :func:`_validate_sqlite_vec_only` (#237, 2026-05-21) — mirrors
       the pgvector validate-only pattern.
    2. Ask whether to enable the provider (skipped on validate-only path).
    3. Prompt for the SQLite db_path (default
       ``~/.claude/claude-hooks-memory.db``).
    4. Drop the system-wide MCP launcher and register it in
       ``~/.claude.json``.
    5. Delegate the embedder choice (Ollama / remote llamafile /
       OpenAI / local llamafile) to :func:`_setup_embedding_engine`.

    Idempotent: re-runs upgrade the launcher in place; the lazy
    schema migration in the provider carries existing dbs to the
    current ``LATEST_VERSION`` on first ``store()``/``recall()`` call.

    Skipped silently in non-interactive mode when the provider isn't
    already enabled — same precedent as ``_setup_pgvector_mcp``.
    """
    pcfg = (cfg.get("providers") or {}).get("sqlite_vec") or {}
    already_enabled = bool(pcfg.get("enabled"))
    existing_db = pcfg.get("db_path") or "~/.claude/claude-hooks-memory.db"
    launcher_path = _sqlite_vec_launcher_path()
    launcher_present = launcher_path.exists()
    expanded_db = Path(os.path.expanduser(existing_db))
    fully_configured = bool(
        already_enabled
        and expanded_db.exists()
        and launcher_present
    )

    print("\n--- sqlite_vec ---")
    print("  Optional: local-only persistent memory backed by SQLite + sqlite-vec.")
    print("  v1.6+: also installs a system-wide MCP launcher so external")
    print("  clients (Cursor / Codex / OpenWebUI / Claude Desktop) can share")
    print("  the same .db file. Shared embedder dialog so failover with")
    print("  llamafile works the same way as pgvector.")

    if non_interactive:
        if not already_enabled:
            print("  --non-interactive and not currently enabled -> skipping sqlite_vec.")
            return
        ans = "y"
        print("  --non-interactive: keeping existing sqlite_vec config.")
    elif fully_configured:
        # #237 (2026-05-21): mirror pgvector's validate-only shortcut.
        # When sqlite_vec is already fully configured (enabled, db file
        # present, launcher dropped), default V so the lowest-impact
        # action is the easy one.
        print(f"  Currently configured: enabled, db at {expanded_db},")
        print(f"  launcher at {launcher_path}")
        choice = input(
            "  [V]alidate only / [R]e-install / [S]kip? [V/r/s]: "
        ).strip().lower() or "v"
        if choice in ("s", "skip", "n", "no"):
            print("  Skipped.")
            return
        if choice in ("v", "validate", "y", "yes"):
            # Yes/y maps to validate here for the same reason as pgvector:
            # the natural "yes I want this" answer for an already-working
            # install is "yes, confirm it's working".
            _validate_sqlite_vec_only(cfg)
            return
        # Fall through to full re-install on R / re-install / anything else.
        ans = "y"
    else:
        default = "Y" if already_enabled else "N"
        ans = input(
            f"  Set up sqlite_vec? [{default}/{'n' if default == 'Y' else 'y'}]: "
        ).strip().lower() or default.lower()
        if ans not in ("y", "yes"):
            print("  Skipped.")
            return

    # 1. db_path — preserve existing when present.
    if non_interactive:
        db_path = existing_db
    else:
        raw = input(f"  SQLite db path [{existing_db}]: ").strip()
        db_path = raw or existing_db

    expanded = os.path.expanduser(db_path)
    parent = Path(expanded).parent
    if not parent.exists():
        if dry_run:
            print(f"  [dry-run] Would mkdir -p {parent}")
        else:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"  Cannot create {parent}: {e}")
                print("  Pick a different db_path and re-run install.py.")
                return

    # 2. Python dep breadcrumb.
    if not _sqlite_vec_extension_available():
        print("  Note: the 'sqlite-vec' Python package isn't installed in the")
        print("  current env. Install with: pip install sqlite-vec  (or include")
        print("  the [sqlite-vec] extra: pip install claude-hooks[sqlite-vec]).")

    # 3. Apply config + drive embedder dialog (only on a fresh wire-up;
    # re-runs respect an existing embedder choice).
    cfg.setdefault("providers", {}).setdefault("sqlite_vec", {})
    sv = cfg["providers"]["sqlite_vec"]
    sv["enabled"] = True
    sv["db_path"] = db_path
    sv.setdefault("table", "memory")
    sv.setdefault("recall_k", 5)
    sv.setdefault("store_mode", "auto")
    sv.setdefault("timeout", 10.0)
    if not sv.get("embedder"):
        _setup_embedding_engine(
            cfg, provider="sqlite_vec",
            non_interactive=non_interactive, dry_run=dry_run,
        )

    # 4. System-wide MCP launcher (v1.6+).
    #
    # Same shape as ``_setup_pgvector_mcp`` — drops a tiny script at
    # ``~/.local/bin/sqlite-vec-mcp`` (POSIX) or
    # ``%LOCALAPPDATA%\claude-hooks\bin\sqlite-vec-mcp.cmd`` (Windows)
    # and registers it under ``~/.claude.json`` -> mcpServers.sqlite_vec.
    # Other MCP-aware tools (Cursor, Codex, OpenWebUI, Claude Desktop)
    # can point at the absolute path to share the same .db file the
    # hook framework reads in-process.
    #
    # ``--non-interactive`` always installs (matches the pgvector
    # behavior); skip explicitly with --skip-sqlite-vec-launcher if you
    # ever need to (no flag today; add when there's a use case).
    py_path = find_conda_env_python()
    py = str(py_path) if py_path.exists() else sys.executable
    launcher_path = _sqlite_vec_launcher_path()
    if non_interactive:
        install_launcher = True
    else:
        if launcher_path.exists():
            print(f"  Existing launcher: {launcher_path}")
            choice = input(
                "  [V]alidate only / [R]e-install / [S]kip? [V/r/s]: "
            ).strip().lower() or "v"
            if choice in ("s", "skip", "n", "no"):
                install_launcher = False
                validate_only = False
            elif choice in ("v", "validate", "y", "yes"):
                install_launcher = False
                validate_only = True
            else:
                install_launcher = True
                validate_only = False
        else:
            ans = input("  Install system-wide MCP launcher? [Y/n]: ").strip().lower()
            install_launcher = ans in ("", "y", "yes")
            validate_only = False

    if install_launcher:
        if dry_run:
            print(f"  [dry-run] Would write launcher: {launcher_path}")
            print(f"  [dry-run] Would register mcpServers.sqlite_vec in ~/.claude.json")
        else:
            _write_sqlite_vec_launcher(launcher_path, py=py, repo=str(HERE))
            print(f"  Launcher: {launcher_path}")
            _register_sqlite_vec_mcp_in_claude_json(launcher_path)
            print(f"  ~/.claude.json: registered mcpServers.sqlite_vec -> {launcher_path}")
    elif not non_interactive and locals().get("validate_only"):
        # Read-only spawn + initialize + immediate shutdown.
        ok = _validate_sqlite_vec_launcher(launcher_path)
        print(f"  Launcher validate: {'OK' if ok else 'FAIL'}")

    print(f"  Done. sqlite_vec.enabled = True, db_path = {db_path}")
    print(f"  After Claude Code restart, tools surface as:")
    print(f"    mcp__sqlite_vec__sqlite-vec-find / -store / -count")


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


def _sqlite_vec_launcher_path() -> Path:
    """Choose the system-wide install location for the sqlite-vec MCP
    launcher. Mirrors ``_pgvector_launcher_path`` so both stores share
    the same install convention.

    POSIX: ``~/.local/bin/sqlite-vec-mcp``.
    Windows: ``%LOCALAPPDATA%/claude-hooks/bin/sqlite-vec-mcp.cmd``.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"))
        return base / "claude-hooks" / "bin" / "sqlite-vec-mcp.cmd"
    return Path(os.path.expanduser("~/.local/bin/sqlite-vec-mcp"))


def _write_sqlite_vec_launcher(path: Path, *, py: str, repo: str) -> None:
    """Write the sqlite-vec MCP launcher script with interpreter +
    PYTHONPATH baked in. Mirrors ``_write_pgvector_launcher`` exactly
    apart from the module name and identifying comment.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        body = (
            "@echo off\r\n"
            "REM sqlite-vec-mcp launcher (claude-hooks) -- generated by install.py\r\n"
            f'set PYTHONPATH={repo};%PYTHONPATH%\r\n'
            f'"{py}" -m claude_hooks.sqlite_vec_mcp %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )
        path.write_text(body, encoding="utf-8")
    else:
        body = (
            "#!/usr/bin/env sh\n"
            "# sqlite-vec-mcp launcher (claude-hooks) -- generated by install.py\n"
            f'PYTHONPATH="{repo}:${{PYTHONPATH:-}}" exec "{py}" -m claude_hooks.sqlite_vec_mcp "$@"\n'
        )
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    if str(path.parent) not in (os.environ.get("PATH") or "").split(os.pathsep):
        print(f"  [!] {path.parent} is not in PATH -- only Claude Code can find it (absolute path).")
        print(f"      Add to PATH if you want Cursor/Codex/etc. to spawn `sqlite-vec-mcp` by name.")


def _validate_sqlite_vec_launcher(launcher_path: Path) -> bool:
    """Spawn the launcher, send a single ``initialize`` request, read
    one response, kill it. Returns True if the server reported its
    serverInfo with ``name == "claude-hooks-sqlite-vec"``.

    Read-only — does not touch the .db file. Used by the re-run
    ``Validate only`` path.
    """
    import subprocess as _sp
    import json as _json
    if not launcher_path.exists():
        return False
    try:
        proc = _sp.Popen(
            [str(launcher_path)] if os.name == "nt" else ["/bin/sh", str(launcher_path)],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
            text=True,
        )
    except OSError:
        return False
    try:
        req = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(req)
        proc.stdin.flush()
        # Block for one line of stdout, with a short timeout.
        import select
        if hasattr(select, "select"):
            rdy, _, _ = select.select([proc.stdout], [], [], 5.0)
            if not rdy:
                return False
        line = proc.stdout.readline()
        if not line:
            return False
        resp = _json.loads(line)
        info = (((resp.get("result") or {}).get("serverInfo")) or {})
        return info.get("name") == "claude-hooks-sqlite-vec"
    except (OSError, ValueError, _json.JSONDecodeError):
        return False
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# -- bin/ shim wrappers (cross-platform PATH glue) ----------------------- #
#
# The bin/ shims (claude-hook, claude-consultants, claude-advisor,
# claude-hooks-daemon, caliber-grounding-proxy, ...) live inside the
# repo and resolve their own location with ``dirname "$0"``. That works
# fine when invoked by absolute path (e.g. from settings.json's
# UserPromptSubmit hook entry) but breaks two ways for skill CLIs that
# use the bare command name:
#
#   1. Skills run inside Claude Code's bash subprocess, whose PATH does
#      NOT include the repo's bin/ on any platform -- so a bare
#      ``claude-consultants config show`` from /consultants--config
#      returns 127 / command not found.
#
#   2. Symlinking the repo shim into a PATH dir doesn't help either:
#      ``$0`` becomes the symlink path and REPO resolves to the wrong
#      tree (e.g. /usr/local instead of the repo), so the shim fails
#      to source its bin/_resolve_python*.sh helper.
#
# Fix: drop tiny exec-wrapper scripts in a known PATH-friendly location
# that ``exec`` the absolute repo shim path. The wrapper has zero logic
# beyond forwarding args -- the real shim still runs and resolves REPO
# correctly via the absolute ``$0``.
#
# Locations (matches the pgvector-mcp launcher pattern above):
#   POSIX (Linux + macOS): ``~/.local/bin/<shim>``  (POSIX sh wrapper)
#   Windows:               ``%LOCALAPPDATA%\claude-hooks\bin\<shim>``
#                          (POSIX sh wrapper for MSYS / Git-bash that
#                           Claude Code uses on Windows) + ``<shim>.cmd``
#                          (native cmd.exe / PowerShell wrapper).
#
# All wrappers are tagged in their first comment line so a re-run of
# install.py replaces only its own files and never clobbers a hand-rolled
# wrapper of the same name.

# List of bin/ shims that should get a PATH wrapper. Helpers (anything
# starting with ``_``) and platform-specific .cmd files are excluded --
# the .cmd siblings are produced from the POSIX shim by this installer.
_SHIM_WRAPPER_TAG = "claude-hooks shim wrapper -- generated by install.py"


def _shim_names_to_install(repo_path: Path) -> list[str]:
    """Return the sorted list of bin/ shim base names to wrap.

    Walks ``<repo>/bin/`` and picks every regular file that:
      * does not start with ``_`` (helpers like _resolve_python.sh)
      * does not end in ``.cmd`` (Windows native sibling, generated)
      * has a shebang line (filters out READMEs etc.)
    """
    names: list[str] = []
    bin_dir = repo_path / "bin"
    if not bin_dir.is_dir():
        return names
    for child in sorted(bin_dir.iterdir()):
        if not child.is_file():
            continue
        n = child.name
        if n.startswith("_") or n.endswith(".cmd"):
            continue
        try:
            with open(child, "rb") as f:
                head = f.read(4)
            if head[:2] != b"#!":
                continue
        except OSError:
            continue
        names.append(n)
    return names


def _shim_wrapper_dir() -> Path:
    """Choose the wrapper install dir for the current platform.

    POSIX (Linux + macOS): ``~/.local/bin``. Conventional user-bin dir,
    on PATH for most modern desktops; we warn if it isn't.
    Windows: ``%LOCALAPPDATA%\\claude-hooks\\bin``. Mirrors the existing
    ``pgvector-mcp`` launcher path so all claude-hooks user-bin glue
    lives in one place.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"))
        return base / "claude-hooks" / "bin"
    return Path(os.path.expanduser("~/.local/bin"))


def _write_shim_wrapper_posix(path: Path, target: Path) -> None:
    """POSIX sh wrapper: ``exec`` the absolute repo shim with all args."""
    body = (
        "#!/usr/bin/env sh\n"
        f"# {_SHIM_WRAPPER_TAG}\n"
        f'exec "{target}" "$@"\n'
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_shim_wrapper_cmd(path: Path, target: Path) -> None:
    """Windows .cmd wrapper for native cmd.exe / PowerShell.

    Claude Code itself uses MSYS bash on Windows and reaches the POSIX
    wrapper, but the .cmd sibling lets the user invoke the same command
    name from a normal shell without thinking about it.
    """
    # ``call`` would re-enter the same .cmd; use the absolute repo
    # ``.cmd`` shim if it exists, else fall back to running the POSIX
    # shim through sh.
    posix_target = target
    cmd_target = target.with_suffix(target.suffix + ".cmd") if target.suffix else target.with_name(target.name + ".cmd")
    if cmd_target.exists():
        body = (
            "@echo off\r\n"
            f"REM {_SHIM_WRAPPER_TAG}\r\n"
            f'"{cmd_target}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )
    else:
        # No .cmd sibling in the repo -- run the POSIX shim through sh.
        # MSYS / Git-bash provides ``sh.exe`` on PATH whenever bash is
        # installed, which is true on every Claude Code Windows host.
        body = (
            "@echo off\r\n"
            f"REM {_SHIM_WRAPPER_TAG}\r\n"
            f'sh "{posix_target}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )
    path.write_text(body, encoding="utf-8")


def _is_managed_shim_wrapper(path: Path) -> bool:
    """True if the file looks like a wrapper we previously wrote.

    Checked by reading the first ~200 bytes and matching the tag. We
    deliberately don't trust the path alone -- a hand-rolled wrapper at
    the same name should NOT be silently replaced.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(256)
        return _SHIM_WRAPPER_TAG.encode("ascii") in head
    except OSError:
        return False


def _install_bin_shim_wrappers(repo_path: Path, *, dry_run: bool) -> None:
    """Drop PATH-friendly wrappers for every bin/* shim in the repo.

    Idempotent: replaces only files tagged with ``_SHIM_WRAPPER_TAG``;
    skips any pre-existing file of the same name that isn't ours.
    Prints a single summary line at the end.
    """
    names = _shim_names_to_install(repo_path)
    if not names:
        return
    wrapper_dir = _shim_wrapper_dir()
    if dry_run:
        print(f"  [dry-run] Would write {len(names)} shim wrappers under {wrapper_dir}")
        return
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped: list[str] = []
    for n in names:
        target = (repo_path / "bin" / n).resolve()
        if os.name == "nt":
            posix_path = wrapper_dir / n
            cmd_path = wrapper_dir / (n + ".cmd")
            for p, writer in ((posix_path, _write_shim_wrapper_posix),
                              (cmd_path, _write_shim_wrapper_cmd)):
                if p.exists() and not _is_managed_shim_wrapper(p):
                    skipped.append(str(p))
                    continue
                writer(p, target)
                written += 1
        else:
            p = wrapper_dir / n
            if p.exists() and not _is_managed_shim_wrapper(p):
                skipped.append(str(p))
                continue
            _write_shim_wrapper_posix(p, target)
            written += 1
    print(f"  Bin wrappers: wrote {written} under {wrapper_dir}")
    if skipped:
        print(f"  Skipped {len(skipped)} pre-existing non-managed file(s):")
        for s in skipped[:5]:
            print(f"    {s}")
        if len(skipped) > 5:
            print(f"    ... and {len(skipped) - 5} more")
    # PATH-membership: on Windows we MUST get the wrapper dir into the
    # user's persistent PATH because Claude Code's bash subprocess
    # inherits its PATH from the parent process, so a skill calling
    # ``claude-consultants`` by bare name fails until the dir is in
    # User PATH (HKCU\Environment). On POSIX ``~/.local/bin`` is
    # almost always already on the user's interactive PATH, but if
    # not we just print a clear shell-rc hint -- modifying shell rc
    # files non-interactively is too invasive.
    path_dirs = (os.environ.get("PATH") or "").split(os.pathsep)
    if str(wrapper_dir) in path_dirs:
        return
    if os.name == "nt":
        _ensure_windows_user_path_includes(wrapper_dir)
    else:
        print(f"  [!] {wrapper_dir} is not in PATH for this shell.")
        print(f"      To enable bare-name skill CLIs (claude-consultants, claude-advisor, ...):")
        print(f'        echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.bashrc  # or ~/.zshrc')
        print(f"      Then open a new shell (or restart Claude Code).")


def _read_windows_user_path() -> Optional[str]:
    """Read HKCU\\Environment\\PATH via reg query.

    Returns the raw User PATH string (REG_EXPAND_SZ or REG_SZ), or
    ``None`` if the value is missing or unreadable. Reading via reg
    avoids the system+user merge that ``%PATH%`` and Python's
    ``os.environ`` see, which would lead us to ADD a dir that is
    already on system PATH (harmless but noisy) or skip a dir that
    is on system PATH but not user PATH (broken result).
    """
    try:
        out = subprocess.check_output(
            ["reg", "query", "HKCU\\Environment", "/v", "PATH"],
            stderr=subprocess.STDOUT, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    # Output shape:
    #   HKEY_CURRENT_USER\Environment
    #       PATH    REG_EXPAND_SZ    C:\foo;C:\bar
    for line in out.splitlines():
        line = line.strip()
        if line.upper().startswith("PATH"):
            # Split on whitespace, drop name + type, rejoin remainder.
            parts = line.split(None, 2)
            if len(parts) >= 3:
                return parts[2]
    return None


def _ensure_windows_user_path_includes(wrapper_dir: Path) -> None:
    """Prepend ``wrapper_dir`` to HKCU\\Environment\\PATH if missing.

    Uses ``reg add`` rather than ``setx`` because ``setx`` silently
    truncates PATH to 1024 chars, which on a developer machine with
    a long user PATH is destructive. ``reg add`` writes the literal
    bytes we hand it, capped only by the registry's REG_EXPAND_SZ
    limit (~32 KB).

    Idempotent. Skips with a warning if the resulting PATH would be
    absurdly long (>= 16 KB) -- defensive guard, you'd have to be
    deliberately abusing user PATH to hit it.
    """
    target = str(wrapper_dir)
    cur = _read_windows_user_path()
    cur_dirs = (cur or "").split(";") if cur else []
    cur_dirs_norm = [d.lower().rstrip("\\") for d in cur_dirs if d]
    if target.lower().rstrip("\\") in cur_dirs_norm:
        # Already there but not visible to this shell -- the next
        # shell spawn (or Claude Code restart) will pick it up.
        print(f"  [info] {wrapper_dir} already on User PATH (open a new shell to see it).")
        return
    new_path = (target + ";" + cur) if cur else target
    if len(new_path) > 16384:
        print(f"  [warn] User PATH would exceed 16 KB; refusing to extend it.")
        print(f"         Add manually if you need it: {wrapper_dir}")
        return
    try:
        subprocess.check_output(
            ["reg", "add", "HKCU\\Environment", "/v", "PATH",
             "/t", "REG_EXPAND_SZ", "/d", new_path, "/f"],
            stderr=subprocess.STDOUT, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  [warn] reg add failed: {e}")
        print(f"         Add manually:   setx PATH \"{wrapper_dir};%PATH%\"")
        return
    # Notify Explorer so future processes pick up the new PATH without
    # logoff. Best-effort: a one-shot SendMessageTimeoutW broadcast.
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"), SMTO_ABORTIFHUNG, 5000, None,
        )
    except Exception:
        pass
    print(f"  [ok] Prepended {wrapper_dir} to User PATH (HKCU\\Environment).")
    print(f"       Open a new shell or restart Claude Code to pick it up.")


def _remove_bin_shim_wrappers(*, dry_run: bool) -> int:
    """Uninstall counterpart -- removes only files tagged as ours."""
    wrapper_dir = _shim_wrapper_dir()
    if not wrapper_dir.is_dir():
        return 0
    removed = 0
    for child in sorted(wrapper_dir.iterdir()):
        if not child.is_file():
            continue
        if not _is_managed_shim_wrapper(child):
            continue
        if dry_run:
            print(f"  [dry-run] Would remove {child}")
        else:
            try:
                child.unlink()
            except OSError as e:
                print(f"  [warn] could not remove {child}: {e}")
                continue
        removed += 1
    return removed


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


def _register_sqlite_vec_mcp_in_claude_json(launcher_path: Path) -> None:
    """Register ``mcpServers.sqlite_vec`` at the root of ``~/.claude.json``.

    Mirrors ``_register_pgvector_mcp_in_claude_json`` — root-level entry
    visible to every project, semantic-tagged backup before write, same
    stdio shape so Claude Code spawns the launcher per session.
    """
    p = Path(os.path.expanduser("~/.claude.json"))
    if not p.exists():
        p.write_text("{}", encoding="utf-8")
    raw = p.read_text(encoding="utf-8")
    ts = _now_ts()
    bak = p.with_suffix(f".json.bak-{ts}-sqlite-vec-mcp")
    bak.write_text(raw, encoding="utf-8")
    cfg = json.loads(raw)
    mcps = cfg.setdefault("mcpServers", {})
    mcps["sqlite_vec"] = {
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

    Like ``_verify_pgvector_dsn``, falls back to a conda-env
    subprocess when the calling interpreter lacks psycopg — without
    this fallback, install.py running on system py3 (which has no
    psycopg) returns False here and silently re-prompts schema init
    on every re-run, making the user think the existing schema has
    vanished.
    """
    # Fast path.
    try:
        import psycopg  # type: ignore  # noqa: PLC0415
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = %s",
                        (table_name,),
                    )
                    return cur.fetchone() is not None
        except Exception:
            return False
    except ImportError:
        pass

    # Subprocess fallback via the conda env's python.
    conda_py = find_conda_env_python()
    if not conda_py.exists():
        return False
    script = (
        "import json, sys\n"
        "try:\n"
        "    import psycopg\n"
        "    with psycopg.connect(sys.argv[1], connect_timeout=5) as conn:\n"
        "        with conn.cursor() as cur:\n"
        "            cur.execute("
        "'SELECT 1 FROM information_schema.tables WHERE table_name = %s', "
        "(sys.argv[2],))\n"
        "            print(json.dumps({'present': cur.fetchone() is not None}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'present': False, 'error': str(e)}))\n"
    )
    try:
        rc = subprocess.run(
            [str(conda_py), "-c", script, dsn, table_name],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = (rc.stdout or "").strip().splitlines()
    if not out:
        return False
    try:
        return bool(json.loads(out[-1]).get("present"))
    except json.JSONDecodeError:
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

    Falls back to the conda env's python when the calling interpreter
    lacks psycopg, mirroring ``_verify_pgvector_dsn`` and
    ``_pgvector_tables_present``. The DDL is piped via stdin (it's a
    few KB so argv would be cramped).
    """
    # Build the full DDL once so both code paths use identical text.
    sys.path.insert(0, str(HERE / "scripts"))
    try:
        from migrate_to_pgvector import MODELS, schema_sql_for_model  # type: ignore
    finally:
        sys.path.pop(0)
    spec = MODELS[model]
    full_sql = _PGVECTOR_SHARED_DDL + "\n" + schema_sql_for_model(spec)

    # Fast path: psycopg in this interpreter.
    try:
        import psycopg  # type: ignore  # noqa: PLC0415
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(full_sql)
            conn.commit()
        return
    except ImportError:
        pass

    # Subprocess fallback.
    conda_py = find_conda_env_python()
    if not conda_py.exists():
        raise RuntimeError(
            "psycopg not in current python and conda env not found at "
            f"{conda_py}; cannot initialize pgvector schema. "
            "Activate the conda env or install psycopg in your shell."
        )
    script = (
        "import sys\n"
        "import psycopg\n"
        "ddl = sys.stdin.read()\n"
        "with psycopg.connect(sys.argv[1]) as conn:\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute(ddl)\n"
        "    conn.commit()\n"
    )
    rc = subprocess.run(
        [str(conda_py), "-c", script, dsn],
        input=full_sql, capture_output=True, text=True, timeout=60,
    )
    if rc.returncode != 0:
        raise RuntimeError(
            f"pgvector schema init failed (rc={rc.returncode}): "
            f"{rc.stderr.strip()[-500:]}"
        )


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


def _ensure_axon_deps(*, non_interactive: bool, dry_run: bool) -> bool:
    """Verify axon's runtime deps in the claude-hooks conda env.

    axon is installed via the upstream ``axoniq`` pip package and
    transitively needs uvicorn / httpx-sse / pydantic-settings /
    sse-starlette to serve its HTTP MCP. The conda env we hit on
    2026-05-07 had drifted — uvicorn et al. were dropped, axon
    crash-looped under systemd with ModuleNotFoundError, and the
    /root/.axon registry dir was missing so systemd's namespace
    setup failed every restart.

    This helper probes the env, and if anything's missing offers to
    pip-install ``requirements-axon.txt`` in one shot. Returns True
    when axon is importable after the run (or already was), False
    on any failure / user-declined install.
    """
    conda_py = find_conda_env_python()
    py = str(conda_py) if conda_py.exists() else sys.executable

    probe = subprocess.run(
        [py, "-c",
         "import axon, uvicorn, httpx_sse, pydantic_settings, "
         "sse_starlette; print(axon.__version__ if hasattr(axon, "
         "'__version__') else 'ok')"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        print(f"\nAxon deps:      OK ({probe.stdout.strip()})")
        return True

    # Surface the actual failing import so the operator knows what
    # broke. The probe's stderr ends with "ModuleNotFoundError: ...".
    err_tail = (probe.stderr or "").strip().splitlines()
    last_line = err_tail[-1] if err_tail else "(no error output)"
    print("\nAxon deps:      INCOMPLETE")
    print(f"  Probe failed: {last_line}")
    print("  axon needs uvicorn / httpx-sse / pydantic-settings / sse-starlette")
    print("  to serve its HTTP MCP at :8420.")

    req_axon = HERE / "requirements-axon.txt"
    if not req_axon.is_file():
        print(f"  [!!] {req_axon} missing — cannot auto-install.")
        return False

    if dry_run:
        print(f"  [dry-run] Would: {py} -m pip install -r {req_axon}")
        return False

    if non_interactive:
        print("  --non-interactive: installing axon deps...")
    else:
        ans = input("  Install axon deps now? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped. The axon-host systemd unit will crash-")
            print("  loop on import until installed manually:")
            print(f"    {py} -m pip install -r {req_axon}")
            return False

    pip_bin = str(Path(py).parent / ("pip.exe" if os.name == "nt" else "pip"))
    pip_cmd = [pip_bin, "install", "-r", str(req_axon)] \
        if Path(pip_bin).exists() \
        else [py, "-m", "pip", "install", "-r", str(req_axon)]
    rc = subprocess.run(pip_cmd, capture_output=True, text=True)
    if rc.returncode != 0:
        print(f"  pip install failed:\n{rc.stderr[-500:]}")
        print(f"  Run manually: {' '.join(pip_cmd)}")
        return False
    print("  Installed.")
    # Re-probe so the caller knows the env is actually ready.
    re_probe = subprocess.run(
        [py, "-c",
         "import axon, uvicorn, httpx_sse, pydantic_settings, sse_starlette"],
        capture_output=True, text=True,
    )
    return re_probe.returncode == 0


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


CONSULTANTS_ENV_NAME = "claude-hooks-consultants"


def _install_consultants(cfg: dict, cfg_path: Path, *,
                         non_interactive: bool, dry_run: bool) -> bool:
    """Optional /consultants engine setup.

    Returns True if the dedicated conda env is present (and any
    service unit / config wiring requested by the user has been
    written), False otherwise. The skills installer uses the return
    value to decide whether to deploy the four /consultants skills.

    Conda is **mandatory**: the consultants stack (LangGraph,
    LangServe) is heavy and version-pinned, so we never fall back to
    a bare venv or system Python. If conda is missing the function
    prints a clear message pointing at Miniconda installation and
    returns False.
    """
    print("\n==> /consultants engine")
    consultants_py = find_conda_env_python(env_name=CONSULTANTS_ENV_NAME)
    already_present = consultants_py.exists()
    if already_present:
        print(f"    {CONSULTANTS_ENV_NAME} env exists at:")
        print(f"      {consultants_py}")
    else:
        print(f"    {CONSULTANTS_ENV_NAME} env not found.")

    # Decide whether to (re)install. In non-interactive mode, only
    # update the existing env; never create a new one without consent.
    if non_interactive:
        if not already_present:
            print("    --non-interactive: skipping (no consent to create env).")
            return False
    else:
        prompt = ("    Install /consultants engine?"
                  if not already_present
                  else "    Refresh /consultants engine deps?")
        ans = input(f"{prompt} [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("    Skipping /consultants install.")
            return already_present

    # Conda is mandatory.
    conda_bin = _find_conda()
    if not conda_bin:
        print("    error: conda not found on PATH.")
        print("    Install Miniconda from "
              "https://docs.conda.io/projects/miniconda/ then re-run.")
        return False

    if dry_run:
        print("    [dry-run] Would create env, install consultants, "
              "and (optionally) install service unit.")
        return already_present

    # #223 (2026-05-19): if a previous consultants install already
    # exists, surface any drift between claude-hooks.json's
    # smart_start flag and consultants-config.toml's [service].mode
    # so the user knows the prompt below is the resolution step.
    if already_present:
        _detect_consultants_config_drift(
            cfg, consultants_py=consultants_py,
            non_interactive=non_interactive, dry_run=dry_run,
        )

    # Service mode prompt — non-interactive defaults to always-on.
    service_mode = "always-on"
    if not non_interactive:
        ans = input("    Service mode: [a]lways-on (default) or "
                    "[s]mart-start (engine spawned on demand): ").strip().lower()
        if ans.startswith("s"):
            service_mode = "smart-start"

    # Create env if needed.
    if not already_present:
        print(f"    Creating conda env '{CONSULTANTS_ENV_NAME}' "
              "(Python 3.11)...")
        rc = subprocess.run(
            [conda_bin, "create", "-n", CONSULTANTS_ENV_NAME,
             "python=3.11", "-y"],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            print(f"    conda create failed:\n{rc.stderr[-500:]}")
            return False
        consultants_py = find_conda_env_python(
            env_name=CONSULTANTS_ENV_NAME)
        if not consultants_py.exists():
            print(f"    error: env created but python not found at "
                  f"{consultants_py}")
            return False

    # pip install -e consultants/[test] — heavy, but using the env's
    # pip ensures all deps land in the right place. The ``[test]``
    # extra pulls in pytest + pytest-asyncio + pytest-timeout so the
    # bench harness tests (M11b coder + M11c tool_executor, both call
    # ``run_pytest_against_sandbox`` which subprocesses pytest with
    # ``--timeout=<s>``) can run in this env. Without it, the oracle
    # subprocess errors out on the unknown ``--timeout`` flag and
    # 4 bench-harness tests fail per the M11c-1 verification.
    consultants_target = str(HERE / "consultants") + "[test]"
    print(f"    Installing consultants/[test] into "
          f"{consultants_py.parent.name}...")
    rc = subprocess.run(
        [str(consultants_py), "-m", "pip", "install", "-e",
         consultants_target],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"    pip install -e consultants/[test] failed:\n"
              f"{rc.stderr[-500:]}")
        return False

    # Wire smart-start flag into config/claude-hooks.json.
    consultants_cfg = ((cfg.setdefault("hooks", {})
                       .setdefault("consultants", {})))
    smart = consultants_cfg.setdefault("smart_start", {})
    smart["enabled"] = (service_mode == "smart-start")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n",
                        encoding="utf-8")
    print(f"    Service mode: {service_mode}")

    # #223 (2026-05-19): mirror the chosen service mode to
    # ``~/.claude/consultants-config.toml`` via the canonical
    # ``set_service_mode`` mutator. Pre-#223 only the claude-hooks.json
    # smart_start flag was written, so the engine's own TOML still
    # said ``mode = "always-on"`` even after the operator picked
    # smart-start — a silent two-source drift the v1.8.2 pandorum
    # restart hit head-on. Best-effort: a failure here just logs.
    _sync_consultants_service_mode(
        service_mode, consultants_py=consultants_py, dry_run=dry_run,
    )

    # Persist the engine port the install picked (currently the
    # config default) so the verify step uses the same number the
    # task will bind.
    engine_port = int(consultants_cfg.get("engine_url",
                      "http://127.0.0.1:38095").rsplit(":", 1)[-1].rstrip("/"))
    forwarder_port = int(((smart.get("forwarder_url")
                           or "http://127.0.0.1:38096"))
                         .rsplit(":", 1)[-1].rstrip("/"))

    # M14 follow-up (2026-05-18): wire the consultants long-term-
    # memory store. Defaults to the new sqlite_vec backend + an
    # embedder borrowed from the main recall pipeline's pgvector or
    # sqlite_vec block so the engine doesn't try to embed via the
    # NullEmbedder on first store call. Non-fatal: a write failure
    # here just leaves the M14 defaults in place — the user can
    # always re-run install.py or hand-edit
    # ~/.claude/consultants-config.toml.
    try:
        _setup_consultants_store(
            cfg, consultants_py=consultants_py,
            non_interactive=non_interactive, dry_run=dry_run,
        )
    except Exception as e:
        print(f"    [warn] consultants store config setup failed: {e}")
        print(f"           consultants daemon will fall back to "
              f"sqlite_vec at ~/.claude/consultants-store.db "
              f"without an embedder — store calls will fail until "
              f"you hand-edit ~/.claude/consultants-config.toml.")

    # Platform autostart.
    if platform.system() == "Linux":
        if service_mode == "always-on":
            _install_consultants_systemd_unit(consultants_py, dry_run=dry_run)
        else:
            print("    Smart-start: the engine will be spawned by the "
                  "consultants forwarder on first request.")
            print("    Forwarder unit ships under systemd/ — install + "
                  "enable it to autostart on boot.")
    elif os.name == "nt":
        _install_consultants_windows(
            consultants_py=consultants_py,
            service_mode=service_mode,
            engine_port=engine_port,
            forwarder_port=forwarder_port,
            non_interactive=non_interactive,
            dry_run=dry_run,
        )
        # #222: a host that previously ran the OTHER service mode
        # ends up with both tasks registered + both engines running.
        # Offer to prune the stale one now that we know what mode
        # this install picked.
        _prune_stale_consultants_task(
            service_mode=service_mode,
            non_interactive=non_interactive,
            dry_run=dry_run,
        )
    elif sys.platform == "darwin":
        _install_consultants_launchd(
            consultants_py=consultants_py,
            service_mode=service_mode,
            engine_port=engine_port,
            forwarder_port=forwarder_port,
            non_interactive=non_interactive,
            dry_run=dry_run,
        )
    else:
        print("    No autostart manager wired for this platform — "
              "start the engine manually with `consultants-server` or "
              "`python -m consultants.server`.")

    return True


_CONSULTANTS_TASK_NAME = "claude-hooks-consultants"
_CONSULTANTS_FORWARDER_TASK_NAME = "claude-hooks-consultants-forwarder"

# Same XML shape as ``_DAEMON_TASK_XML`` (logon trigger, no execution
# time limit, restart on failure) — only the Description differs.
# Templated so always-on and smart-start can share it. UTF-16 on disk
# because that's what schtasks /XML expects.
_CONSULTANTS_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
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


def _write_consultants_task_xml(*, description: str, command: str,
                                arguments: str, workdir: str,
                                prefix: str) -> Path:
    """Write a UTF-16 task XML to a temp file and return its path.
    Caller cleans it up after schtasks consumes it."""
    xml = _CONSULTANTS_TASK_XML.format(
        description=_xml_escape(description),
        user_id=_xml_escape(_windows_user_id()),
        command=_xml_escape(command),
        arguments=_xml_escape(arguments),
        workdir=_xml_escape(workdir),
    )
    import tempfile  # noqa: PLC0415 — Windows-only path
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".xml")
    os.close(fd)
    Path(path).write_bytes(xml.encode("utf-16"))
    return Path(path)


def _ask_optional_float(prompt: str, *, default: Optional[float],
                          none_synonyms: tuple[str, ...] = ("never", "off")
                          ) -> tuple[bool, Optional[float]]:
    """Prompt for an optional float. Returns ``(changed, value)``.

    Blank input → ``(False, None)`` so callers know to leave the
    field unchanged. ``never`` / ``off`` → ``(True, None)`` (explicit
    "never expire" answer). Otherwise parses to float.
    """
    default_label = "never" if default is None else f"{default}"
    raw = input(f"  {prompt} [{default_label}]: ").strip()
    if not raw:
        return False, None
    if raw.lower() in none_synonyms:
        return True, None
    try:
        return True, float(raw)
    except ValueError:
        print(f"    [warn] not a number: {raw!r} — keeping default")
        return False, None


def _ask_optional_int(prompt: str, *, default: int
                       ) -> tuple[bool, Optional[int]]:
    raw = input(f"  {prompt} [{default}]: ").strip()
    if not raw:
        return False, None
    try:
        return True, int(raw)
    except ValueError:
        print(f"    [warn] not an integer: {raw!r} — keeping default")
        return False, None


def _ask_optional_bool(prompt: str, *, default: bool
                        ) -> tuple[bool, Optional[bool]]:
    label = "Y/n" if default else "y/N"
    raw = input(f"  {prompt} [{label}]: ").strip().lower()
    if not raw:
        return False, None
    if raw in ("y", "yes", "true", "1", "on"):
        return True, True
    if raw in ("n", "no", "false", "0", "off"):
        return True, False
    print(f"    [warn] not a yes/no: {raw!r} — keeping default")
    return False, None


def _customize_consultants_store_knobs(*, non_interactive: bool
                                        ) -> tuple[dict, dict]:
    """Optional interactive customization of [store.ttl] +
    [store.distillation] knobs at install time (#220).

    Returns ``(ttl_overrides, distill_overrides)`` — two dicts of
    field-name → value that the helper script applies. Empty dicts
    mean "no overrides, keep M14 defaults".

    Non-interactive runs always return empty dicts (defaults are
    correct; the operator can always tune via
    ``claude-consultants config set-store-{ttl,distillation}``).
    """
    if non_interactive:
        return {}, {}
    ans = input(
        "    Customize TTL + distillation knobs now? "
        "(M14 defaults are sensible; say n to skip) [y/N]: "
    ).strip().lower()
    if ans not in ("y", "yes"):
        return {}, {}

    ttl_over: dict = {}
    distill_over: dict = {}

    print("    [store.ttl] — episodic-memory expiry windows")
    changed, val = _ask_optional_bool(
        "TTL enabled?", default=True)
    if changed:
        ttl_over["enabled"] = bool(val)
    changed, val = _ask_optional_float(
        "Research namespace TTL (days)", default=30.0)
    if changed:
        ttl_over["research_days"] = val
    changed, val = _ask_optional_float(
        "Tool-results namespace TTL (hours)", default=24.0)
    if changed:
        ttl_over["tool_results_hours"] = val
    changed, val = _ask_optional_float(
        "Project namespace TTL (days)", default=None)
    if changed:
        ttl_over["project_days"] = val
    changed, val = _ask_optional_float(
        "User namespace TTL (days)", default=None)
    if changed:
        ttl_over["user_days"] = val
    changed, val = _ask_optional_bool(
        "Refresh expires_at on every successful recall hit?",
        default=True)
    if changed:
        ttl_over["refresh_on_read"] = bool(val)
    changed, val = _ask_optional_float(
        "Cohort jitter — fraction (#215, 0..1)", default=0.1)
    if changed and val is not None:
        if 0.0 <= val <= 1.0:
            ttl_over["jitter_pct"] = val
        else:
            print(f"    [warn] jitter_pct out of range — kept default")

    print("    [store.distillation] — semantic-memory consolidation")
    changed, val = _ask_optional_bool(
        "Distillation enabled?", default=True)
    if changed:
        distill_over["enabled"] = bool(val)
    raw = input(
        "  Distillation model [gemma4:31b-cloud]: ").strip()
    if raw:
        distill_over["model"] = raw
    changed, val = _ask_optional_float(
        "Sweep cadence (seconds, >=30)", default=3600.0)
    if changed and val is not None and val >= 30.0:
        distill_over["sweep_interval_seconds"] = float(val)
    elif changed:
        print(f"    [warn] sweep_interval_seconds < 30 — kept default")
    changed, val = _ask_optional_int(
        "Minimum entries to trigger distillation", default=3)
    if changed and val is not None and val >= 1:
        distill_over["min_entries_per_distillation"] = int(val)
    changed, val = _ask_optional_int(
        "Max session entries per prompt (cost cap)", default=50)
    if changed and val is not None and val >= 1:
        distill_over["max_session_entries"] = int(val)
    changed, val = _ask_optional_int(
        "Max distillations per sweep (#215; 0=uncapped)", default=5)
    if changed and val is not None and val >= 0:
        distill_over["max_groups_per_sweep"] = int(val)
    changed, val = _ask_optional_float(
        "Seconds between distillations within a sweep (#215)",
        default=5.0)
    if changed and val is not None and val >= 0.0:
        distill_over["pace_seconds_between_distillations"] = float(val)

    return ttl_over, distill_over


def _sync_consultants_service_mode(service_mode: str, *,
                                   consultants_py: Path,
                                   dry_run: bool) -> None:
    """Write ``[service].mode = <service_mode>`` into
    ``~/.claude/consultants-config.toml`` via the consultants-env
    ``consultants.config.set_service_mode`` mutator.

    Pre-#223 (2026-05-19) the installer wrote the chosen mode ONLY to
    ``config/claude-hooks.json`` (``hooks.consultants.smart_start.enabled``),
    leaving the consultants engine's own TOML at its previous value.
    The two-source drift was silent until the v1.8.2 pandorum reinstall
    hit a "configured for always-on but smart-start forwarder is
    answering" mismatch — the installer's restart logic was always-on-only
    and produced false-negative health-check timeouts.

    Best-effort: if the consultants env isn't installed yet, or the
    subprocess fails, this logs a warning but doesn't fail the install
    (the user can fix it later with ``claude-consultants config
    set-service-mode``). The companion drift detector in
    :func:`_detect_consultants_config_drift` is the recovery path.
    """
    if dry_run:
        print(f"    [dry-run] Would sync [service].mode = "
              f"{service_mode!r} into consultants-config.toml")
        return
    if not consultants_py.exists():
        print(f"    [warn] consultants env python not found at "
              f"{consultants_py}; skipping [service].mode sync. "
              f"Run `claude-consultants config set-service-mode "
              f"{service_mode}` after install.")
        return
    helper = (
        "import sys\n"
        "from consultants.config import set_service_mode\n"
        "set_service_mode(sys.argv[1], scope='user')\n"
        "print('ok')\n"
    )
    try:
        proc = subprocess.run(
            [str(consultants_py), "-c", helper, service_mode],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"    [warn] [service].mode sync subprocess failed: {e}")
        return
    if proc.returncode != 0:
        print("    [warn] [service].mode sync failed:")
        print(f"           {proc.stderr.strip()[-300:]}")
        print(f"           Run `claude-consultants config "
              f"set-service-mode {service_mode}` to fix manually.")
        return
    print(f"    Synced [service].mode = {service_mode!r} into "
          f"~/.claude/consultants-config.toml")


def _read_consultants_config_service_mode(
        consultants_py: Path) -> Optional[str]:
    """Return the ``[service].mode`` currently persisted in
    ``~/.claude/consultants-config.toml``, or ``None`` if the env
    isn't installed / the file doesn't exist / parsing fails.

    Used by :func:`_detect_consultants_config_drift` to compare
    against ``hooks.consultants.smart_start.enabled`` in
    ``config/claude-hooks.json`` at install start. A drift means
    one side was edited by hand (or by an older installer) without
    the other catching up.
    """
    if not consultants_py.exists():
        return None
    helper = (
        "from consultants.config import load_config\n"
        "cfg = load_config()\n"
        "print(cfg.service.mode)\n"
    )
    try:
        proc = subprocess.run(
            [str(consultants_py), "-c", helper],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    mode = proc.stdout.strip()
    return mode if mode in ("always-on", "smart-start") else None


def _detect_consultants_config_drift(cfg: dict, *,
                                     consultants_py: Path,
                                     non_interactive: bool,
                                     dry_run: bool) -> None:
    """Compare ``hooks.consultants.smart_start.enabled`` in
    ``config/claude-hooks.json`` against ``[service].mode`` in
    ``~/.claude/consultants-config.toml`` and warn (or offer to fix
    when interactive) on drift.

    Called at the START of ``_setup_consultants_engine`` BEFORE the
    user is prompted for the service mode, so the prompt can default
    to the value that's actually being honored at runtime.

    Returns the drift state as a side-effect via printed output. The
    installer continues either way — the user picks the mode they
    want from the prompt and the sync at the END of the function will
    realign both files.
    """
    if dry_run:
        return
    smart_enabled = bool((cfg.get("hooks", {})
                            .get("consultants", {})
                            .get("smart_start", {})
                            .get("enabled")))
    json_mode = "smart-start" if smart_enabled else "always-on"
    toml_mode = _read_consultants_config_service_mode(consultants_py)
    if toml_mode is None:
        # No consultants env or no TOML — fresh install, nothing to drift.
        return
    if toml_mode == json_mode:
        return
    print(f"    [drift] config/claude-hooks.json says service_mode = "
          f"{json_mode!r}, but ~/.claude/consultants-config.toml says "
          f"{toml_mode!r}.")
    print("            The runtime engine honors the TOML; the installer's "
            "restart logic honors the JSON. They must agree.")
    print("            The service-mode prompt below will resolve it: "
          "whichever mode you pick gets written to both files.")


def _setup_lsp_engine(cfg: dict, *, non_interactive: bool, dry_run: bool) -> None:
    """v1.9+: wire the bundled ``claude_hooks.lsp_engine`` daemon
    integration. Three pieces:

    1. **Detect installed language servers** via the matrix in
       ``claude_hooks.lang_servers``. Tier 1 LSs that are missing
       can be auto-installed via the host's native package manager
       (npm / go / rustup / apt / dnf / brew / scoop); Tier 2 LSs
       (lua-language-server, zls, omnisharp) are detection-only —
       the user installs them manually.

    2. **Drop a starter cclsp.json** when at least one Tier-1 LS is
       on disk and no ``cclsp.json`` already exists at the project
       root. The file is what the engine reads to know which LSP
       handles which extension — installer never touches an
       existing file.

    3. **Toggle ``hooks.lsp_engine.enabled``** based on the user's
       answer. Off by default; the prompt explains the latency
       tradeoff so an informed yes lands.

    Validate-only path: when the engine is already enabled and at
    least one Tier-1 LS is on disk, offers the same ``[V/R/S]``
    shortcut as the pgvector / sqlite_vec dialogs.

    All destructive ops (install commands, file writes) honor
    ``dry_run``; ``non_interactive`` skips every prompt that would
    otherwise auto-execute a subprocess (mirrors the
    ``feedback_install_destructive_noninteractive`` posture — never
    auto-install without explicit y/N).
    """
    eng_cfg = (cfg.setdefault("hooks", {})
               .setdefault("lsp_engine", {}))
    enabled = bool(eng_cfg.get("enabled", False))

    print("\n--- LSP engine (v1.9+) ---")
    print("  Optional: per-project session-scoped LSP daemon. Adds live")
    print("  type-error / undefined-symbol diagnostics from pyright /")
    print("  gopls / rust-analyzer / clangd / etc into PostToolUse")
    print("  alongside ruff. See docs/lsp-engine.md for the design.")

    try:
        from claude_hooks import lang_servers as _lang
    except Exception as e:  # pragma: no cover - import surface
        print(f"  Skipping — lang_servers module failed to import: {e}")
        return

    state = _lang.detect_language_servers()
    n_tier1_installed = sum(
        1 for st in state.values()
        if st.installed and st.spec.tier == 1
    )

    cclsp_target = Path(os.getcwd()) / "cclsp.json"
    cclsp_present = cclsp_target.exists()
    fully_configured = enabled and n_tier1_installed > 0 and cclsp_present

    if non_interactive:
        if not enabled:
            print("  --non-interactive: keeping LSP engine disabled "
                  "(opt-in only). Re-run install.py interactively to enable.")
            return
        # Already enabled + non-interactive: report state only.
        _lsp_print_detection_table(state)
        if cclsp_present:
            print(f"  cclsp.json: present at {cclsp_target}")
        else:
            print(f"  cclsp.json: MISSING at {cclsp_target} "
                  "(run interactively to drop a starter)")
        return

    # Validate-only shortcut when fully configured.
    if fully_configured:
        print(f"  Currently configured: enabled, {n_tier1_installed} "
              f"Tier-1 LS(s) installed, cclsp.json at {cclsp_target}")
        choice = input(
            "  [V]alidate only / [R]e-install / [S]kip? [V/r/s]: ",
        ).strip().lower()
        if not choice:
            choice = "v"
        if choice in ("s", "skip", "n", "no"):
            print("  Skipped.")
            return
        if choice in ("v", "validate", "y", "yes"):
            _lsp_print_detection_table(state)
            print(f"  cclsp.json: {cclsp_target} (present)")
            print("  LSP engine: validate-only complete. No writes performed.")
            return
        # On "r" fall through to the full re-install flow.

    # Print detection table so the user can see what's installed.
    _lsp_print_detection_table(state)

    # Offer the install loop for every missing LS where we have
    # something useful to say:
    #   - Tier 1: always include (the loop prints either an
    #     [Y/n] auto-install prompt OR a "no package manager found,
    #     install manually" pointer — both are user-visible signal).
    #   - Tier 2: only include when we DO have a usable installer —
    #     no point asking about niche LSs no one on this host has a
    #     package manager for.
    #
    # Pre-v1.9.x this filtered strictly on ``spec.tier == 1``, leaving
    # lua / zls / omnisharp permanently in the "manual only" bucket
    # even on hosts where scoop / brew / winget would have happily
    # installed them. The display table still groups by tier
    # (cosmetic).
    missing_to_offer = [
        st for st in state.values()
        if not st.installed
        # Skip the on-disk-but-not-on-PATH case — re-installing
        # something already on disk would be wasteful and the
        # detection table already shows the restart-shell hint.
        and not getattr(st, "on_disk_path", None)
        and (
            st.spec.tier == 1 or st.installer_for_missing is not None
        )
    ]
    if missing_to_offer:
        ans = input(
            "\n  Install missing language servers now? [y/N]: ",
        ).strip().lower()
        if ans in ("y", "yes"):
            # Before iterating: on Windows, offer to bootstrap scoop if
            # the user has any SCOOP-only LSs in the queue (OmniSharp is
            # the canonical case — no winget package exists). One
            # consolidated prompt rather than per-LS, so we don't
            # nag the same question three times if OmniSharp + zls +
            # lua are all queued. Bootstrap also adds the ``extras``
            # bucket so the subsequent ``scoop install extras/...``
            # commands resolve.
            state = _lsp_maybe_bootstrap_scoop(
                state, dry_run=dry_run,
            )
            # Re-build the offer list against the refreshed state —
            # what was SCOOP-only (no installer) becomes SCOOP-with-
            # installer after the bootstrap.
            missing_to_offer = [
                st for st in state.values()
                if not st.installed
                and not getattr(st, "on_disk_path", None)
                and (
                    st.spec.tier == 1
                    or st.installer_for_missing is not None
                )
            ]
            _lsp_run_install_loop(missing_to_offer, state, dry_run=dry_run)
        else:
            print("  Skipped install loop. Re-run anytime to install.")

    # Re-detect after the install loop so the starter cclsp.json
    # reflects what's now on disk.
    state = _lang.detect_language_servers()
    n_tier1_installed = sum(
        1 for st in state.values()
        if st.installed and st.spec.tier == 1
    )

    if n_tier1_installed == 0:
        print("\n  No Tier-1 language servers detected — LSP engine "
              "integration won't have anything to query. Skipping "
              "cclsp.json + enable toggle.")
        return

    # Offer starter cclsp.json.
    _lsp_offer_starter_cclsp(
        state, cclsp_target, dry_run=dry_run,
    )

    # Enable toggle.
    if enabled:
        print("\n  LSP engine integration: already enabled in config.")
    else:
        prompt_default = "Y"
        ans = input(
            "\n  Enable LSP engine hook integration "
            f"(hooks.lsp_engine.enabled = true)?\n"
            "  Adds ~10 ms to SessionStart and ~5-15 ms to PostToolUse on\n"
            "  edited files. Diagnostics show up in the same context\n"
            f"  block as ruff. [{prompt_default}/n]: ",
        ).strip().lower()
        if not ans:
            ans = prompt_default.lower()
        if ans in ("y", "yes"):
            if dry_run:
                print("  [dry-run] Would set hooks.lsp_engine.enabled = true")
            else:
                eng_cfg["enabled"] = True
            print("  LSP engine: enabled. Restart Claude Code sessions to "
                  "pick up the new hook wiring.")
        else:
            print("  Kept disabled. Re-run install.py to flip later.")


def _lsp_print_detection_table(state) -> None:
    """Print the LS detection table grouped by tier."""
    tier1 = [(n, st) for n, st in state.items() if st.spec.tier == 1]
    tier2 = [(n, st) for n, st in state.items() if st.spec.tier == 2]

    # Heading labels reflect the v1.9.x change to offer auto-install
    # for any LS with a registered package-manager command. Tier 2
    # is no longer "manual only" — it's just lower-priority / niche.
    print("\n  Tier 1 (universal, recommended):")
    for name, st in tier1:
        _lsp_print_row(name, st)
    if tier2:
        print("  Tier 2 (optional, niche languages):")
        for name, st in tier2:
            _lsp_print_row(name, st)


def _lsp_print_row(name: str, st) -> None:
    """One row of the LS detection table."""
    label = st.spec.display
    if st.installed:
        # 32-char column so the longest LS name
        # ("typescript-language-server") fits.
        print(f"    [ok]  {label:32} installed ({st.binary_path})")
        return

    # Three "missing-on-PATH" states (in preference order):
    #
    # 1. ON DISK but not on PATH — binary exists at a known install
    #    location but the shell can't see it. Most common cause: the
    #    user installed via winget LLVM.LLVM and hasn't restarted
    #    cmd.exe yet (winget added Program Files\LLVM\bin to system
    #    PATH in the registry, but the running shell took its PATH
    #    snapshot at launch). Don't offer to re-install — tell the
    #    user to restart their shell.
    # 2. Missing AND we have an installer — surface that.
    # 3. Missing with no installer — surface the manual path or docs.
    if getattr(st, "on_disk_path", None):
        # ASCII-only print — Windows cp1252 console can't encode
        # U+2192 (→) and similar arrows. The "->" arrow + bracketed
        # label survive in every console encoding.
        print(f"    [!!]  {label:32} on disk, not on PATH "
              f"({st.on_disk_path})")
        print(f"          -> restart your shell to pick up updated PATH")
        return

    suffix = "MISSING"
    if st.installer_for_missing is not None:
        suffix = f"MISSING — installable via {st.installer_for_missing.value}"
    elif st.spec.tier == 2:
        suffix = "MISSING (see docs/lsp-engine.md)"
    elif st.spec.docs_url:
        suffix = f"MISSING — install manually ({st.spec.docs_url})"
    print(f"    [!!]  {label:32} {suffix}")


def _lsp_is_windows() -> bool:
    """OS check wrapper — extracted so tests can patch this single
    function instead of monkeypatching ``os.name``. Patching the
    latter globally breaks ``pathlib.Path`` instantiation on Linux
    test hosts because Path() picks a backend off ``os.name`` at
    construction time."""
    return os.name == "nt"


def _lsp_maybe_bootstrap_scoop(state, *, dry_run: bool):
    """Offer to install scoop on Windows when the install loop has
    SCOOP-only blocked LSs (canonical: OmniSharp — no winget package).

    Returns the (possibly re-detected) state dict so the caller's
    ``missing_to_offer`` filter refreshes ``installer_for_missing``
    promotions correctly.

    Earlier v1.9.x had a "Case 2" here that silently added the
    ``extras`` bucket — but all our LSs are actually in scoop's
    ``main`` bucket (the default one added at install). Verified
    via ``scoop search`` on 2026-05-21. So no bucket-management
    needed; the install matrix dispatches plain ``scoop install
    <name>`` and that just works after a vanilla scoop install.

    Live-caught (PATH refresh / extras-bucket-wrongness saga,
    pandorum 2026-05-21).
    """
    from claude_hooks import lang_servers as _lang  # local import

    if not _lsp_is_windows():
        return state
    if _lang.is_scoop_installed():
        return state

    # Find LSs that (a) are missing, (b) have a scoop install command,
    # (c) have no currently-available installer (e.g., OmniSharp on
    # a host without scoop). zls/lua-LS have winget alternatives so
    # they don't end up in this bucket — only the truly-blocked LSs do.
    scoop_only_blocked = []
    for st in state.values():
        if st.installed or st.installer_for_missing is not None:
            continue
        if _lang.Installer.SCOOP not in st.spec.installers:
            continue
        if not _lang.INSTALL_COMMANDS.get(
            _lang.Installer.SCOOP, {},
        ).get(st.spec.name):
            continue
        scoop_only_blocked.append(st)

    if not scoop_only_blocked:
        return state

    names = ", ".join(st.spec.name for st in scoop_only_blocked)
    print(
        f"\n  These language servers can be installed via scoop on "
        f"Windows but scoop isn't on PATH: {names}."
    )
    print(
        "  scoop installs entirely to the user profile (no admin, no "
        "system PATH changes)."
    )
    print("  See https://scoop.sh/ for the project.")
    ans = input(
        "  Install scoop now to unlock these? [y/N]: ",
    ).strip().lower()
    if ans not in ("y", "yes"):
        print("  Skipped scoop bootstrap. These LSs will stay manual.")
        return state

    if dry_run:
        print("    [dry-run] Would install scoop via PowerShell.")
        return state

    print("    Running: PowerShell scoop installer...")
    ok, msg = _lang.install_scoop_windows(dry_run=False)
    if not ok:
        print(f"    [FAIL] scoop install failed: {msg}")
        print(
            "    (continuing — these LSs will stay manual; install "
            "scoop yourself from https://scoop.sh/ and re-run)"
        )
        return state
    print(f"    [ok] {msg}")

    # Re-detect — scoop is now on PATH (install_scoop_windows refreshes
    # os.environ['PATH']), so ``installer_for_missing`` promotes from
    # None to SCOOP for the affected LSs.
    return _lang.detect_language_servers()


def _lsp_run_install_loop(
    missing_specs,
    state,
    *,
    dry_run: bool,
) -> None:
    """Per-missing-spec install loop. Each [Y/n] confirmation is its
    own decision — the user can pick and choose. Toolchain-missing
    paths print a manual-install message instead of trying."""
    from claude_hooks import lang_servers as _lang  # local — kept light

    for st in missing_specs:
        installer = st.installer_for_missing
        if installer is None:
            # No installable path — surface why and skip.
            url = st.spec.docs_url or "the project's docs"
            print(f"\n  Install {st.spec.name}? Requires a package manager "
                  "not found on PATH.")
            print(f"  Install manually from {url} and re-run.")
            print(f"  [skipping {st.spec.name}]")
            continue

        cmd = _lang.INSTALL_COMMANDS.get(installer, {}).get(st.spec.name)
        if not cmd:
            print(f"\n  [skipping {st.spec.name}] no install command "
                  f"registered for {installer.value}.")
            continue

        cmd_str = " ".join(cmd)
        ans = input(
            f"\n  Install {st.spec.name} via `{cmd_str}`? [Y/n]: ",
        ).strip().lower()
        if ans and ans not in ("y", "yes"):
            print(f"  [skipping {st.spec.name}]")
            continue

        if dry_run:
            print(f"    [dry-run] Would run: {cmd_str}")
            continue

        print(f"    Running: {cmd_str}")
        ok, msg = _lang.install_language_server(
            st.spec, installer, dry_run=False,
        )
        if ok:
            # Resolve the now-installed binary path.
            import shutil as _sh
            path = _sh.which(st.spec.bin) or "(not on PATH yet)"
            # ASCII markers only — Windows cp1252 console crashes on
            # U+2713 / U+2717. Matches the existing [ok] / [FAIL] style
            # used elsewhere in the installer.
            print(f"    [ok] {st.spec.name} now at {path}")
        else:
            print(f"    [FAIL] {st.spec.name} install failed: {msg}")
            print(f"    (continuing — install manually later from "
                  f"{st.spec.docs_url or 'the docs'})")


def _lsp_offer_starter_cclsp(
    state,
    target: Path,
    *,
    dry_run: bool,
) -> None:
    """Offer to write the starter cclsp.json based on detected LSs.

    Refuses to overwrite an existing file (matches the engine's
    convention: cclsp.json is user-owned, project state, not
    installer-owned)."""
    from claude_hooks import lang_servers as _lang  # local

    if target.exists():
        print(f"\n  cclsp.json already at {target} — leaving it untouched.")
        return

    blob = _lang.starter_cclsp_json(state)
    server_names = [s["command"][0] for s in blob["servers"]]
    if not server_names:
        return

    print(f"\n  Drop a starter cclsp.json at {target}?")
    print(f"  Will include: {', '.join(server_names)}")
    ans = input("  [Y/n]: ").strip().lower()
    if ans and ans not in ("y", "yes"):
        print("  Skipped. The LSP engine will return [] for every file "
              "until a cclsp.json is provided.")
        return

    ok, msg = _lang.write_starter_cclsp_json(state, target, dry_run=dry_run)
    if ok:
        print(f"    [ok] {msg}")
    else:
        print(f"    [FAIL] {msg}")


def _setup_consultants_store(cfg: dict, *, consultants_py: Path,
                             non_interactive: bool,
                             dry_run: bool) -> None:
    """M14 follow-up — wire the consultants long-term-memory store.

    The M14 defaults (2026-05-18) ship with ``store.enabled = True``,
    ``backend = "sqlite_vec"`` at ``~/.claude/consultants-store.db``,
    TTL on, distillation on. But the consultants store needs an
    **embedder** to turn ``content`` into vectors at store /
    recall_hybrid time. Without one configured, every call falls
    back to ``NullEmbedder`` and raises ``EmbedderError``.

    This helper borrows the embedder config from the main recall
    pipeline's ``providers.pgvector`` or ``providers.sqlite_vec``
    block in ``claude-hooks.json`` — that way a host whose recall
    is already wired to llamafile/ollama gets a matching consultants
    store without re-typing the embedder block.

    Selection rule:
      1. If ``providers.pgvector.enabled = true`` AND it has an
         embedder configured → consultants backend = "pgvector",
         DSN + table + embedder copied over. A dedicated table
         (default ``consultants_store``) is used so the recall
         pipeline's ``memories_<model>`` isn't co-mingled.
      2. Else if ``providers.sqlite_vec.enabled = true`` AND it has
         an embedder → consultants backend = "sqlite_vec",
         dedicated db path under ``~/.claude/consultants-store.db``,
         embedder copied.
      3. Else → leave M14 defaults but print a warning that the
         store will fail at runtime unless the operator wires an
         embedder by hand.

    The consultants store is persisted to
    ``~/.claude/consultants-config.toml`` via the canonical
    :func:`consultants.config.save_config` so the TOML round-trips
    cleanly with the CLI's other mutators.
    """
    providers = cfg.get("providers") or {}
    pg = providers.get("pgvector") or {}
    sv = providers.get("sqlite_vec") or {}

    def _has_embedder(block: dict) -> bool:
        emb = block.get("embedder")
        return isinstance(emb, str) and bool(emb.strip())

    chosen_backend: Optional[str] = None
    embedder_name: Optional[str] = None
    embedder_options: dict = {}
    pgvector_dsn: Optional[str] = None
    pgvector_table: Optional[str] = None
    sqlite_vec_path: Optional[str] = None

    if pg.get("enabled") and _has_embedder(pg):
        chosen_backend = "pgvector"
        embedder_name = str(pg.get("embedder") or "").strip() or None
        embedder_options = dict(pg.get("embedder_options") or {})
        pgvector_dsn = str(pg.get("dsn") or "").strip() or None
        # Dedicated table keeps consultants writes separate from the
        # recall pipeline's ``memories_<model>`` so the M14 reaper
        # never touches user-curated recall data.
        pgvector_table = "consultants_store"
    elif sv.get("enabled") and _has_embedder(sv):
        chosen_backend = "sqlite_vec"
        embedder_name = str(sv.get("embedder") or "").strip() or None
        embedder_options = dict(sv.get("embedder_options") or {})
        # Always a dedicated file — never share the recall db_path so
        # the reaper can't scan recall-pipeline rows.
        sqlite_vec_path = "~/.claude/consultants-store.db"
    else:
        print("    [warn] No enabled pgvector/sqlite_vec provider "
              "with an embedder found in main config.")
        print("           Consultants store will use M14 defaults "
              "(sqlite_vec @ ~/.claude/consultants-store.db) but "
              "WITHOUT an embedder — store calls will raise.")
        print("           Hand-edit ~/.claude/consultants-config.toml "
              "and add an [store] block with embedder + "
              "embedder_options to enable the store.")
        return

    if dry_run:
        print(f"    [dry-run] Would set consultants store backend = "
              f"{chosen_backend!r} with embedder = {embedder_name!r}")
        return

    # #220 (2026-05-18): optional interactive customization of the
    # M14 TTL + distillation knobs. Defaults stay correct without
    # prompts; the interactive prompts let an operator opt into a
    # different research window, a different distillation cadence,
    # or a different distiller model at install time instead of
    # forcing them to discover `claude-consultants config set-store*`
    # after the fact.
    ttl_overrides, distill_overrides = _customize_consultants_store_knobs(
        non_interactive=non_interactive,
    )

    # Load existing TOML via the consultants-env python so we use the
    # canonical dataclass + render path (subprocess isolates the
    # heavy LangGraph imports from the main installer process).
    payload = {
        "backend": chosen_backend,
        "embedder": embedder_name,
        "embedder_options": embedder_options,
        "pgvector_dsn": pgvector_dsn,
        "pgvector_table": pgvector_table,
        "sqlite_vec_path": sqlite_vec_path,
        "ttl": ttl_overrides,
        "distillation": distill_overrides,
    }
    helper = (
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "from consultants import config as cc\n"
        "cfg = cc.load_config()\n"
        "cfg.store.enabled = True\n"
        "cfg.store.backend = payload['backend']\n"
        "if payload.get('embedder'):\n"
        "    cfg.store.embedder = payload['embedder']\n"
        "if payload.get('embedder_options'):\n"
        "    cfg.store.embedder_options = dict(payload['embedder_options'])\n"
        "if payload.get('pgvector_dsn'):\n"
        "    cfg.store.pgvector_dsn = payload['pgvector_dsn']\n"
        "if payload.get('pgvector_table'):\n"
        "    cfg.store.pgvector_table = payload['pgvector_table']\n"
        "if payload.get('sqlite_vec_path'):\n"
        "    cfg.store.sqlite_vec_path = payload['sqlite_vec_path']\n"
        "ttl = payload.get('ttl') or {}\n"
        "for k, v in ttl.items():\n"
        "    setattr(cfg.store.ttl, k, v)\n"
        "dist = payload.get('distillation') or {}\n"
        "for k, v in dist.items():\n"
        "    if k == 'fallback_models':\n"
        "        cfg.store.distillation.fallback_models = tuple(v)\n"
        "    else:\n"
        "        setattr(cfg.store.distillation, k, v)\n"
        "path = cc.save_config(cfg, scope='user')\n"
        "print(str(path))\n"
    )
    proc = subprocess.run(
        [str(consultants_py), "-c", helper],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("    [warn] consultants config write failed:")
        print(f"           {proc.stderr.strip()[-300:]}")
        return
    path = proc.stdout.strip() or "~/.claude/consultants-config.toml"
    print(f"    Consultants store wired:")
    print(f"      backend  = {chosen_backend}")
    print(f"      embedder = {embedder_name}")
    if chosen_backend == "pgvector":
        # DSN may contain a password — print only host:port/db to
        # avoid leaking credentials into install logs.
        safe_dsn = pgvector_dsn or ""
        if "@" in safe_dsn:
            safe_dsn = "***@" + safe_dsn.rsplit("@", 1)[-1]
        print(f"      dsn      = {safe_dsn}")
        print(f"      table    = {pgvector_table}")
    else:
        print(f"      db_path  = {sqlite_vec_path}")
    print(f"      written  -> {path}")


def _wait_for_consultants_health(port: int, *,
                                 timeout: float = 30.0) -> bool:
    """Poll ``http://127.0.0.1:<port>/v1/health`` until it returns
    200 or ``timeout`` elapses. Used to confirm the engine /
    forwarder is up after the scheduled task fires.

    First-poll latency on Windows is generous (~5–10 s for the engine
    cold start because LangChain imports take a beat); the forwarder
    is much faster (stdlib only). 30 s default covers both with
    headroom."""
    import time as _time  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    deadline = _time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/v1/health"
    while _time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if 200 <= r.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        _time.sleep(0.5)
    return False


def _register_consultants_task(*, task_name: str, description: str,
                               exec_command: str, exec_arguments: str,
                               workdir: str, port: int,
                               health_timeout: float,
                               non_interactive: bool) -> bool:
    """Generic helper: register a single Windows scheduled task,
    /Run it, and verify health. Used for both the always-on engine
    task and the smart-start forwarder task. Returns True iff the
    task is registered AND health-checked at the end.

    Mirrors ``_install_daemon_windows_steps`` but tighter — the
    consultants tasks don't need the daemon's special-cases (no
    pythonw fallback to .cmd, no first-run-port-bind dance)."""
    delete_argstr = f'/Delete /TN "{task_name}" /F'
    delete_argv = ["/Delete", "/TN", task_name, "/F"]
    run_argstr = f'/Run /TN "{task_name}"'
    run_argv = ["/Run", "/TN", task_name]

    # Already-installed branch: in non-interactive mode just verify;
    # otherwise prompt for re-install.
    if _windows_task_exists(task_name):
        print(f"    · scheduled task '{task_name}' already exists")
        if non_interactive:
            print("    · --non-interactive: leaving as-is, verifying health")
            if _wait_for_consultants_health(port, timeout=health_timeout):
                print(f"    · responding on 127.0.0.1:{port}")
                return True
            print(f"    [!!] not responding — try `schtasks {run_argstr}`")
            return False
        ans = input("    Re-install (delete + recreate)? [y/N]: "
                    ).strip().lower()
        if ans in ("y", "yes"):
            if not _run_schtasks_elevated(delete_argstr, delete_argv):
                print("    [!!] could not delete existing task — leaving as-is")
                return False
            # fall through to fresh install
        else:
            if _wait_for_consultants_health(port, timeout=health_timeout):
                print(f"    · responding on 127.0.0.1:{port}")
                return True
            print(f"    · not currently responding — `schtasks {run_argstr}`")
            return False

    # Fresh install path. In non-interactive mode print the schtasks
    # commands and bail (we can't fire UAC without a user).
    xml_path = _write_consultants_task_xml(
        description=description,
        command=exec_command,
        arguments=exec_arguments,
        workdir=workdir,
        prefix=f"{task_name}-",
    )
    create_argstr = f'/Create /XML "{xml_path}" /TN "{task_name}" /F'
    create_argv = ["/Create", "/XML", str(xml_path), "/TN", task_name, "/F"]
    try:
        if non_interactive:
            print("    --non-interactive: cannot prompt for UAC. "
                  "Run from an elevated cmd:")
            print(f"      schtasks {create_argstr}")
            print(f"      schtasks {run_argstr}")
            return False

        print(f"    Registering '{task_name}'...")
        print(f"      Command: {exec_command} {exec_arguments}")
        print("      UAC prompt is scoped to one schtasks call.")
        if not _run_schtasks_elevated(create_argstr, create_argv):
            print("    [!!] schtasks /Create failed (UAC declined?)")
            return False
        if not _windows_task_exists(task_name):
            print("    [!!] task not detected after /Create")
            return False
        print(f"    · task '{task_name}' registered")

        # Trigger now — LogonTrigger only fires at next logon.
        _run_schtasks_elevated(run_argstr, run_argv)
        if _wait_for_consultants_health(port, timeout=health_timeout):
            print(f"    · responding on 127.0.0.1:{port}")
            return True
        print(f"    [!!] task triggered but not responding on "
              f"127.0.0.1:{port} within {health_timeout:.0f}s")
        print(f"         Inspect: schtasks /Query /TN \"{task_name}\" /V /FO LIST")
        return False
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass


def _install_consultants_windows(*, consultants_py: Path, service_mode: str,
                                 engine_port: int, forwarder_port: int,
                                 non_interactive: bool, dry_run: bool) -> None:
    """Register the appropriate Windows scheduled task(s) for the
    consultants engine.

    - **always-on**: register a single ``claude-hooks-consultants``
      task that runs ``<consultants-env>/pythonw.exe -m
      consultants.server``. Engine listens on ``engine_port``.
    - **smart-start**: register a ``claude-hooks-consultants-forwarder``
      task running in the **main** claude-hooks env (stdlib only). The
      forwarder spawns the engine on demand into the consultants env.

    On either path we print clear `schtasks` fallback commands when
    non-interactive (UAC can't be prompted), and clean up the temp XML
    after schtasks consumes it. Mirrors the daemon's auto-install
    behaviour so /consultants is a first-class Windows citizen, not a
    CLI-only afterthought."""
    if dry_run:
        print("    [dry-run] would register Windows scheduled task")
        return

    workdir = str(HERE.resolve())

    if service_mode == "always-on":
        # pythonw avoids the cmd flash; fall back to console python only
        # if pythonw is missing (rare).
        pyw = find_conda_env_pythonw(env_name=CONSULTANTS_ENV_NAME)
        exec_path = pyw if pyw is not None else consultants_py
        if pyw is None:
            print("    [!] pythonw.exe missing in consultants env — using "
                  "python.exe; a console window will be visible.")
        ok = _register_consultants_task(
            task_name=_CONSULTANTS_TASK_NAME,
            description=("claude-hooks /consultants engine — multi-agent "
                         "council (always-on)"),
            exec_command=str(exec_path),
            exec_arguments=("-m consultants.server "
                            f"--host 127.0.0.1 --port {engine_port}"),
            workdir=workdir,
            port=engine_port,
            health_timeout=30.0,
            non_interactive=non_interactive,
        )
        if ok:
            print("    · always-on engine: ready")
        return

    # smart-start: forwarder runs in the MAIN claude-hooks env so we
    # don't carry LangChain's import cost when the engine is reaped.
    main_pyw = find_conda_env_pythonw()
    main_py = find_conda_env_python()
    if main_pyw is not None:
        exec_command = str(main_pyw)
    elif main_py.exists():
        exec_command = str(main_py)
    else:
        print("    [!!] main claude-hooks env not found — can't register "
              "the forwarder. Run `python install.py` first.")
        return
    # The forwarder spawns the engine on demand. Pass the CONSULTANTS
    # env's pythonw.exe (not python.exe) so the spawned engine doesn't
    # pop a visible console window on the user's desktop. Fall back to
    # python.exe only if pythonw is missing; consultants_forwarder.py
    # belt-and-braces with CREATE_NO_WINDOW | DETACHED_PROCESS so even
    # a python.exe fallback stays hidden.
    consultants_pyw = find_conda_env_pythonw(env_name=CONSULTANTS_ENV_NAME)
    engine_exec = consultants_pyw if consultants_pyw is not None else consultants_py
    if consultants_pyw is None:
        print("    [!] pythonw.exe missing in consultants env — engine "
              "will use python.exe (forwarder adds creationflags so no "
              "console flashes anyway).")
    forwarder_args = (
        f"-m claude_hooks.consultants_forwarder "
        f"--listen-port {forwarder_port} "
        f"--engine-python \"{engine_exec}\""
    )
    ok = _register_consultants_task(
        task_name=_CONSULTANTS_FORWARDER_TASK_NAME,
        description=("claude-hooks /consultants smart-start forwarder — "
                     "spawns engine on demand"),
        exec_command=exec_command,
        exec_arguments=forwarder_args,
        workdir=workdir,
        port=forwarder_port,
        health_timeout=15.0,
        non_interactive=non_interactive,
    )
    if ok:
        print("    · smart-start forwarder: ready")


# macOS launchd plist template — same shape as the daemon's, with
# the ProgramArguments rewritten to invoke the engine / forwarder
# directly. KeepAlive=true gives us systemd-style restart semantics.
_CONSULTANTS_LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{program_args_xml}
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logfile}</string>
  <key>StandardErrorPath</key><string>{logfile}</string>
</dict>
</plist>
"""


def _consultants_launchd_plist(*, label: str, argv: list[str],
                               workdir: str, logfile: str) -> str:
    program_args_xml = "\n".join(
        f"    <string>{_xml_escape(a)}</string>" for a in argv
    )
    return _CONSULTANTS_LAUNCHD_PLIST.format(
        label=_xml_escape(label),
        program_args_xml=program_args_xml,
        workdir=_xml_escape(workdir),
        logfile=_xml_escape(logfile),
    )


def _install_consultants_launchd(*, consultants_py: Path, service_mode: str,
                                 engine_port: int, forwarder_port: int,
                                 non_interactive: bool, dry_run: bool) -> None:
    """Register a macOS launchd LaunchAgent for the consultants
    engine (always-on) or smart-start forwarder. Mirrors
    ``_install_daemon_launchd`` but with HTTP /v1/health verification
    and a parameterised ProgramArguments so the same code handles
    both modes."""
    if dry_run:
        print("    [dry-run] would write LaunchAgent plist + launchctl load")
        return

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    home = str(Path.home())
    workdir = str(HERE.resolve())

    if service_mode == "always-on":
        label = "com.claude-hooks.consultants"
        argv = [str(consultants_py), "-m", "consultants.server",
                "--host", "127.0.0.1", "--port", str(engine_port)]
        port = engine_port
        log = f"{home}/.claude/claude-hooks-consultants.log"
        timeout = 30.0
    else:
        # Forwarder runs in the main env so LangChain doesn't load
        # until the engine is actually needed.
        main_py = find_conda_env_python()
        if not main_py.exists():
            print("    [!!] main claude-hooks env not found — can't wire "
                  "the forwarder.")
            return
        label = "com.claude-hooks.consultants-forwarder"
        argv = [str(main_py), "-m", "claude_hooks.consultants_forwarder",
                "--listen-port", str(forwarder_port),
                "--engine-python", str(consultants_py)]
        port = forwarder_port
        log = f"{home}/.claude/claude-hooks-consultants-forwarder.log"
        timeout = 15.0

    dest = plist_dir / f"{label}.plist"
    if dest.exists():
        if non_interactive:
            print(f"    · {dest.name} already installed — "
                  f"verifying health on 127.0.0.1:{port}")
            if _wait_for_consultants_health(port, timeout=5.0):
                print(f"    · responding on 127.0.0.1:{port}")
                return
            print(f"    [!!] not responding — try: "
                  f"launchctl kickstart -k gui/$(id -u)/{label}")
            return
        ans = input(f"    {dest.name} already installed. "
                    "Re-install + re-verify? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            subprocess.run(["launchctl", "unload", "-w", str(dest)],
                           capture_output=True)
            try:
                dest.unlink()
            except OSError as e:
                print(f"    [!!] could not remove {dest}: {e}")
                return
        else:
            if _wait_for_consultants_health(port, timeout=5.0):
                print(f"    · responding on 127.0.0.1:{port}")
            else:
                print(f"    [!!] not responding — try: "
                      f"launchctl kickstart -k gui/$(id -u)/{label}")
            return

    plist_content = _consultants_launchd_plist(
        label=label, argv=argv, workdir=workdir, logfile=log,
    )
    try:
        dest.write_text(plist_content, encoding="utf-8")
    except OSError as e:
        print(f"    [!!] Failed to write {dest}: {e}")
        return
    print(f"    + wrote {dest}")
    rc = subprocess.run(
        ["launchctl", "load", "-w", str(dest)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"    [!!] launchctl load failed:\n{rc.stderr.strip()[-300:]}")
        return
    print("    · loaded into launchd")
    if _wait_for_consultants_health(port, timeout=timeout):
        print(f"    · responding on 127.0.0.1:{port}")
    else:
        print(f"    [!!] not responding within {timeout:.0f}s — try: "
              f"launchctl kickstart -k gui/$(id -u)/{label}")


def _install_consultants_systemd_unit(consultants_py: Path, *,
                                      dry_run: bool) -> None:
    """Drop the always-on systemd --user unit and reload."""
    if dry_run:
        print("    [dry-run] Would install claude-hooks-consultants.service")
        return
    unit_src = HERE / "systemd" / "claude-hooks-consultants.service"
    if not unit_src.exists():
        print(f"    warning: unit file missing at {unit_src}")
        return
    user_units = Path.home() / ".config" / "systemd" / "user"
    user_units.mkdir(parents=True, exist_ok=True)
    target = user_units / "claude-hooks-consultants.service"
    text = unit_src.read_text(encoding="utf-8")
    # Substitute placeholders the unit file uses.
    text = text.replace("@PYTHON@", str(consultants_py))
    text = text.replace("@WORKINGDIR@", str(HERE))
    target.write_text(text, encoding="utf-8")
    print(f"    Wrote {target}")
    # Best-effort reload + enable.
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True)
    rc = subprocess.run(
        ["systemctl", "--user", "enable", "--now",
         "claude-hooks-consultants.service"],
        capture_output=True, text=True,
    )
    if rc.returncode == 0:
        print("    Service enabled + started.")
    else:
        print(f"    systemctl enable: {rc.stderr.strip()[-300:]}")
        print("    Run manually: "
              "systemctl --user enable --now claude-hooks-consultants.service")


def main() -> int:
    ap = argparse.ArgumentParser(prog="install.py", description="claude-hooks installer")
    ap.add_argument("--dry-run", action="store_true", help="don't write any files")
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt -- fail if a decision is needed",
    )
    ap.add_argument("--uninstall", action="store_true", help="remove claude-hooks from settings.json")
    ap.add_argument(
        "--rewire", action="store_true",
        help="override hook-path drift detection (v1.5.1+) and rewrite "
             "existing hook entries to this install.py's repo path. Without "
             "this flag, --non-interactive refuses to rewrite when existing "
             "hooks point at a different location.",
    )
    ap.add_argument(
        "--skip-daemon-restart", action="store_true",
        help="skip the end-of-install restart of claude-hooks-daemon "
             "and claude-hooks-consultants (v1.5.2+). Without this flag, "
             "install.py restarts running services at completion so they "
             "load newly-pulled code; pass this to leave the running "
             "processes alone (advanced).",
    )
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
        # pgvector and sqlite_vec have bespoke setup (we own their MCP
        # servers, install launchers system-wide, configure DSN/db-path
        # + embedder). Handled by ``_setup_pgvector_mcp`` /
        # ``_setup_sqlite_vec_mcp`` after the standard pick loop.
        if cls.name in ("pgvector", "sqlite_vec"):
            continue
        chosen[cls.name] = pick_provider(cls, report, args.non_interactive)

    # Verify each chosen provider.
    print("\n==> Verifying chosen servers...")
    for cls in REGISTRY:
        if cls.name in ("pgvector", "sqlite_vec"):
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

    # Ollama chat backend (v1.4): HyDE + reflect + consolidate.
    # These were hard-coded defaults in config.py until v1.4 — now
    # an explicit dialog so users without an Ollama instance get
    # a clean skip path. NOT used by /get-advice or /consultants
    # (those have their own model configs).
    _setup_chat_backends(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # pgvector: ask if available, install system-wide launcher, register
    # in mcpServers. Self-contained -- no detect/verify path through the
    # generic loop above.
    _setup_pgvector_mcp(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # sqlite_vec (v1.4): purely local provider — no MCP server, no
    # schema migration. The embedder dialog is shared with pgvector
    # so a user running both gets a "same as pgvector?" shortcut
    # (handled inside _setup_embedding_engine's idempotency check).
    _setup_sqlite_vec_mcp(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # Qdrant + Memory KG (v1.4): both embed server-side; the
    # installer only validates connectivity and surfaces that the
    # embedding model lives inside the MCP container. No prompts,
    # no config mutation.
    _validate_qdrant_embedding(
        cfg,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    _validate_memory_kg_embedding(
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

    # LSP engine (v1.9+): opt-in per-project LSP daemon that feeds
    # pyright / gopls / rust-analyzer / clangd / etc diagnostics into
    # PostToolUse alongside ruff. Detects installed language servers,
    # offers per-OS auto-install for missing Tier-1 servers, drops a
    # starter cclsp.json, toggles ``hooks.lsp_engine.enabled``.
    _setup_lsp_engine(
        cfg,
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
    # Offer to install the daily pgvector backup timer (opt-in under
    # providers.pgvector.enabled). Default: 01:17 local, 7 daily / 4
    # weekly / 3 monthly retention, dumps in
    # /shared/config/mcp-pgvector/backups/.
    _install_pgvector_backup_systemd(
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
        non_interactive=args.non_interactive,
        rewire=bool(getattr(args, "rewire", False)),
    )

    # PATH-friendly wrappers for every bin/* shim. Required so skills
    # that invoke the CLIs by bare name (claude-consultants,
    # claude-advisor, ...) resolve from Claude Code's bash subprocess
    # on every platform. See ``_install_bin_shim_wrappers`` for the
    # symlink-vs-wrapper rationale.
    print(f"\n==> Bin wrappers ({_shim_wrapper_dir()})")
    _install_bin_shim_wrappers(HERE, dry_run=args.dry_run)

    # Detect companion tools and install skills.
    print("\n==> Companion tools")
    installed_tools = _detect_companion_tools(cfg)

    # /consultants engine — opt-in install of the dedicated conda env
    # + service unit. Mutates installed_tools so the consultants
    # skills only install when the env is present.
    consultants_present = _install_consultants(
        cfg, cfg_path,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )
    installed_tools["claude-consultants"] = consultants_present

    _install_skills(installed_tools, non_interactive=args.non_interactive, dry_run=args.dry_run)

    # Episodic memory setup.
    _setup_episodic(cfg, cfg_path, args, dry_run=args.dry_run)

    # Optional: Claude Code env-var recommendations.
    _prompt_env_vars(
        settings_path,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
    )

    # #222 (2026-05-19): clear __pycache__ before the service restart
    # below. Stale .pyc files survived the pandorum v1.7.0→v1.8.1
    # pull and the daemon's running bytecode ended up out of sync
    # with what was on disk — the ping handshake silently rejected
    # because the protocol const had changed in a module whose .pyc
    # was loaded from cache. Clearing on every install is cheap and
    # rules the failure mode out across the board.
    if not args.dry_run:
        removed = _clear_pycache(HERE)
        if removed:
            print(f"\n==> Cleared {removed} __pycache__ director"
                  f"{'y' if removed == 1 else 'ies'} (post-pull "
                  "bytecode hygiene)")

    # v1.5.2+: pick up newly-pulled code by restarting the long-lived
    # daemon (and the consultants engine if installed). Without this,
    # the running pythonw process keeps executing whatever bytecode
    # was loaded at its spawn time, so `git pull && python install.py`
    # has no observable effect until the next host reboot or manual
    # restart.
    # #223: thread the loaded config through so the restart logic
    # can target the right consultants task + port for the host's
    # selected service mode (smart-start vs always-on), and so the
    # state report can flag mode-vs-runtime drift.
    _restart_managed_services(
        dry_run=args.dry_run,
        skip=bool(getattr(args, "skip_daemon_restart", False)),
        cfg=cfg,
    )

    # #222: end-of-install state report so the operator can confirm
    # the expected services are up + no duplicates / orphans remain.
    # #223: cfg threaded in so the report knows which port to probe
    # first (matches the chosen service mode) and can surface drift.
    if not bool(getattr(args, "skip_daemon_restart", False)):
        _service_state_report(dry_run=args.dry_run, cfg=cfg)

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
    non_interactive: bool = False,
    rewire: bool = False,
) -> None:
    """Merge claude-hooks entries into ``settings.json``.

    v1.5.1+ path-drift safeguard: if existing ``_managedBy``
    entries point at a different ``repo_path`` than this run,
    refuse in ``--non-interactive`` mode (raises
    :class:`HookPathDrift` with exit code 2) and prompt in
    interactive mode unless ``rewire=True`` is explicitly passed.
    Backup is named ``.bak-<ts>-hook-rewrite`` so the recovery
    trail is obvious.
    """
    settings = _load_json(settings_path)

    # ---- Path-drift detection ------------------------------------ #
    existing_repo = _extract_existing_hook_repo_path(settings)
    current_repo = Path(str(repo_path).replace("\\", "/"))
    drift = (
        existing_repo is not None
        and existing_repo != current_repo
    )
    if drift and not rewire:
        if non_interactive:
            raise HookPathDrift(current_repo, existing_repo)
        print(
            f"\n  [!!] Existing hooks point at: {existing_repo}\n"
            f"       This install.py is from:   {current_repo}\n"
            f"       Rewriting will un-deploy the existing install.\n"
        )
        ans = input("  Rewire hooks to the new path? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Skipped hook rewrite. Existing wiring preserved.")
            return

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
    reason = "hook-rewrite" if drift else "hook-install"
    bak = _backed_up_save_json(
        settings_path, settings, reason=reason, dry_run=False,
    )
    if bak is not None:
        print(f"  Backup written: {bak}")
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
    # Remove any bin/* wrappers we previously installed. Tagged-only --
    # hand-rolled wrappers under the same name are left alone.
    wrappers_removed = _remove_bin_shim_wrappers(dry_run=dry_run)
    if wrappers_removed:
        print(f"  Removed {wrappers_removed} bin shim wrapper(s) from {_shim_wrapper_dir()}")
    if dry_run:
        print("[dry-run] Not writing.")
        return 0
    bak = _backed_up_save_json(
        settings_path, settings, reason="uninstall",
    )
    if bak is not None:
        print(f"  Backup written: {bak}")
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


def backup_path(p: Path, reason: str = "save") -> Path:
    """Return a timestamped backup path with a semantic reason suffix.

    ``reason`` is a short kebab-case tag (e.g. ``hook-rewrite``,
    ``plugin-enable``, ``env-vars``) so a directory full of
    ``settings.json.bak-*`` files is readable at a glance. Defaults
    to ``save`` for callers that don't supply context.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)
    return p.with_suffix(p.suffix + f".bak-{ts}-{safe}")


def _backed_up_save_json(
    path: Path, data: dict, *, reason: str, dry_run: bool = False,
) -> Optional[Path]:
    """Atomically replace ``path`` with ``data`` as JSON, making a
    timestamped backup first when ``path`` already exists.

    Returns the backup path that was written (or ``None`` if no
    backup was made — fresh file or dry-run). Every save against
    ``settings.json`` should funnel through here so a destructive
    change always leaves a recovery trail. The ``reason`` tag is
    embedded in the backup filename so ``dir /b settings.json.bak-*``
    tells you what each backup is for.
    """
    if dry_run:
        return None
    bak: Optional[Path] = None
    if path.exists():
        bak = backup_path(path, reason=reason)
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_json(path, data)
    return bak


# --------------------------------------------------------------------- #
# Hook-path drift detection (v1.5.1+)
# --------------------------------------------------------------------- #

def _extract_existing_hook_repo_path(settings: dict) -> Optional[Path]:
    """Inspect existing ``_managedBy: claude-hooks`` entries in
    ``settings`` and return the repo path they currently point at.

    Returns ``None`` if no managed entries exist (fresh install) or
    if their commands don't follow the expected
    ``<repo>/bin/claude-hook[.cmd] <Event>`` shape. When multiple
    distinct repo paths are detected (truly broken state), returns
    the one that appears most often — the caller's path-drift
    comparison still surfaces the mismatch.
    """
    counts: dict[Path, int] = {}
    hooks = (settings or {}).get("hooks") or {}
    for blocks in hooks.values():
        if not isinstance(blocks, list):
            continue
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("_managedBy") != "claude-hooks":
                continue
            for h in (blk.get("hooks") or []):
                cmd = (h or {}).get("command") or ""
                # Command shape:
                #   "<repo>/bin/claude-hook <Event>"   (POSIX)
                #   "<repo>\\bin\\claude-hook.cmd <Event>"  (Windows alt)
                # Strip the trailing event word and the bin/* segment.
                for marker in ("/bin/claude-hook", "\\bin\\claude-hook"):
                    idx = cmd.find(marker)
                    if idx > 0:
                        repo = cmd[:idx]
                        # Normalize Windows backslashes for comparison
                        repo_norm = Path(repo.replace("\\", "/"))
                        counts[repo_norm] = counts.get(repo_norm, 0) + 1
                        break
    if not counts:
        return None
    # Most common wins; ties broken by lexical order for determinism.
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]


class HookPathDrift(SystemExit):
    """Raised when install.py is invoked from a different repo path
    than the one currently wired into ``settings.json`` and the user
    has not explicitly confirmed the rewrite."""

    def __init__(self, current: Path, existing: Path):
        msg = (
            f"\n  [REFUSED] Hook-path drift detected.\n"
            f"  Existing _managedBy hooks point at: {existing}\n"
            f"  This install.py is running from:    {current}\n"
            f"  Rewriting would silently un-deploy the existing install.\n"
            f"  Re-run interactively to confirm, or run install.py from\n"
            f"  the existing path, or pass --rewire to override.\n"
        )
        super().__init__(msg)
        self.code = 2
        self.current = current
        self.existing = existing


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
    ("reflect",            None),    # built-in: uses claude-hooks reflect module
    ("consolidate",        None),    # built-in: uses claude-hooks consolidate module
    ("save-learning",      None),    # standalone
    ("find-skills",        None),    # standalone
    ("setup-caliber",      "caliber"),  # needs caliber installed
    ("episodic",           None),    # queries remote episodic-server API
    ("wrapup",             None),    # session state summary for hand-off / compact
    # /get-advice — single dispatcher skill (v1.3+); routes by verb
    # (`ask` default-implicit, `model`, `effort`, `tools`) through the
    # already-subcommand-aware bin/claude-advisor CLI.
    ("get-advice",         None),    # LLM-to-LLM advisor (uses bin/claude-advisor)
    # /consultants — single dispatcher skill (v1.3+); routes by verb
    # (`ask` default-implicit, `followup`, `list`, `show`, `config`)
    # through the already-subcommand-aware bin/claude-consultants CLI.
    # The skill requires the ``claude-hooks-consultants`` conda env
    # which install.py creates on user opt-in via _install_consultants().
    # When the consultants env is missing the skill is skipped silently.
    ("consultants",        "claude-consultants"),
]

# Legacy per-verb skill dirs from v1.2 and earlier. Removed
# unconditionally on every install run so upgraders don't end up
# with stale `--variant` slash commands cluttering the menu.
LEGACY_SKILL_DIRS: tuple[str, ...] = (
    "get-advice--model", "get-advice--effort", "get-advice--tools",
    "consultants--list", "consultants--show",
    "consultants--config", "consultants--followup",
)


def _detect_companion_tools(cfg: Optional[dict] = None) -> dict[str, bool]:
    """Check which companion tools are installed. Returns {name: bool}.

    Special-case: the ``episodic-memory`` Node binary is only needed
    on hosts that run the server. On a CLIENT-mode host (cfg.episodic
    .mode == "client") the host POSTs to a remote server and never
    invokes the local binary; report ``n/a (CLIENT)`` instead of
    ``MISSING`` so the warning doesn't keep nagging users to install
    a tool they don't need. Same shape as the v1.6.1 proxy-detection
    fix — don't conflate "is this binary on disk?" with "is this
    host supposed to have it?".
    """
    ep_mode = ((cfg or {}).get("episodic") or {}).get("mode", "off")
    result: dict[str, bool] = {}
    for bin_name, npm_pkg, importance, description in COMPANION_TOOLS:
        found = shutil.which(bin_name) is not None
        if bin_name == "episodic-memory" and not found and ep_mode == "client":
            print(f"  [ok] {bin_name:24} {'n/a (CLIENT)':12} "
                  f"[{importance}] server-only; this host posts to a remote")
            # Record as found so the "can install via npm" hint below
            # doesn't bait the user. The actual install pathway runs
            # on the server host.
            result[bin_name] = True
            continue
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
        bak = _backed_up_save_json(
            settings_path, settings, reason="plugin-marketplace",
        )
        if bak is not None:
            print(f"  Backup written: {bak}")
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
        bak = _backed_up_save_json(
            settings_path, settings, reason="plugin-enable",
        )
        if bak is not None:
            print(f"  Backup written: {bak}")

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

    # Legacy cleanup — remove `--variant` dirs from v1.2 and earlier
    # so the slash-command menu doesn't show stale entries after
    # the v1.3 dispatcher-skill consolidation. Idempotent: no-op on
    # fresh installs, removes on upgrades, no-op on re-runs.
    for legacy in LEGACY_SKILL_DIRS:
        stale = user_skills_dir / legacy
        if stale.exists():
            print(f"  [rm] /{legacy:20} removed (v1.3: collapsed into parent skill)")
            if not dry_run:
                shutil.rmtree(stale)

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

    bak = backup_path(settings_path, reason="env-vars-cli")
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
