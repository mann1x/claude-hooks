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

# The conda env Python that bin/claude-hook prefers at runtime.
CONDA_PY_LINUX = Path.home() / "anaconda3" / "envs" / "claude-hooks" / "bin" / "python"
CONDA_PY_WIN = Path.home() / "anaconda3" / "envs" / "claude-hooks" / "python.exe"

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

# PreToolUse is opt-in — added only if the user enabled it in config.
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


def _find_conda() -> Optional[str]:
    """Find the conda executable, trying common locations."""
    # Check if conda is already on PATH (e.g. env is active).
    if shutil.which("conda"):
        return "conda"
    # Try known install locations.
    for candidate in [
        Path.home() / "anaconda3" / "condabin" / "conda",
        Path.home() / "miniconda3" / "condabin" / "conda",
        Path("/opt/conda/condabin/conda"),
        Path.home() / "anaconda3" / "condabin" / "conda",
        Path.home() / "miniconda3" / "condabin" / "conda",
    ]:
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

    Linux-only (systemd). Idempotent — skips units that already
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
            print(f"  · {install_name} already installed — leaving as-is")
            continue
        src = src_dir / src_name
        if not src.exists():
            print(f"  [!!] {src} missing — skipping")
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


_CALIBER_PROXY_UNIT = "caliber-grounding-proxy.service"


