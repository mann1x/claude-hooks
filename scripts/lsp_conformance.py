#!/usr/bin/env python3
"""Acceptance suite for the LSP engine against a real C/C++/CUDA tree.

Every check here is an *observed* cclsp failure or a gap found while
replacing it — not a wishlist. The fixture is a mixed compile-database
project because that exercises byte framing, large preambles, background
indexing and multi-extension routing at once, and because clangd is the
server that produced the bug that started this: it renders every
function hover as ``→ <type>``, and ``→`` is U+2192 — three bytes, one
UTF-16 unit. cclsp sliced its buffer by character count against a
byte-valued ``Content-Length``, so the first such hover desynced the
stream permanently.

Usage::

    python3 scripts/lsp_conformance.py                    # autodetect
    python3 scripts/lsp_conformance.py --root /path/to/tree
    python3 scripts/lsp_conformance.py --json

Exit status is the number of failed checks, so it can gate a deploy.
A check that cannot run (no clangd, no compile DB) is SKIP, never PASS:
"could not test" and "tested and fine" are the distinction this whole
subsystem exists to preserve.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import LspServerSpec  # noqa: E402
from claude_hooks.lsp_engine.engine import Engine  # noqa: E402
from claude_hooks.lsp_engine.lsp import (  # noqa: E402
    LspProtocolError, client_capabilities,
)
from claude_hooks.lang_servers import (  # noqa: E402
    server_version, version_major, version_warning,
)

#: Trees to try when --root is not given, most specific first.
_CANDIDATE_ROOTS = (
    "/shared/dev/llama.cpp",
    "/srv/dev-disk-by-label-opt/dev/llama.cpp",
    "/opt/llama.cpp",
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Result:
    def __init__(self, name: str, status: str, detail: str = ""):
        self.name, self.status, self.detail = name, status, detail

    def __str__(self) -> str:
        colour = {"PASS": "\033[32m", "FAIL": "\033[31m",
                  "SKIP": "\033[33m"}.get(self.status, "")
        reset = "\033[0m" if colour else ""
        line = f"  {colour}{self.status:4}{reset}  {self.name}"
        return f"{line}\n          {self.detail}" if self.detail else line


def find_fixture(explicit: Optional[str]) -> Optional[tuple[Path, Path]]:
    """Return (root, compile_db_dir) for the first usable tree."""
    roots = [explicit] if explicit else list(_CANDIDATE_ROOTS)
    for raw in roots:
        if not raw:
            continue
        root = Path(raw)
        if not root.is_dir():
            continue
        for rel in ("build", ".", "out"):
            if (root / rel / "compile_commands.json").is_file():
                return root.resolve(), (root / rel).resolve()
    return None


def clangd_spec(db_dir: Path) -> LspServerSpec:
    return LspServerSpec(
        extensions=("c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx",
                    "cu", "cuh"),
        command=("clangd", f"--compile-commands-dir={db_dir}",
                 "--background-index=false"),
    )


def pick_files(db_path: Path) -> dict[str, Path]:
    """One translation unit per extension present in the compile DB."""
    out: dict[str, Path] = {}
    try:
        db = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in db:
        f = Path(entry.get("file", ""))
        ext = f.suffix.lstrip(".").lower()
        if ext and ext not in out and f.is_file():
            out[ext] = f
    return out


def find_arrow_hover(engine: Engine, path: Path,
                     limit: int = 200) -> tuple[Optional[str], Optional[int]]:
    """Locate a hover whose text contains U+2192 — the cclsp killer.

    Scanning rather than hardcoding a line, because the fixture is a
    real tree that moves. If no arrow is found the check SKIPs: a
    fixture that cannot produce the condition cannot prove immunity to
    it.
    """
    for line in range(1, limit):
        try:
            res = engine.hover(path, line, 12)
        except Exception:
            continue
        for text in res.items:
            if "→" in text:
                return text, line
    return None, None


# ───────────────────────────── checks ───────────────────────────────


def check_version(_ctx) -> Result:
    binary = shutil.which("clangd")
    if not binary:
        return Result("clangd version floor", SKIP, "clangd not on PATH")
    line = server_version("clangd")
    major = version_major(line or "")
    warn = version_warning("clangd", line or "")
    if major is None:
        return Result("clangd version floor", FAIL,
                      f"could not parse version from {line!r}")
    detail = f"clangd {major}"
    if warn:
        return Result("clangd version floor", FAIL, warn)
    return Result("clangd version floor", PASS,
                  f"{detail} (>= 17 for C++23, >= 22 for CUDA 13.x)")


def check_lazy_spawn(ctx) -> Result:
    """cclsp preloaded every server against a hardcoded 3 s race, which
    is why cline could not load the MCP at all."""
    engine = Engine(ctx["root"], [clangd_spec(ctx["db_dir"])])
    try:
        before = len(engine.active_servers())
        if before != 0:
            return Result("lazy spawn", FAIL,
                          f"{before} server(s) started before any request")
        engine.did_open(ctx["files"]["cpp"],
                        ctx["files"]["cpp"].read_text(errors="replace"))
        after = len(engine.active_servers())
        return (Result("lazy spawn", PASS,
                       "0 servers at construction, 1 after first request")
                if after == 1 else
                Result("lazy spawn", FAIL, f"{after} servers after one open"))
    finally:
        engine.shutdown()


def check_byte_framing(ctx) -> Result:
    """The bug that started this.

    Not "does a hover work" — cclsp's hover worked too, once. The check
    is that a request *after* a multi-byte hover still works.
    """
    engine = ctx["engine"]
    path = ctx["files"].get("cu") or ctx["files"].get("cpp")
    text, line = find_arrow_hover(engine, path)
    if text is None:
        return Result("byte framing survives U+2192", SKIP,
                      "no arrow-bearing hover found in this fixture")
    after = engine.hover(path, line, 12)
    if not after.consulted:
        return Result("byte framing survives U+2192", FAIL,
                      f"server stopped answering after the hover at "
                      f"line {line} — this is the cclsp wedge")
    defn = engine.definition(path, line, 12)
    if defn.failures:
        return Result("byte framing survives U+2192", FAIL,
                      f"follow-up request failed: {defn.failures}")
    return Result("byte framing survives U+2192", PASS,
                  f"hover at {path.name}:{line} contained U+2192; "
                  f"two later requests still answered")


def check_multi_extension_routing(ctx) -> Result:
    engine = ctx["engine"]
    answered, silent = [], []
    for ext, path in sorted(ctx["files"].items()):
        res = engine.hover(path, 20, 10)
        (answered if res.consulted else silent).append(ext)
    if silent:
        return Result("multi-extension routing", FAIL,
                      f"no server answered for: {', '.join(silent)}")
    return Result("multi-extension routing", PASS,
                  f"routed and answered: {', '.join(answered)}")


def check_compile_db(ctx) -> Result:
    from claude_hooks.lsp_integration import missing_compile_db
    path = ctx["files"].get("cu") or ctx["files"].get("cpp")
    if missing_compile_db(path):
        return Result("compile DB detected", FAIL,
                      f"no compile_commands.json found above {path}")
    return Result("compile DB detected", PASS, str(ctx["db_dir"]))


def check_diagnostics_are_honest(ctx) -> Result:
    """A TU that failed to parse must not read as a clean file."""
    from claude_hooks.lsp_integration import is_translation_unit_failure
    engine = ctx["engine"]
    path = ctx["files"].get("cpp")
    res = engine.get_diagnostics_result(path, timeout=30.0)
    if not res.settled:
        return Result("diagnostics are honest", FAIL,
                      f"{res.server or 'server'} never published within "
                      f"{res.timeout:.0f}s and the engine did not say so")
    broken = [d for d in res.items if is_translation_unit_failure(d)]
    if broken:
        return Result("diagnostics are honest", FAIL,
                      f"TU failure not surfaced: {broken[0].message[:120]}")
    return Result("diagnostics are honest", PASS,
                  f"{len(res.items)} diagnostic(s), none a TU failure, "
                  f"settled in {res.waited:.1f}s")


def check_slow_publish_is_not_clean(ctx) -> Result:
    """The bug the opencoti session found on 2026-09-16.

    On their cosmocc tree clangd needed longer than the wait, and an
    empty list came back rendered as *"No diagnostics"* — a clean bill
    of health the server never gave. Reproduced here by asking with a
    deliberately impossible budget, which is the same observation as a
    cold 24 MB preamble and far cheaper to arrange. The engine must
    report *unsettled*, and the MCP layer must not spell that "No
    diagnostics".
    """
    from claude_hooks.lsp_mcp import server as mcp_server
    # The budget is pinned on the *spec*, so the tool's own call — which
    # passes no timeout — inherits it. Staging it any other way would
    # test a path the MCP surface does not take, and the MCP surface is
    # where the bug was reported.
    spec = clangd_spec(ctx["db_dir"])
    spec = dataclasses.replace(spec, diagnostics_timeout=0.001)
    # Its own engine: the shared one has already been asked about these
    # files by earlier checks, and a cached publish answers instantly,
    # which would turn this into a SKIP that proves nothing.
    engine = Engine(ctx["root"], [spec],
                    startup_timeout=60.0, request_timeout=30.0)
    path = ctx["files"].get("cpp")
    try:
        engine.did_open(path, path.read_text(errors="replace"))
        res = engine.get_diagnostics_result(path)
        if res.settled:
            return Result("slow publish is not reported clean", SKIP,
                          "server published within 1ms — cannot stage the "
                          "condition on this fixture")
        if res.items:
            return Result("slow publish is not reported clean", FAIL,
                          "unsettled result carried items")

        srv = mcp_server.LspMcpServer.__new__(mcp_server.LspMcpServer)
        entry = types.SimpleNamespace(engine=engine, root=ctx["root"])
        srv.registry = types.SimpleNamespace(for_path=lambda _p: entry)
        with _patched_file_path(mcp_server.LspMcpServer, path):
            rendered = srv._tool_get_diagnostics({"file_path": str(path)})
        if "No diagnostics for" in rendered:
            return Result("slow publish is not reported clean", FAIL,
                          "a timeout was rendered as 'No diagnostics'")
        if "NO ANSWER YET" not in rendered:
            return Result("slow publish is not reported clean", FAIL,
                          f"unexpected rendering: {rendered[:120]}")
        return Result("slow publish is not reported clean", PASS,
                      "an unpublished TU renders as NO ANSWER YET, not clean")
    finally:
        engine.shutdown()


@contextlib.contextmanager
def _patched_file_path(cls, path):
    original = cls._file_path
    cls._file_path = lambda _self, _args: path
    try:
        yield
    finally:
        cls._file_path = original


def check_cancel_does_not_poison(ctx) -> Result:
    """A request we gave up on must not leave the next one queued behind
    work nobody is waiting for."""
    engine = Engine(ctx["root"], [clangd_spec(ctx["db_dir"])],
                    startup_timeout=60.0, request_timeout=0.001)
    path = ctx["files"]["cpp"]
    try:
        try:
            engine.did_open(path, path.read_text(errors="replace"))
        except Exception:
            pass
        engine.references(path, 20, 10, seed=False)     # will time out
        client = next(iter(engine._clients.values()), None)
        if client is None:
            return Result("cancel does not poison the queue", SKIP,
                          "server did not start within the budget")
        if client._pending:
            return Result("cancel does not poison the queue", FAIL,
                          f"{len(client._pending)} request(s) left pending")
        return Result("cancel does not poison the queue", PASS,
                      "pending map empty after a timeout")
    finally:
        engine.shutdown()


def check_respawn_on_desync(ctx) -> Result:
    """A desynced stream is terminal — the engine must replace the
    process rather than keep asking a server that cannot answer."""
    engine = Engine(ctx["root"], [clangd_spec(ctx["db_dir"])],
                    startup_timeout=60.0, request_timeout=30.0)
    path = ctx["files"]["cpp"]
    try:
        engine.did_open(path, path.read_text(errors="replace"))
        spec, first = next(iter(engine._clients.items()))
        first._desynced = True                      # what a bad frame sets
        engine.hover(path, 20, 10)
        second = engine._clients.get(spec)
        if second is None:
            return Result("respawn on desync", FAIL,
                          "desynced client dropped but not replaced")
        if second is first:
            return Result("respawn on desync", FAIL,
                          "desynced client was reused")
        if engine._uri_routing:
            routed = next(iter(engine._uri_routing))
            if spec in engine._uri_routing[routed][1] and not second.is_alive:
                return Result("respawn on desync", FAIL,
                              "routing kept for a dead replacement")
        return Result("respawn on desync", PASS,
                      "desynced client stopped and replaced; routing reset")
    finally:
        engine.shutdown()


def check_pending_fail_fast(ctx) -> Result:
    """When the reader dies, waiters must be woken, not left to burn
    their full timeout each."""
    from claude_hooks.lsp_engine.lsp import LspClient
    from queue import Queue
    c = LspClient(["true"], ctx["root"])
    q: Queue = Queue(maxsize=1)
    c._pending[99] = q
    c._fail_pending("reader died")
    if q.empty():
        return Result("pending requests fail fast", FAIL,
                      "waiter was not woken")
    msg = q.get_nowait()
    if "error" not in msg:
        return Result("pending requests fail fast", FAIL,
                      f"woken with a non-error: {msg}")
    return Result("pending requests fail fast", PASS,
                  f"woken with {msg['error']['message']!r}")


def check_progress_is_captured(ctx) -> Result:
    """'still indexing' and 'dead' must not be the same observation."""
    caps = client_capabilities()
    if not caps.get("window", {}).get("workDoneProgress"):
        return Result("progress reporting", FAIL,
                      "window.workDoneProgress not declared — servers "
                      "will not send $/progress")
    engine = Engine(ctx["root"], [LspServerSpec(
        extensions=("c", "cpp", "cu", "h", "hpp", "cuh"),
        command=("clangd", f"--compile-commands-dir={ctx['db_dir']}",
                 "--background-index=true"))],
        startup_timeout=60.0, request_timeout=30.0)
    path = ctx["files"]["cpp"]
    try:
        engine.did_open(path, path.read_text(errors="replace"))
        client = next(iter(engine._clients.values()))
        deadline = time.time() + 25
        while time.time() < deadline:
            snap = client.progress_snapshot()
            if snap:
                return Result("progress reporting", PASS,
                              f"captured {snap['title']!r}"
                              + (f" at {snap['percentage']}%"
                                 if snap.get("percentage") is not None else ""))
            time.sleep(0.3)
        return Result("progress reporting", SKIP,
                      "declared, but the server reported none in 25 s "
                      "(index may be warm)")
    finally:
        engine.shutdown()


def check_protocol_error_is_raised(ctx) -> Result:
    """A malformed frame must raise, not be skipped — skipping keeps
    reading from a desynced offset, which is the permanent wedge."""
    import io
    from claude_hooks.lsp_engine.lsp import LspClient
    read_frame = getattr(LspClient._read_frame, "__func__",
                         LspClient._read_frame)
    body = b"{not json"
    data = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    try:
        read_frame(io.BytesIO(data))
    except LspProtocolError:
        return Result("malformed frame raises", PASS,
                      "LspProtocolError, so the caller can respawn")
    return Result("malformed frame raises", FAIL,
                  "a bad frame was swallowed")


def check_navigation_surface(ctx) -> Result:
    """The tools a caller actually uses, against real C++."""
    engine = ctx["engine"]
    path = ctx["files"].get("cpp")
    syms = engine.find_symbols(path, "main")
    if not syms.items:
        # Not every TU has main; take any symbol the server reports.
        res = engine._fan_out(
            engine._ensure_open(path),
            lambda c: c.document_symbols(path, timeout=30.0),
            what="documentSymbol")
        if not res.items:
            return Result("navigation surface", FAIL,
                          "documentSymbol returned nothing for a real TU")
        target = res.items[0]
    else:
        target = syms.items[0]

    line, ch = target.selection.start.line, target.selection.start.character
    checks = {
        "hover": engine.hover(path, line, ch),
        "definition": engine.definition(path, line, ch),
        "references": engine.references(path, line, ch, seed=False),
        "incoming_calls": engine.calls(path, line, ch, direction="incoming"),
    }
    dead = [name for name, r in checks.items() if r.failures]
    if dead:
        return Result("navigation surface", FAIL,
                      f"failed for {target.name}: {', '.join(dead)}")
    got = [name for name, r in checks.items() if r.items]
    return Result("navigation surface", PASS,
                  f"on {target.name!r}: answered by {', '.join(got) or 'none'}"
                  f" (no failures across {len(checks)} requests)")


CHECKS: list[tuple[str, Callable]] = [
    ("version", check_version),
    ("lazy_spawn", check_lazy_spawn),
    ("byte_framing", check_byte_framing),
    ("malformed_frame", check_protocol_error_is_raised),
    ("respawn_on_desync", check_respawn_on_desync),
    ("pending_fail_fast", check_pending_fail_fast),
    ("cancel", check_cancel_does_not_poison),
    ("routing", check_multi_extension_routing),
    ("compile_db", check_compile_db),
    ("diagnostics", check_diagnostics_are_honest),
    ("slow-publish", check_slow_publish_is_not_clean),
    ("progress", check_progress_is_captured),
    ("navigation", check_navigation_surface),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.environ.get("LSP_CONFORMANCE_ROOT"),
                    help="C/C++/CUDA tree with a compile_commands.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", action="append",
                    help="run only these checks (repeatable)")
    args = ap.parse_args()

    fixture = find_fixture(args.root)
    if fixture is None:
        sys.stderr.write(
            "No fixture found. Pass --root pointing at a C/C++ tree with a "
            "compile_commands.json (tried: "
            + ", ".join(_CANDIDATE_ROOTS) + ").\n")
        return 1
    root, db_dir = fixture
    files = pick_files(db_dir / "compile_commands.json")
    if not files:
        sys.stderr.write(f"compile DB at {db_dir} lists no existing files.\n")
        return 1

    if not args.json:
        print(f"LSP conformance suite")
        print(f"  fixture : {root}")
        print(f"  compileDB: {db_dir}/compile_commands.json")
        print(f"  TUs      : " + ", ".join(
            f"{e}={p.name}" for e, p in sorted(files.items())))
        print()

    engine = Engine(root, [clangd_spec(db_dir)],
                    startup_timeout=90.0, request_timeout=45.0)
    ctx = {"root": root, "db_dir": db_dir, "files": files, "engine": engine}

    results: list[Result] = []
    try:
        for name, fn in CHECKS:
            if args.only and name not in args.only:
                continue
            t0 = time.time()
            try:
                res = fn(ctx)
            except Exception as e:      # a check must never abort the run
                res = Result(name, FAIL, f"{type(e).__name__}: {e}")
            res.seconds = round(time.time() - t0, 1)   # type: ignore[attr-defined]
            results.append(res)
            if not args.json:
                print(res, f"  [{res.seconds}s]" if res.seconds >= 1 else "")
    finally:
        engine.shutdown()

    failed = [r for r in results if r.status == FAIL]
    if args.json:
        print(json.dumps({
            "fixture": str(root),
            "results": [{"name": r.name, "status": r.status,
                         "detail": r.detail,
                         "seconds": getattr(r, "seconds", 0)}
                        for r in results],
            "failed": len(failed),
        }, indent=2))
    else:
        skipped = [r for r in results if r.status == SKIP]
        print()
        print(f"  {len(results) - len(failed) - len(skipped)} passed, "
              f"{len(failed)} failed, {len(skipped)} skipped")
        if skipped:
            print("  (a SKIP is 'could not test', never 'tested and fine')")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
