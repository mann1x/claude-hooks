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
        print(f"  Found: '{c.server_key}' → {c.url}  ({c.notes})")
        if non_interactive:
            return c
        ans = input(f"  Use this? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            return c
        return None
    print(f"  Multiple candidates:")
    for i, c in enumerate(cands, 1):
        print(f"    [{i}] '{c.server_key}' → {c.url}  ({c.source}, {c.confidence})")
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
        # Drop our own previous entries (anything tagged _managedBy).
        cleaned: list[dict] = []
        for blk in existing:
            if not isinstance(blk, dict):
                continue
            kept_hooks = [
                h
                for h in (blk.get("hooks") or [])
                if not (isinstance(h, dict) and h.get("_managedBy") == MANAGED_BY)
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
                if not (isinstance(h, dict) and h.get("_managedBy") == MANAGED_BY)
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
]


def _detect_companion_tools() -> dict[str, bool]:
    """Check which companion tools are installed. Returns {name: bool}."""
    result: dict[str, bool] = {}
    for bin_name, npm_pkg, importance, description in COMPANION_TOOLS:
        found = shutil.which(bin_name) is not None
        status = "installed" if found else "MISSING"
        marker = "  ✓" if found else "  ✗"
        print(f"{marker} {bin_name:24} {status:12} [{importance}] {description}")
        result[bin_name] = found

    missing = [(n, pkg, imp, desc) for n, pkg, imp, desc in COMPANION_TOOLS
               if not result[n] and pkg is not None]
    if missing:
        print(f"\n  {len(missing)} tool(s) can be installed via npm:")
        for bin_name, npm_pkg, importance, _ in missing:
            print(f"    npm install -g {npm_pkg}")
    return result


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
                print(f"  ✓ /{skill_name:20} up to date")
                continue
            else:
                to_install.append(skill_name)
                print(f"  ↑ /{skill_name:20} will update")
        else:
            to_install.append(skill_name)
            print(f"  + /{skill_name:20} will install")

    for skill_name, reason in skipped:
        print(f"  ⊘ /{skill_name:20} skipped ({reason})")

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
        print(f"  ✓ /{skill_name} installed")


if __name__ == "__main__":
    raise SystemExit(main())