def _install_caliber_proxy_systemd(
    cfg: dict, *, non_interactive: bool, dry_run: bool,
) -> None:
    """Install the caliber-grounding-proxy systemd unit when
    ``caliber_proxy.enabled`` is true in config. Linux-only;
    idempotent — skips if the unit is already installed.

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
        return  # idempotent — leave existing unit alone

    print("\n==> caliber-grounding-proxy systemd unit")
    if not src.exists():
        print(f"  [!!] {src} missing — skipping")
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
        print(f"  [!!] {src} missing — skipping")
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
    other per-process state across hook invocations — saves ~150-300 ms
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

    The daemon itself is OPTIONAL — installs that skip this step still
    work because the client falls back to in-process dispatch when the
    daemon isn't running. So this prompt always defaults to "yes" but
    a "no" is harmless.
    """
    cfg_section = (cfg.get("hooks") or {}).get("daemon") or {}
    if cfg_section.get("enabled") is False:
        # Explicit opt-out via config — respect it without prompting.
        return

    print("\n==> claude-hooks-daemon (long-lived hook executor)")
    print("    Owns providers, HyDE cache, and Python interpreter across")
    print("    hook calls — saves ~150-300 ms per hook.")
    print("    OPTIONAL: hooks fall back to in-process dispatch when the")
    print("    daemon isn't running, so skipping this is safe.")

    if dry_run:
        print("  [dry-run] skipping daemon install.")
        return

    if not non_interactive:
        ans = input("  Install + enable claude-hooks-daemon? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("  Skipped.")
            return

    if os.name == "nt":
        _install_daemon_windows()
        return

    # POSIX path: try systemd first, then launchd.
    if Path("/etc/systemd/system").is_dir():
        _install_daemon_systemd()
        return
    if sys.platform == "darwin":
        _install_daemon_launchd()
        return
    print("  [!!] No supported autostart manager (systemd / launchd) detected.")
    print("       Run manually:  bin/claude-hooks-daemon")


def _install_daemon_systemd() -> None:
    src = HERE / "systemd" / _DAEMON_UNIT
    dest = Path("/etc/systemd/system") / _DAEMON_UNIT
    if dest.exists():
        print(f"  · {_DAEMON_UNIT} already installed — leaving as-is")
        return
    if not src.exists():
        print(f"  [!!] {src} missing — skipping")
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
    if rc.returncode == 0:
        print(f"  · enabled + started {_DAEMON_UNIT}")
    else:
        print(f"  [!!] enable failed:\n{rc.stderr.strip()[-300:]}")


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


def _install_daemon_launchd() -> None:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    dest = plist_dir / "com.claude-hooks.daemon.plist"
    if dest.exists():
        print(f"  · {dest} already installed — leaving as-is")
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
    if rc.returncode == 0:
        print("  · loaded into launchd (will start at next login + now)")
    else:
        print(f"  [!!] launchctl load failed:\n{rc.stderr.strip()[-300:]}")


def _install_daemon_windows() -> None:
    """On Windows we don't auto-register a scheduled task (needs admin).

    Instead, print the exact `schtasks` invocation the user can run from
    an elevated PowerShell or cmd. The shim ``bin/claude-hooks-daemon.cmd``
    is what the task should execute.
    """
    cmd_path = (HERE / "bin" / "claude-hooks-daemon.cmd").resolve()
    print("  Windows: register the daemon as a logon-triggered task")
    print("  by running the following from an elevated cmd:")
    print()
    print(
        f'  schtasks /Create /SC ONLOGON /TN "claude-hooks-daemon" '
        f'/TR "\\"{cmd_path}\\"" /RL HIGHEST /F'
    )
    print()
    print("  Or use pm2-windows-startup if you already manage Node-style")
    print("  background services via pm2. Both approaches work; pick one.")


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

    conda_py = CONDA_PY_WIN if os.name == "nt" else CONDA_PY_LINUX
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
        print("  --non-interactive: installing httpx[http2]…")
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
    probe — if it imports cleanly we move on; otherwise we describe what
    the user gains by installing and ask. Defaults to ``Y`` to keep the
    installer feeling forward.
    """
    conda_py = CONDA_PY_WIN if os.name == "nt" else CONDA_PY_LINUX
    py = str(conda_py) if conda_py.exists() else sys.executable

    print("\n==> code_graph optional extras")
    print("    code_graph runs without these — they unlock additional features.")
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
            print("    --non-interactive: installing…")
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
    conda_py = CONDA_PY_WIN if os.name == "nt" else CONDA_PY_LINUX
    in_conda = os.environ.get("CONDA_DEFAULT_ENV") == "claude-hooks"

    if conda_py.exists():
        if in_conda:
            print(f"Conda env:      claude-hooks (active)")
        else:
            print(f"Conda env:      claude-hooks (exists, not active)")
        print(f"Hook runtime:   {conda_py}")
        return

    # Env doesn't exist — offer to create it.
    print("Conda env:      NOT FOUND")
    conda_bin = _find_conda()
    if not conda_bin:
        print("  conda not found on this system — skipping env setup.")
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
        print(f"  Done — conda env ready.")
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
        help="never prompt — fail if a decision is needed",
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
        chosen[cls.name] = pick_provider(cls, report, args.non_interactive)

    # Verify each chosen provider.
    print("\n==> Verifying chosen servers...")
    for cls in REGISTRY:
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
    # units (Linux only; idempotent — skips units already installed).
    _install_proxy_stack_systemd(
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
    # spinning up a fresh interpreter — saves 150-300 ms per hook. The
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

    conda_py = CONDA_PY_WIN if os.name == "nt" else CONDA_PY_LINUX
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

    # Substitute the {cmd} placeholder.
    for event, blocks in template.items():
        for block in blocks:
            for h in block["hooks"]:
                h["command"] = h["command"].format(cmd=cmd)

    settings.setdefault("hooks", {})
    for event, blocks in template.items():
        existing = settings["hooks"].get(event) or []
        # Drop ALL previous claude-hooks entries — by _managedBy tag OR
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
        print(f"  No settings at {settings_path} — nothing to do.")
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
    # Windows paths use backslashes — convert to forward slashes so bash
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
        # Not configured — mention availability.
        print(f"\n  Episodic memory: not configured (use --episodic-server or --episodic-client URL)")


def _fix_plugin_paths() -> None:
    """Fix stale paths in plugin JSON files.

    If files were copied from another machine (e.g. Linux paths on Windows),
    rewrite paths to use local directories. Handles both installed_plugins.json
    (installPath) and known_marketplaces.json (installLocation).
    """
    plugins_dir = Path(os.path.expanduser("~/.claude/plugins"))
    total_fixed = 0

    # Fix installed_plugins.json — installPath entries.
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

    # Fix known_marketplaces.json — installLocation entries.
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
    ~/.claude/settings.json. Defaults to No — nothing is applied without
    explicit user consent.

    Covers:
      - CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1  (kills subagent Warmup
        drain; see docs/issue-warmup-token-drain.md + #47922).
      - The "bcherny stack" (DISABLE_ADAPTIVE_THINKING +
        MAX_THINKING_TOKENS + AUTO_COMPACT_WINDOW +
        AUTOCOMPACT_PCT_OVERRIDE). Default No — per our field test it
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
        print(f"  [!!] Existing settings.json 'env' is not an object — aborting.")
        return
    for k, v in to_set.items():
        env[k] = v
    _save_json(settings_path, settings)
    print(f"  Updated: {settings_path}")
    for k, v in to_set.items():
        print(f"    {k}={v}")


if __name__ == "__main__":
    raise SystemExit(main())
