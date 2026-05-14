"""``claude-hooks-models`` — registry CLI for llamafile chat models.

The registry lives at ``~/.claude/llamafile-models.json`` and maps a
user-chosen label to a GGUF path + port + ctx + mode. The daemon
reads this file on demand (mtime-watch) and spawns / supervises the
llamafiles. See ``docs/llamafile-chat-models.md`` for the full design.

Subcommands:
  list [--json]
  add <label> <gguf-path> [--ctx N] [--port N]
                          [--mode auto|cpu] [--idle-timeout SEC]
                          [--notes "..."]
  remove <label> [--force]
  rename <old> <new>
  copy <src> <new-label> [--port N] [--ctx N]
  show <label> [--json]
  path
  probe <label>
  gc

All subcommands operate on the registry file directly; ``show``,
``probe``, and ``gc`` additionally talk to the daemon via RPC when
it's running (best-effort — they degrade gracefully when it isn't).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from claude_hooks.chat_model_registry import (
    DEFAULT_REGISTRY_PATH,
    InvalidGguf,
    InvalidLabel,
    LabelCollision,
    NoFreePort,
    PortCollision,
    Registry,
    UnknownLabel,
)


# ----------------------------------------------------------------- #
# Output helpers
# ----------------------------------------------------------------- #

def _fmt_table(rows: list[dict], cols: list[str]) -> str:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows))
              for c in cols} if rows else {c: len(c) for c in cols}
    lines = ["  ".join(c.ljust(widths[c]) for c in cols),
             "  ".join("-" * widths[c] for c in cols)]
    for r in rows:
        lines.append("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    return "\n".join(lines)


def _spec_to_row(spec) -> dict:
    return {
        "label": spec.label,
        "port": spec.port,
        "ctx": spec.ctx_size,
        "mode": spec.mode,
        "idle": spec.idle_timeout_seconds,
        "gguf": spec.gguf_path,
    }


def _err(msg: str) -> int:
    print(f"claude-hooks-models: {msg}", file=sys.stderr)
    return 2


# ----------------------------------------------------------------- #
# Subcommands
# ----------------------------------------------------------------- #

def _spec_to_dict(spec) -> dict:
    """ModelSpec -> JSON-friendly dict (with label included)."""
    d = spec.to_json()
    d["label"] = spec.label
    return d


def cmd_list(reg: Registry, args) -> int:
    specs = reg.list_specs()
    if args.json:
        print(json.dumps(
            [_spec_to_dict(s) for s in specs], indent=2, sort_keys=True,
        ))
        return 0
    if not specs:
        print("(no models registered; use `claude-hooks-models add` to "
              "register one)")
        return 0
    rows = [_spec_to_row(s) for s in specs]
    print(_fmt_table(rows, ["label", "port", "ctx", "mode", "idle", "gguf"]))
    return 0


def cmd_add(reg: Registry, args) -> int:
    add_kwargs = {
        "gguf_path": args.gguf_path,
        "ctx_size": args.ctx,
        "port": args.port,
        "mode": args.mode,
        "notes": args.notes or "",
    }
    if args.idle_timeout is not None:
        add_kwargs["idle_timeout_seconds"] = args.idle_timeout
    try:
        spec = reg.add(args.label, **add_kwargs)
    except LabelCollision as e:
        return _err(f"label exists: {e}")
    except PortCollision as e:
        return _err(f"port conflict: {e}")
    except InvalidLabel as e:
        return _err(f"invalid label: {e}")
    except InvalidGguf as e:
        return _err(f"invalid gguf: {e}")
    except NoFreePort as e:
        return _err(f"no free port in default range: {e}")
    print(f"added {spec.label!r} -> {spec.gguf_path}  "
          f"(port={spec.port}, ctx={spec.ctx_size}, mode={spec.mode})")
    return 0


def cmd_remove(reg: Registry, args) -> int:
    # Best-effort reap via daemon RPC unless --force was passed.
    if not args.force:
        _best_effort_shutdown(args.label)
    if not reg.remove(args.label):
        return _err(f"unknown label: {args.label!r}")
    print(f"removed {args.label!r}")
    return 0


def cmd_rename(reg: Registry, args) -> int:
    try:
        reg.rename(args.old, args.new)
    except UnknownLabel as e:
        return _err(f"unknown label: {e}")
    except LabelCollision as e:
        return _err(f"label exists: {e}")
    except InvalidLabel as e:
        return _err(f"invalid label: {e}")
    print(f"renamed {args.old!r} -> {args.new!r}")
    return 0


def cmd_copy(reg: Registry, args) -> int:
    copy_kwargs = {"port": args.port}
    if args.ctx is not None:
        copy_kwargs["ctx_size"] = args.ctx
    try:
        spec = reg.copy(args.src, args.new_label, **copy_kwargs)
    except UnknownLabel as e:
        return _err(f"unknown source: {e}")
    except LabelCollision as e:
        return _err(f"label exists: {e}")
    except PortCollision as e:
        return _err(f"port conflict: {e}")
    except NoFreePort as e:
        return _err(f"no free port in default range: {e}")
    print(f"copied {args.src!r} -> {spec.label!r}  "
          f"(port={spec.port}, ctx={spec.ctx_size})")
    return 0


def cmd_show(reg: Registry, args) -> int:
    try:
        spec = reg.get(args.label)
    except UnknownLabel as e:
        return _err(f"unknown label: {e}")
    out = _spec_to_dict(spec)
    # Best-effort live status from daemon
    live = _best_effort_status(args.label)
    if live is not None:
        out["_live"] = live
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"label: {spec.label}")
        print(f"gguf:  {spec.gguf_path}")
        print(f"port:  {spec.port}")
        print(f"ctx:   {spec.ctx_size}")
        print(f"mode:  {spec.mode}")
        print(f"idle:  {spec.idle_timeout_seconds}s")
        if spec.notes:
            print(f"notes: {spec.notes}")
        if live is not None:
            print(f"live:  {live}")
        else:
            print("live:  (daemon not reachable or model not running)")
    return 0


def cmd_path(reg: Registry, args) -> int:
    print(str(reg.path))
    return 0


def cmd_probe(reg: Registry, args) -> int:
    """Ask the daemon to spawn the model, report the result, leave
    it running. The next idle reaper tick will reap if nothing
    else touches it."""
    try:
        reg.get(args.label)
    except UnknownLabel as e:
        return _err(f"unknown label: {e}")
    try:
        from claude_hooks import daemon_client as dc
    except ImportError as e:
        return _err(f"daemon_client import failed: {e}")
    resp = dc.chat_model_ensure(args.label, timeout=120.0)
    if resp is None:
        return _err("daemon not reachable; start it with "
                    "`claude-hooks-daemon-ctl start`")
    if not resp.get("ready") or "port" not in resp:
        return _err(f"daemon refused to bring up {args.label!r}: "
                    f"{resp.get('reason', 'unknown')}")
    print(f"OK: {args.label!r} listening on port {resp['port']} "
          f"(mode={resp.get('mode', '?')}, "
          f"spawned={resp.get('spawned', False)})")
    if resp.get("evicted"):
        print(f"     evicted to make room: {resp['evicted']}")
    return 0


def cmd_gc(reg: Registry, args) -> int:
    """Ask the daemon to reap any running processes whose labels are
    no longer in the registry."""
    try:
        from claude_hooks import daemon_client as dc
    except ImportError as e:
        return _err(f"daemon_client import failed: {e}")
    resp = dc.chat_model_gc(timeout=30.0)
    if resp is None:
        return _err("daemon not reachable")
    if not resp.get("ok", True):
        return _err(f"daemon refused gc: {resp.get('reason', 'unknown')}")
    reaped = resp.get("reaped") or []
    if reaped:
        print(f"reaped {len(reaped)} orphan(s): {reaped}")
    else:
        print("no orphans found")
    return 0


# ----------------------------------------------------------------- #
# Daemon-RPC helpers (best-effort, never raise)
# ----------------------------------------------------------------- #

def _best_effort_shutdown(label: str) -> None:
    try:
        from claude_hooks import daemon_client as dc
    except ImportError:
        return
    try:
        dc.chat_model_shutdown(label, timeout=15.0)
    except Exception:
        pass


def _best_effort_status(label: str) -> Optional[dict]:
    try:
        from claude_hooks import daemon_client as dc
    except ImportError:
        return None
    try:
        resp = dc.chat_model_status(label, timeout=5.0)
    except Exception:
        return None
    if resp is None or resp.get("available") is False:
        return None
    models = resp.get("models") or []
    for m in models:
        if m.get("label") == label:
            return m
    return None


# ----------------------------------------------------------------- #
# Main
# ----------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-hooks-models",
        description="Manage llamafile chat-model registry entries.",
    )
    p.add_argument("--registry", type=Path, default=None,
                   help="Override registry path (default: "
                        f"{DEFAULT_REGISTRY_PATH})")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("list", help="list registered models")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sp.add_parser("add", help="register a new model")
    s.add_argument("label")
    s.add_argument("gguf_path")
    s.add_argument("--ctx", type=int, default=16384)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--mode", choices=["auto", "cpu"], default="auto")
    s.add_argument("--idle-timeout", type=int, default=None,
                   dest="idle_timeout")
    s.add_argument("--notes", default="")
    s.set_defaults(func=cmd_add)

    s = sp.add_parser("remove", help="remove a registered model")
    s.add_argument("label")
    s.add_argument("--force", action="store_true",
                   help="skip the daemon shutdown RPC")
    s.set_defaults(func=cmd_remove)

    s = sp.add_parser("rename", help="rename a label")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=cmd_rename)

    s = sp.add_parser("copy", help="copy an entry under a new label")
    s.add_argument("src")
    s.add_argument("new_label")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--ctx", type=int, default=None)
    s.set_defaults(func=cmd_copy)

    s = sp.add_parser("show", help="show one entry (incl. live status)")
    s.add_argument("label")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sp.add_parser("path", help="print the registry file path")
    s.set_defaults(func=cmd_path)

    s = sp.add_parser("probe",
                      help="ask the daemon to bring up the model now")
    s.add_argument("label")
    s.set_defaults(func=cmd_probe)

    s = sp.add_parser("gc",
                      help="reap daemon-running models no longer in registry")
    s.set_defaults(func=cmd_gc)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    reg = Registry(path=args.registry or DEFAULT_REGISTRY_PATH)
    return args.func(reg, args)


if __name__ == "__main__":
    raise SystemExit(main())
