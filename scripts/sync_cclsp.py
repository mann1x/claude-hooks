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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePath
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from claude_hooks.lang_servers import (  # noqa: E402
    SPECS, server_version, version_warning,
)
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


# npm's Windows shim names node.exe first, then the script it runs:
#
#   ... & "%_prog%"  "%dp0%\node_modules\pyright\langserver.index.js" %*
#
# Targets are not reliably ``.js``: typescript-language-server points at
# a ``.mjs`` and vscode-langservers-extracted at an extensionless file.
# So capture every %dp0%-relative token, drop the interpreter, and take
# the last one.
_SHIM_TARGET_RE = re.compile(r'"%dp0%\\+([^"]+)"', re.IGNORECASE)
_NODE_RE = re.compile(r"node(\.exe)?$", re.IGNORECASE)


def _node_shim_target(shim: Path) -> Optional[Path]:
    """Return the script an npm ``.cmd`` shim wraps, if resolvable."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    targets = [t for t in _SHIM_TARGET_RE.findall(text)
               if not _NODE_RE.fullmatch(t)]
    if not targets:
        return None
    target = shim.parent / targets[-1].replace("\\", os.sep)
    return target if target.is_file() else None


def _find_entry(cfg: dict, spec) -> Optional[dict]:
    """Locate the entry that represents ``spec``, if any.

    Matching on ``command[0]`` alone is not enough once a command has
    been rewritten to ``["node", "<script>"]``: every rewritten entry
    then keys as ``node``, so the next run recognises none of them and
    appends a duplicate for each. That happened — pandorum's config grew
    from 12 servers to 14, with html/json/bash/ts listed twice.

    Falling back to **extension overlap** is both robust and faithful:
    it is exactly how cclsp itself resolves a server
    (``servers.filter(s => s.extensions.includes(ext))``), so an entry
    that would answer for this spec's files *is* this spec's entry,
    whatever its command says.
    """
    for entry in cfg["servers"]:
        cmd = entry.get("command") or []
        if cmd and _bin_key(cmd[0]) == _bin_key(spec.bin):
            return entry
    for entry in cfg["servers"]:
        if set(entry.get("extensions") or []) & set(spec.extensions):
            return entry
    return None


def dedupe_servers(cfg: dict) -> list[str]:
    """Collapse entries that claim overlapping extensions.

    Only duplicates are dropped — an entry for a server that is merely
    uninstalled is still left alone, since another host may share the
    file. Returns notes describing what was removed.
    """
    notes: list[str] = []
    kept: list[dict] = []
    claimed: set[str] = set()
    for entry in cfg["servers"]:
        exts = set(entry.get("extensions") or [])
        overlap = exts & claimed
        if overlap and exts <= claimed:
            cmd = (entry.get("command") or ["?"])[0]
            notes.append(
                f"  DEDUPE {PurePath(cmd).name} -> dropped duplicate for "
                f"{','.join(sorted(exts))}")
            continue
        claimed |= exts
        kept.append(entry)
    cfg["servers"] = kept
    return notes


def spawnable_command(resolved: str, args: list[str]) -> tuple[list[str], bool]:
    """Return ``(command, rewritten)`` that ``cclsp`` can actually spawn.

    cclsp calls ``child_process.spawn(cmd, args, {shell: false})``. Since
    the fix for CVE-2024-27980, Node **refuses** to execute a ``.cmd`` or
    ``.bat`` file that way — it raises ``EINVAL`` before the process
    exists. Every npm-installed language server on Windows is a ``.cmd``
    shim, so all of them fail; only the native ``.exe`` servers (gopls,
    clangd, rust-analyzer, …) ever worked.

    Measured on pandorum 2026-09-13: 6 of 12 configured servers threw
    ``EINVAL``, including ``pyright-langserver.cmd``, which predated any
    of this. Python, TypeScript and bash had never worked through the
    MCP on that host.

    Note this is the *opposite* constraint from our own engine, which
    spawns via Python: ``CreateProcess`` there happily runs a ``.cmd``
    but ignores ``PATHEXT``, so it needs the resolved shim path. Two
    consumers, two spawn models — which is why only the MCP's config
    gets rewritten.
    """
    path = Path(resolved)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return [resolved, *args], False
    target = _node_shim_target(path)
    if target is None:
        # Nothing better available; leave it and let the caller warn.
        return [resolved, *args], False
    return ["node", str(target), *args], True


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


EXCLUDE_KEY = "_cclsp_sync"


def excluded_names(cfg: dict) -> set[str]:
    """Servers this config has opted out of.

    Persisted *inside* the config file, because the alternative is a
    flag the operator must remember on every future run — and the whole
    point of this script is that drift happens when a step is skipped.
    cclsp reads the file with ``JSON.parse`` and only ever touches
    ``config.servers``, so an extra top-level key is inert to it.
    """
    block = cfg.get(EXCLUDE_KEY)
    if not isinstance(block, dict):
        return set()
    return {str(n) for n in (block.get("exclude") or [])}


def add_exclusions(cfg: dict, names: Iterable[str]) -> set[str]:
    """Record ``names`` as excluded and return the full exclusion set."""
    current = excluded_names(cfg) | {str(n) for n in names}
    if current:
        cfg.setdefault(EXCLUDE_KEY, {})["exclude"] = sorted(current)
    return current


def reconcile(cfg: dict, *, resolve_commands: bool = False,
              exclude: Optional[set[str]] = None) -> tuple[dict, list[str]]:
    """Return ``(new_cfg, notes)``.

    ``resolve_commands`` rewrites the command of *newly added* entries
    to the absolute path ``shutil.which`` reports, and rewrites Windows
    ``.cmd`` shims to ``node <script>`` — see :func:`spawnable_command`.

    It also **repairs** an existing entry that names a ``.cmd`` shim.
    That is a deliberate exception to "an existing command is never
    overwritten": a shim cclsp raises ``EINVAL`` on is not hand-tuning,
    it is a server that cannot start. Leaving it would have preserved
    pandorum's dead ``pyright-langserver.cmd`` entry — the one that
    proved this class of failure predates the sync script entirely.
    Anything that is not a ``.cmd``/``.bat`` shim is still left alone.
    """
    notes: list[str] = []
    exclude = exclude or excluded_names(cfg)

    for spec in SPECS:
        resolved = shutil.which(spec.bin)
        entry = _find_entry(cfg, spec)

        if spec.name in exclude or spec.bin in exclude:
            if entry is not None:
                cfg["servers"] = [s for s in cfg["servers"] if s is not entry]
                notes.append(f"  EXCLUDE {spec.bin}: removed (opted out)")
            continue
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

        if entry is not None and resolve_commands:
            existing = entry.get("command") or []
            if existing and Path(existing[0]).suffix.lower() in (".cmd", ".bat"):
                # The entry may name a bare shim ("pyright-langserver.cmd")
                # rather than a path. Resolve it, or there is no directory
                # to find the shim's target in — which is how pyright
                # warned while its five identical siblings repaired.
                shim = existing[0]
                if not Path(shim).is_absolute():
                    shim = shutil.which(shim) or resolved or shim
                repaired, rewritten = spawnable_command(
                    shim, list(existing[1:]))
                if rewritten:
                    entry["command"] = repaired
                    notes.append(
                        f"  REPAIR {spec.bin}: .cmd shim cannot be spawned by "
                        f"cclsp (EINVAL) -> node {Path(repaired[1]).name}")
                else:
                    notes.append(
                        f"  WARN   {spec.bin}: .cmd shim will fail with EINVAL "
                        f"and its target could not be resolved")

        # A server too old for the standard in use abandons the
        # translation unit and emits nothing — the same observation a
        # clean file produces. Check the binary actually configured,
        # which may be a hand-pointed one (clangd-16, not clangd).
        probe_bin = resolved
        if entry is not None and (entry.get("command") or []):
            probe_bin = entry["command"][0]
        warning = version_warning(spec.name, server_version(probe_bin))
        if warning:
            notes.append(f"  WARN   {warning}")

        if entry is None:
            command = list(spec.cclsp_command)
            if resolve_commands:
                # `which` honours PATHEXT; CreateProcess does not. A bare
                # name here is invisible to cclsp on Windows. The shim it
                # finds is then unspawnable *by node*, so resolve through
                # to the script the shim wraps.
                command, _ = spawnable_command(resolved, command[1:])
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

    notes.extend(dedupe_servers(cfg))
    return cfg, notes


def stale_cclsp_processes(config_path: Path) -> list[tuple[int, float]]:
    """Running ``cclsp`` processes started *before* the config was written.

    cclsp reads its config once, at startup, and caches it. Nothing in
    the MCP surface says so, which makes an edited config look applied
    when it is not: ``mcp__lsp__restart_server`` restarts the *language
    server* using the command cclsp cached when its own process began,
    then reports "Successfully restarted 1 LSP server(s)". Killing the
    language server has the same non-effect. Killing cclsp itself does
    drop the stale config, but no client respawns a stdio MCP server, so
    every ``lsp`` tool goes offline until the client restarts.

    Net: a config change always costs a client restart. The least we can
    do is *say so* instead of letting the next caller trust a stale
    answer. Returns ``(pid, start_epoch)`` pairs, oldest first.
    """
    try:
        cfg_mtime = config_path.stat().st_mtime
    except OSError:
        return []

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,lstart=,cmd="],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return []   # Windows, or no ps — advisory only, never fatal.

    stale: list[tuple[int, float]] = []
    for line in out.splitlines():
        if "cclsp" not in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        # lstart is a fixed 24-char ctime string: "Sat Jul 25 14:01:04 2026"
        stamp = parts[1][:24]
        try:
            started = time.mktime(time.strptime(stamp))
        except ValueError:
            continue
        if started < cfg_mtime:
            stale.append((int(parts[0]), started))
    return sorted(stale, key=lambda p: p[1])


def _report_stale(config_path: Path) -> None:
    stale = stale_cclsp_processes(config_path)
    if not stale:
        return
    print(f"\n  !! {len(stale)} running cclsp process(es) predate this config "
          f"and are serving a STALE copy:")
    for pid, started in stale:
        age = (time.time() - started) / 86400.0
        print(f"       pid {pid:<8} started {time.strftime('%Y-%m-%d %H:%M', time.localtime(started))}"
              f"  ({age:.1f} days ago)")
    print("     cclsp caches its config at startup. restart_server will NOT\n"
          "     pick this up — it relaunches the language server with the\n"
          "     command cclsp cached when it started, and still reports\n"
          "     success. Restart the MCP client (Claude Code / VS Code /\n"
          "     cline) for these changes to take effect.")


def _sync_one(path: Path, *, label: str, write: bool,
              resolve_commands: bool,
              exclude: Iterable[str] = ()) -> tuple[bool, bool]:
    """Sync a single config file. Returns ``(changed, wrote)``."""
    cfg = _load(path)
    before = json.dumps(cfg, sort_keys=True)
    active = add_exclusions(cfg, exclude) if exclude else excluded_names(cfg)
    cfg, notes = reconcile(cfg, resolve_commands=resolve_commands,
                           exclude=active)
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
    ap.add_argument("--mcp-exclude", action="append", default=[],
                    metavar="NAME",
                    help="do not configure NAME in the MCP config, and "
                         "remove it if present. Persisted in the file, so "
                         "later runs keep honouring it. Repeatable. Applies "
                         "to the MCP config only — the engine is a separate "
                         "consumer with its own constraints.")
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
            exclude=a.mcp_exclude,
        )
        changed = changed or m_changed
        wrote = wrote or m_wrote
        # Always check, not only after a write: the config may have been
        # edited by hand (or by another session) since cclsp started.
        _report_stale(mcp_path)

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
