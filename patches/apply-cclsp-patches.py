#!/usr/bin/env python3
"""Patch the installed ``cclsp`` MCP server (the ``lsp`` tools).

We patch rather than reimplement: cclsp is maintained upstream and we
want its fixes. Every transform below is a targeted string replacement
with a distinct marker, so the script is idempotent, reversible, and
**fails loudly** if an anchor is missing rather than half-applying to a
bundle it no longer understands.

``npm i -g cclsp`` replaces the bundle and silently reverts all of this.
Re-run afterwards; ``--check`` reports what is applied.

The patches
-----------

``preload`` — *disable the startup preload.*
    cclsp preloads every server whose extensions appear in the
    workspace, racing each against a hardcoded 3 s readiness timeout
    (a function-local ``const``; ``CCLSP_CONFIG_PATH`` is the only env
    var cclsp reads). The promise it races is one most servers never
    settle, so every preloaded server loses it and startup stalls ~5 s
    while several processes spawn. cline would not load the MCP at all.
    Servers start lazily on first use anyway.

``stdin_exit`` — *exit when the client goes away.*
    A stdio MCP server should die on stdin EOF. cclsp does not, so every
    closed session leaves an orphan holding its language servers. Six
    were found on solidpc on 2026-09-16, the oldest **80 days**.

``idle_reap`` — *stop servers unqueried for 24 h.*
    Bounded by ``CCLSP_IDLE_HOURS`` (default 24). Only safe *because*
    of ``preload``: a reaped server now comes back on next use instead
    of being gone until the client restarts.

``progress`` + ``timeout_msg`` — *say what the server is doing.*
    cclsp ignores ``$/progress`` entirely (zero references in the
    bundle), so a request that times out while clangd builds its index
    reports only ``LSP request timeout: textDocument/hover (30000ms)``.
    That is indistinguishable from a dead server, and it gives the
    caller nothing to decide with. We capture progress notifications and
    fold the latest into the timeout error: what it is doing, how far
    along, a rough ETA derived from elapsed-vs-percentage, and an
    explicit retry hint.

Usage::

    python3 patches/apply-cclsp-patches.py           # apply all
    python3 patches/apply-cclsp-patches.py --check
    python3 patches/apply-cclsp-patches.py --revert
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = "claude-hooks"

# --------------------------------------------------------------------- #
# Transforms: (name, anchor, replacement, marker-substring)
# --------------------------------------------------------------------- #

PATCHES: list[tuple[str, str, str, str]] = [
    (
        "preload",
        "await lspClient.preloadServers();",
        "await Promise.resolve();  /* preload disabled by claude-hooks */",
        "preload disabled by claude-hooks",
    ),
    (
        "stdin_exit",
        'process.stderr.write(`CCLSP Server running on stdio\n`);',
        'process.stderr.write(`CCLSP Server running on stdio\n`);\n'
        '  /* claude-hooks: exit on stdin EOF so a closed client does not '
        'orphan this process and its language servers */\n'
        '  {\n'
        '    const __chBye = () => {\n'
        '      try { lspClient.dispose(); } catch {}\n'
        '      process.exit(0);\n'
        '    };\n'
        '    process.stdin.on("close", __chBye);\n'
        '    process.stdin.on("end", __chBye);\n'
        '    const __chIdleMs = Number(process.env.CCLSP_IDLE_HOURS || 24) '
        '* 3600000;\n'
        '    const __chSweep = setInterval(() => {\n'
        '      const now = Date.now();\n'
        '      for (const [k, s] of lspClient.servers) {\n'
        '        const last = s.__chLastUsed || s.__chStartedAt || 0;\n'
        '        if (last && now - last > __chIdleMs) {\n'
        '          try { s.process.kill(); } catch {}\n'
        '          lspClient.servers.delete(k);\n'
        '          process.stderr.write(`[claude-hooks] reaped LSP server '
        'idle for ${Math.round((now - last) / 3600000)}h\n`);\n'
        '        }\n'
        '      }\n'
        '    }, 3600000);\n'
        '    if (__chSweep.unref) __chSweep.unref();\n'
        '  }',
        "exit on stdin EOF",
    ),
    (
        "started_at",
        "this.setupRestartTimer(serverState);",
        "serverState.__chStartedAt = Date.now();  /* claude-hooks */\n"
        "    this.setupRestartTimer(serverState);",
        "__chStartedAt = Date.now()",
    ),
    (
        "last_used",
        'const server = this.servers.get(key);\n'
        '      if (!server) {\n'
        '        throw new Error("Server exists in map but is undefined");\n'
        '      }\n'
        '      return server;',
        'const server = this.servers.get(key);\n'
        '      if (!server) {\n'
        '        throw new Error("Server exists in map but is undefined");\n'
        '      }\n'
        '      server.__chLastUsed = Date.now();  /* claude-hooks idle reap */\n'
        '      return server;',
        "__chLastUsed = Date.now()",
    ),
    (
        "progress",
        "const { adapter } = serverState;",
        'const { adapter } = serverState;\n'
        '      /* claude-hooks: cclsp ignores $/progress, so a timeout while '
        'the server indexes cannot say so. Keep the latest. */\n'
        '      if (message.method === "$/progress") {\n'
        '        try {\n'
        '          const v = (message.params && message.params.value) || {};\n'
        '          if (v.kind === "end") {\n'
        '            serverState.__chProgress = undefined;\n'
        '          } else if (v.kind === "begin" || v.kind === "report") {\n'
        '            const prev = serverState.__chProgress || {};\n'
        '            serverState.__chProgress = {\n'
        '              title: v.title || prev.title,\n'
        '              message: v.message || prev.message,\n'
        '              percentage: typeof v.percentage === "number" '
        '? v.percentage : prev.percentage,\n'
        '              since: prev.since || Date.now(),\n'
        '            };\n'
        '          }\n'
        '        } catch {}\n'
        '      }',
        "cclsp ignores $/progress",
    ),
    (
        "timeout_msg",
        "reject(new Error(`LSP request timeout: ${method} (${timeout}ms)`));",
        '/* claude-hooks: a bare timeout is indistinguishable from a dead '
        'server. Report what it is doing and when to retry. */\n'
        '        let __chExtra = "";\n'
        '        try {\n'
        '          let __chSt;\n'
        '          for (const s of this.servers.values()) {\n'
        '            if (s.process === process3) { __chSt = s; break; }\n'
        '          }\n'
        '          const p = __chSt && __chSt.__chProgress;\n'
        '          const age = __chSt && __chSt.__chStartedAt\n'
        '            ? Math.round((Date.now() - __chSt.__chStartedAt) / 1000)\n'
        '            : null;\n'
        '          if (p) {\n'
        '            const pct = typeof p.percentage === "number" '
        '? p.percentage : null;\n'
        '            const elapsed = Math.max(1, '
        'Math.round((Date.now() - (p.since || Date.now())) / 1000));\n'
        '            const eta = (pct && pct > 0 && pct < 100)\n'
        '              ? Math.max(1, Math.round(elapsed * (100 - pct) / pct))\n'
        '              : null;\n'
        '            __chExtra = ` — the server is still working: '
        '${p.title || "busy"}${p.message ? " (" + p.message + ")" : ""}'
        '${pct !== null ? ", " + pct + "%" : ""}. This is NOT a failure and '
        'NOT an empty result. ${eta ? "Estimated ~" + eta + "s remaining; r" '
        ': "R"}etry the same request in ${eta ? Math.min(eta, 60) : 30}s.`;\n'
        '          } else if (age !== null && age < 180) {\n'
        '            __chExtra = ` — the server started ${age}s ago and is '
        'still warming up (index / preamble build). This is NOT a failure. '
        'Retry the same request in 15-30s.`;\n'
        '          }\n'
        '        } catch {}\n'
        '        reject(new Error(`LSP request timeout: ${method} '
        '(${timeout}ms)${__chExtra}`));',
        "the server is still working",
    ),
]


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

    print(f"cclsp bundle: {bundle}")
    state = {name: (mark in src) for name, _, _, mark in PATCHES}
    for name, applied in state.items():
        print(f"  {'APPLIED' if applied else 'stock  '}  {name}")

    if a.check:
        return 0 if all(state.values()) else 1

    if a.revert:
        if not any(state.values()):
            print("  nothing to revert")
            return 0
        if orig.is_file():
            shutil.copy2(orig, bundle)
            print(f"  restored from {orig.name}")
            return 0
        raise SystemExit(
            "  no .orig backup found — reinstall with `npm i -g cclsp`.")

    # An older layout applied only the preload patch under a different
    # marker; accept it so upgrading this script is not a reinstall.
    src = src.replace(
        "await Promise.resolve();  "
        "/* preload disabled by claude-hooks: see "
        "patches/apply-cclsp-preload-patch.py */",
        "await Promise.resolve();  /* preload disabled by claude-hooks */")

    if not orig.is_file():
        shutil.copy2(bundle, orig)
        print(f"  backed up to {orig.name}")

    applied_now = []
    for name, anchor, replacement, mark in PATCHES:
        if mark in src:
            continue
        if anchor not in src:
            raise SystemExit(
                f"\n  ANCHOR MISSING for {name!r}. cclsp's bundle has "
                f"changed; re-read the source before forcing this patch. "
                f"Nothing was written.")
        src = src.replace(anchor, replacement, 1)
        applied_now.append(name)

    if not applied_now:
        print("  all patches already applied — nothing to do")
        return 0

    bundle.write_text(src, encoding="utf-8", errors="surrogateescape")
    print(f"  applied: {', '.join(applied_now)}")
    print("  Restart the MCP client (cline / VS Code / Claude Code).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
