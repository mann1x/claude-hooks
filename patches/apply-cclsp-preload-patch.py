#!/usr/bin/env python3
"""Disable cclsp's startup preload of language servers.

Why
---
``cclsp`` preloads every configured server whose extensions appear in
the workspace, *before* it is useful, and races each one against a
hardcoded 3 s readiness timeout:

    const INITIALIZATION_TIMEOUT = 3000;

That timeout is a function-local ``const`` — not an env var, not a
config key, not reachable from ``cclsp.cmd``. ``CCLSP_CONFIG_PATH`` is
the only knob cclsp exposes (verified by enumerating every
``process.env`` read in the 0.7.0 bundle).

The readiness promise it races is one most language servers never
settle, so *every* preloaded server loses, prints
``Initialization timeout ... proceeding anyway``, and startup stalls for
~5 s with several node processes spawning at once.

This stayed invisible on pandorum only because the config was broken:
the single npm server (pyright) died instantly with ``EINVAL``, and the
four native ``.exe`` servers never matched a web workspace. cclsp
started **zero** servers and went idle in ~1 s. Repairing the config so
all servers actually start is what made the preload cost appear — and
cline stopped loading the MCP.

Preload is an optimisation, not a requirement: ``getServer`` already
starts a server lazily on first use ("Starting new server instance").
Disabling it trades a warm first request for a startup that does
nothing, which is the behaviour the working configuration had anyway.

Usage
-----
    python3 patches/apply-cclsp-preload-patch.py          # apply
    python3 patches/apply-cclsp-preload-patch.py --revert
    python3 patches/apply-cclsp-preload-patch.py --check

Idempotent. Keeps a ``.orig`` beside the bundle. Any ``npm i -g cclsp``
replaces the bundle and silently undoes this — re-run afterwards.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

NEEDLE = "await lspClient.preloadServers();"
PATCHED = ("await Promise.resolve();  "
           "/* preload disabled by claude-hooks: see "
           "patches/apply-cclsp-preload-patch.py */")


def find_bundle(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True,
                              text=True, timeout=60, check=True,
                              shell=(sys.platform == "win32"))
        candidate = Path(root.stdout.strip()) / "cclsp" / "dist" / "index.js"
        if candidate.is_file():
            return candidate
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  could not ask npm for its global root: {e}")
    raise SystemExit(
        "cclsp bundle not found. Pass the path to dist/index.js explicitly.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--path", default=None, help="path to cclsp dist/index.js")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    bundle = find_bundle(a.path)
    orig = bundle.with_suffix(".js.orig")
    src = bundle.read_text(encoding="utf-8", errors="surrogateescape")
    applied = PATCHED in src

    print(f"cclsp bundle: {bundle}")
    print(f"  state: {'PATCHED' if applied else 'stock'}")

    if a.check:
        return 0 if applied else 1

    if a.revert:
        if not applied:
            print("  nothing to revert")
            return 0
        if orig.is_file():
            shutil.copy2(orig, bundle)
            print(f"  restored from {orig.name}")
        else:
            bundle.write_text(src.replace(PATCHED, NEEDLE),
                              encoding="utf-8", errors="surrogateescape")
            print("  reverted in place (no .orig found)")
        return 0

    if applied:
        print("  already patched — nothing to do")
        return 0
    if NEEDLE not in src:
        raise SystemExit(
            "  the preload call was not found. cclsp's bundle has changed; "
            "re-check the source before forcing this patch.")

    if not orig.is_file():
        shutil.copy2(bundle, orig)
        print(f"  backed up to {orig.name}")
    bundle.write_text(src.replace(NEEDLE, PATCHED),
                      encoding="utf-8", errors="surrogateescape")
    print("  preload disabled. Servers now start on first use.")
    print("  Restart the MCP client (cline / VS Code / Claude Code).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
