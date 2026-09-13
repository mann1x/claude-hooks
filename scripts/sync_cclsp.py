#!/usr/bin/env python3
"""Reconcile ``cclsp.json`` with the language servers actually installed.

``install.py`` writes a starter ``cclsp.json`` from whatever is present
at setup time and never revisits it. Servers installed afterwards are
therefore never wired, and nothing says so: an unmapped extension means
no server claims the file, so the engine returns an empty diagnostic
list — which is exactly what a clean file returns.

That is not hypothetical. On 2026-09-13 pandorum had nine servers
installed and four of them unmapped — typescript-language-server,
bash-language-server, lua-language-server and zls — because they were
installed after the config was generated. TypeScript had been silently
dead ever since.

This script is the reconciliation step:

* every server in :data:`claude_hooks.lang_servers.SPECS` that resolves
  on ``PATH`` gets an entry,
* existing entries keep their command (a hand-tuned one is not
  overwritten) and gain only the extensions they are missing,
* a server that is *not* installed is never added, and an entry for a
  server that has since been uninstalled is reported but left alone —
  removing it would break a host that shares the file over a checkout.

There are **two** consumers of a ``cclsp.json`` and they read different
files. Syncing one and declaring the subsystem healthy is how this bug
survived its own fix:

* the **built-in engine** reads ``<project>/cclsp.json``;
* the **``lsp`` MCP server** (the third-party ``cclsp`` binary, also used
  by VS Code) reads ``$CCLSP_CONFIG_PATH`` — in practice
  ``~/.config/cclsp/cclsp.json``, a user-global file no project ever
  touches.

On 2026-09-13 the project file had 9 servers and the MCP file had 5, on
both hosts. Asking the MCP about a ``.js`` file returned "No LSP servers
found", while the engine answered the same question correctly, because
typescript / bash / lua / zig had only ever been added to the project
file. ``--mcp`` reconciles the MCP's file as well.

Commands written to the MCP file are **resolved through**
``shutil.which``, which honours ``PATHEXT``. That matters only on
Windows and it matters a lot: ``cclsp`` spawns the binary by the name in
the config, and a bare ``typescript-language-server`` does not resolve to
a ``.CMD`` shim there — the same ``CreateProcess`` trap the engine hit in
v1.16.0.

Usage::

    scripts/sync_cclsp.py              # report, change nothing
    scripts/sync_cclsp.py --write      # apply
    scripts/sync_cclsp.py --write --project /path/to/repo
    scripts/sync_cclsp.py --mcp        # report on the MCP's config too
    scripts/sync_cclsp.py --write --mcp

Exit code is 1 in report mode when something is missing, so it can gate
a check; 0 after a successful ``--write``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path, PurePath

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from claude_hooks.lang_servers import SPECS  # noqa: E402
from claude_hooks.lsp_engine.lsp import language_id_for  # noqa: E402


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"servers": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    data.setdefault("servers", [])
    return data


DEFAULT_MCP_CONFIG = Path.home() / ".config" / "cclsp" / "cclsp.json"

# Executable suffixes a configured command may carry on Windows. Matching
# has to see through them, and through absolute paths: the MCP config
# spells the same server as `/root/go/bin/gopls` on Linux and
# `gopls.exe` on Windows, while SPECS knows it only as `gopls`. Keying on
# the raw string made every already-mapped server look unmapped and
# proposed a duplicate entry for it.
_EXE_SUFFIXES = (".cmd", ".exe", ".bat", ".ps1")


def _bin_key(command: str) -> str:
    """Normalise a configured command to a comparable binary name."""
    name = PurePath(command.replace("\\", "/")).name.lower()
    for suffix in _EXE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def mcp_config_path() -> Path:
    """Where the ``lsp`` MCP server reads its config from.

    Resolution order: the live ``CCLSP_CONFIG_PATH`` env var, then
    whatever ``~/.claude.json`` declares for the cclsp MCP entry, then
    the conventional default. The middle step is the one that matters —
    the MCP is launched by Claude Code with that env var set, so the
    config it *actually* reads is recorded there and nowhere else.
    """
    env = os.environ.get("CCLSP_CONFIG_PATH")
    if env:
        return Path(env)

    claude_json = Path.home() / ".claude.json"
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MCP_CONFIG

    for entry in (data.get("mcpServers") or {}).values():
        if not isinstance(entry, dict):
            continue
        if "cclsp" not in str(entry.get("command", "")).lower():
            continue
        declared = (entry.get("env") or {}).get("CCLSP_CONFIG_PATH")
        if declared:
            return Path(declared)

    return DEFAULT_MCP_CONFIG


def reconcile(cfg: dict, *, resolve_commands: bool = False) -> tuple[dict, list[str]]:
    """Return ``(new_cfg, notes)``.

    ``resolve_commands`` rewrites the command of *newly added* entries
    to the absolute path ``shutil.which`` reports. Existing entries keep
    their command either way — a hand-tuned one is not overwritten.
    """
    by_bin = {
        _bin_key(s["command"][0]): s
        for s in cfg["servers"]
        if s.get("command")
    }
    notes: list[str] = []

    for spec in SPECS:
        resolved = shutil.which(spec.bin)
        entry = by_bin.get(_bin_key(spec.bin))
        if resolved is None:
            if entry is not None:
                notes.append(
                    f"  note   {spec.bin}: mapped but not installed — left "
                    f"alone (another host may share this file)")
            continue

        # An extension the engine would announce as "plaintext" is worse
        # than an unmapped one: the server accepts the document and
        # silently declines to analyse it.
        wanted = [e for e in spec.extensions
                  if language_id_for(f"x.{e}") != "plaintext"]
        skipped = sorted(set(spec.extensions) - set(wanted))
        if skipped:
            notes.append(
                f"  SKIP   {spec.bin}: {','.join(skipped)} have no languageId "
                f"in lsp.py — add them there first")

        if entry is None:
            command = list(spec.cclsp_command)
            if resolve_commands:
                # `which` honours PATHEXT; CreateProcess does not. A bare
                # name here is invisible to cclsp on Windows.
                command[0] = resolved
            cfg["servers"].append({
                "extensions": wanted,
                "command": command,
            })
            notes.append(f"  ADD    {spec.bin} -> {','.join(wanted)}")
            continue

        have = set(entry.get("extensions") or [])
        missing = [e for e in wanted if e not in have]
        if missing:
            entry["extensions"] = sorted(have | set(missing))
            notes.append(f"  EXTEND {spec.bin} += {','.join(missing)}")

    return cfg, notes


def _sync_one(path: Path, *, label: str, write: bool,
              resolve_commands: bool) -> tuple[bool, bool]:
    """Sync a single config file. Returns ``(changed, wrote)``."""
    cfg = _load(path)
    before = json.dumps(cfg, sort_keys=True)
    cfg, notes = reconcile(cfg, resolve_commands=resolve_commands)
    changed = json.dumps(cfg, sort_keys=True) != before

    print(f"\n{label} — {path}")
    if not notes:
        print("  in sync with the installed servers")
        return False, False
    for n in notes:
        print(n)

    if not changed or not write:
        return changed, False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return True, True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=".",
                    help="project root holding cclsp.json (default: cwd)")
    ap.add_argument("--write", action="store_true",
                    help="apply the changes (default: report only)")
    ap.add_argument("--mcp", action="store_true",
                    help="also reconcile the config the `lsp` MCP server "
                         "reads (CCLSP_CONFIG_PATH). Different file, "
                         "different consumer — syncing only the project "
                         "one leaves the MCP stale.")
    ap.add_argument("--mcp-path", default=None,
                    help="override the MCP config location")
    a = ap.parse_args()

    project_path = Path(a.project).resolve() / "cclsp.json"
    changed, wrote = _sync_one(
        project_path, label="engine cclsp.json",
        write=a.write, resolve_commands=False,
    )

    if a.mcp:
        mcp_path = Path(a.mcp_path) if a.mcp_path else mcp_config_path()
        m_changed, m_wrote = _sync_one(
            mcp_path, label="MCP cclsp.json",
            # Windows: cclsp spawns by the configured name, and a bare
            # name does not resolve to a .CMD shim.
            write=a.write, resolve_commands=True,
        )
        changed = changed or m_changed
        wrote = wrote or m_wrote

    if not changed:
        return 0
    if not a.write:
        print("\n  report only — re-run with --write to apply")
        return 1

    if wrote:
        print(f"\n  written. Restart the engine to pick it up:\n"
              f"    python -m claude_hooks.lsp_engine restart "
              f"--project {project_path.parent}")
        if a.mcp:
            print("  The `lsp` MCP server reloads its config at startup — "
                  "restart the Claude Code session (and VS Code, if it "
                  "shares the file) for MCP-side changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
