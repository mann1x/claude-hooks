"""``claude-hooks-res``: claim hosts, GPUs, pods and the cloud account.

    claude-hooks-res status                        # who holds what, who waits
    claude-hooks-res acquire solidpc/gpu0 -r "KLD run"            # now or exit 1
    claude-hooks-res acquire solidpc/gpu0:x solidpc:quiet -r bench --ttl 3h
    claude-hooks-res wait bs2/gpu1 -r "gate"       # book + block until granted
    claude-hooks-res wait --claim 3f2a9c…          # wait on an existing booking
    claude-hooks-res run solidpc/gpu0 -r "ppl" -- ./llama-perplexity …
    claude-hooks-res release solidpc/gpu0          # or --claim ID, or --all
    claude-hooks-res check solidpc:quiet           # exit 0 if grantable now
    claude-hooks-res quiet-check [HOST]            # exit 1 while someone keeps HOST quiet
    claude-hooks-res define ollama-cloud --capacity 3 -d "Ollama Pro connections"
    claude-hooks-res events -n 30

A spec is ``RES[:MODE[:UNITS]]``; MODE is ``shared``/``s``, ``exclusive``/
``x`` (default) or ``quiet``/``q``. Several specs in one command are one
claim, granted all together or not at all.

From a Claude Code session, run ``wait`` as a **background** command: it
exits the moment the claim is granted, which is the notification. A claim
made from a session lasts until it is released, its TTL passes, or the
session ends; ``run`` holds its claim exactly as long as the command.

Exit codes: 0 granted / done, 1 busy / timed out / refused, 2 usage,
3 queued (``acquire --queue``).
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import threading
from typing import Optional

from claude_hooks.resources import model
from claude_hooks.resources.model import Claim, SpecError

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)


def parse_duration(text: Optional[str]) -> float:
    """``90``, ``90s``, ``30m``, ``2h``, ``1d`` → seconds; empty → 0."""
    if not text:
        return 0.0
    m = _DUR.match(str(text))
    if not m:
        raise SpecError(f"bad duration {text!r}: e.g. 90s, 30m, 2h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600,
                                "d": 86400}[m.group(2).lower()]


def _store():
    from claude_hooks.resources import api
    return api.store()


def _owner(args, *, own_process: bool = False):
    from claude_hooks.resources import api
    try:
        return api.owner_here(session_id=args.session, own_process=own_process,
                              follow_pid=getattr(args, "follow_pid", None))
    except ValueError as e:
        raise SpecError(str(e)) from None


def _items(specs) -> tuple:
    return tuple(model.parse_item(s) for s in specs)


def _fmt_claim(c: Claim) -> str:
    since = (c.granted_at or c.created_at)[:19].replace("T", " ")
    ttl = (f", until {c.expires_at[:19].replace('T', ' ')}" if c.expires_at
           else f", for {c.ttl_s // 60} min once granted" if c.ttl_s else "")
    why = f" — {c.reason}" if c.reason else ""
    return f"{c.label()}  [{c.owner}, since {since} UTC{ttl}, id {c.id}]{why}"


def _granted_line(c: Claim, reused: bool = False) -> str:
    verb = "ALREADY HELD" if reused else "GRANTED"
    return (f"{verb} {c.id}: {c.label()}\n"
            f"  release with: claude-hooks-res release --claim {c.id}")


def cmd_status(args) -> int:
    outs, capacity, defs = _store().status()
    if args.json:
        print(json.dumps({
            "claims": [{"id": o.claim.id, "state": o.claim.state,
                        "items": [str(i) for i in o.claim.items],
                        "owner": o.claim.owner, "session_id": o.claim.session_id,
                        "reason": o.claim.reason, "since": o.claim.granted_at
                        or o.claim.created_at, "expires_at": o.claim.expires_at,
                        "position": o.position,
                        "waiting_for": o.block.describe() if o.block else ""}
                       for o in outs],
            "definitions": defs}, indent=2))
        return 0
    held = [o for o in outs if o.granted]
    queue = [o for o in outs if not o.granted]
    if not held and not queue:
        print("nothing held, nobody waiting")
    for o in held:
        print("HELD  " + _fmt_claim(o.claim))
    for o in queue:
        print(f"WAIT#{o.position} " + _fmt_claim(o.claim))
        if o.block:
            print(f"        waiting: {o.block.describe()}")
    if defs and args.defs:
        print("\ndefinitions:")
        for d in defs:
            cap = f"capacity {d['capacity']}" if d["capacity"] else "no capacity"
            print(f"  {d['name']}: {cap}  {d['description']}")
    return 0


def _request(args, *, own_process: bool = False):
    items = _items(args.specs)
    return _store().request(
        items, _owner(args, own_process=own_process), reason=args.reason or "",
        ttl_s=parse_duration(args.ttl), lease_s=int(parse_duration(args.lease)
                                                   or 0) or _default_lease())


def _default_lease() -> int:
    from claude_hooks.resources.store import DEFAULT_LEASE_S
    return DEFAULT_LEASE_S


def _cancel(claim_id: str, session: str) -> None:
    try:
        _store().release([claim_id], session_id=session, event="cancelled")
    except Exception as e:      # the booking still ends with its session
        print(f"(could not cancel booking {claim_id}: {e})", file=sys.stderr)


def _say_waiting(out) -> None:
    print(f"QUEUED {out.claim.id} at position {out.position}: "
          f"{out.claim.label()}", flush=True)
    if out.block:
        print(f"  waiting: {out.block.describe()}", flush=True)
    if out.self_blocked:
        print("  NOTE: blocked by a claim of this same session — release it "
              "or this will never be granted", flush=True)


def cmd_acquire(args) -> int:
    out = _request(args)
    if out.granted:
        print(_granted_line(out.claim, out.reused))
        return 0
    if args.queue:
        _say_waiting(out)
        print(f"  wait for it (run in the background): claude-hooks-res wait "
              f"--claim {out.claim.id}")
        return 3
    _cancel(out.claim.id, out.claim.session_id)
    print(f"BUSY: {out.block.describe() if out.block else out.claim.label()}",
          file=sys.stderr)
    print(f"  book and wait (in the background): claude-hooks-res wait "
          f"{' '.join(args.specs)}" + (f" -r {json.dumps(args.reason)}"
                                       if args.reason else ""),
          file=sys.stderr)
    return 1


def _wait_on(claim_id: str, session: str, timeout_s: float, keep: bool) -> int:
    st = _store()
    stop = threading.Event()

    def on_signal(signum, _frame):
        stop.set()
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    try:
        out = st.wait(claim_id, timeout_s=timeout_s or None,
                      on_update=_say_waiting, stop=stop)
    except KeyboardInterrupt:
        if not keep:
            _cancel(claim_id, session)
            print(f"CANCELLED {claim_id} (interrupted)")
        return 1
    if out is None:
        print(f"GONE {claim_id}: released, expired or its holder ended "
              f"before it was granted")
        return 1
    if out.granted:
        print(_granted_line(out.claim))
        return 0
    if keep:
        print(f"TIMEOUT {claim_id}: still queued at position {out.position} "
              f"(kept)")
    else:
        _cancel(claim_id, session)
        print(f"TIMEOUT {claim_id}: not granted in time; booking cancelled")
    return 1


def cmd_wait(args) -> int:
    timeout = parse_duration(args.timeout)
    if args.claim:
        out = _store().get(args.claim)
        if out is None:
            print(f"GONE {args.claim}: no such claim (released or expired)")
            return 1
        if out.granted:
            print(_granted_line(out.claim))
            return 0
        _say_waiting(out)
        return _wait_on(args.claim, out.claim.session_id, timeout, args.keep)
    if not args.specs:
        print("wait: name resources or --claim ID", file=sys.stderr)
        return 2
    out = _request(args)
    if out.granted:
        print(_granted_line(out.claim, out.reused))
        return 0
    _say_waiting(out)
    return _wait_on(out.claim.id, out.claim.session_id, timeout, args.keep)


def cmd_run(args) -> int:
    cmd = list(args.command or [])
    if not cmd:
        print("run: give the command after --", file=sys.stderr)
        return 2
    st = _store()
    out = _request(args, own_process=True)
    if not out.granted:
        _say_waiting(out)
        rc = _wait_on(out.claim.id, out.claim.session_id,
                      parse_duration(args.timeout), keep=False)
        if rc != 0:
            return rc
        out = st.get(out.claim.id)
        if out is None:
            return 1
    else:
        print(_granted_line(out.claim, out.reused), flush=True)
    claim = out.claim
    done = threading.Event()

    def heartbeat():
        from claude_hooks.resources.store import MAINTENANCE_INTERVAL_S
        while not done.wait(min(60.0, MAINTENANCE_INTERVAL_S)):
            try:
                st.renew(claim_ids=[claim.id])
            except Exception:
                pass

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    proc = None
    try:
        proc = subprocess.Popen(cmd)
        rc = proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            proc.terminate()
            rc = proc.wait()
        else:
            rc = 130
    except OSError as e:
        print(f"run: {e}", file=sys.stderr)
        rc = 127
    finally:
        done.set()
        if not out.reused:
            try:
                st.release([claim.id], session_id=claim.session_id,
                           by="run exited")
            except Exception as e:
                print(f"(release failed, the claim ends with this process: "
                      f"{e})", file=sys.stderr)
    return rc


def cmd_release(args) -> int:
    from claude_hooks.resources import api
    session = args.session if args.session is not None else api.session_id_here()
    st = _store()
    if args.claim:
        done, refused = st.release(args.claim, session_id=session,
                                   force=args.force)
    elif args.all or args.specs:
        if not session:
            print("release: no session here (CLAUDE_CODE_SESSION_ID); use "
                  "--claim ID", file=sys.stderr)
            return 2
        names = [model.parse_item(s).resource for s in args.specs]
        done, refused = st.release(session_id=session, resources=names)
    else:
        print("release: name resources, --claim ID or --all", file=sys.stderr)
        return 2
    for c in done:
        print(f"RELEASED {c.id}: {c.label()}")
    for c in refused:
        print(f"REFUSED {c.id}: held by {c.owner}, not this session — "
              f"--force to break it", file=sys.stderr)
    if not done and not refused:
        print("nothing to release")
    return 1 if refused else 0


def cmd_check(args) -> int:
    items = _items(args.specs)
    outs, capacity, _ = _store().status()
    probe = Claim(id="?", seq=1 << 62, items=items)
    granted = [o.claim for o in outs if o.granted]
    waiting = [o.claim for o in outs if not o.granted]
    b = model.why_waiting(probe, granted, waiting, capacity)
    if b is None:
        print(f"FREE: {probe.label()}")
        return 0
    print(f"BUSY: {b.describe()}")
    return 1


def cmd_quiet_check(args) -> int:
    from claude_hooks.resources import api
    c = api.quiet_holder(args.host, session_id=api.session_id_here())
    if c is None:
        print("not quiet")
        return 0
    print(f"QUIET: {_fmt_claim(c)}")
    return 1


def cmd_renew(args) -> int:
    from claude_hooks.resources import api
    session = args.session if args.session is not None else api.session_id_here()
    n = _store().renew(session_id=session if not args.host_wide else "")
    print(f"renewed {n} claim(s)")
    return 0


def cmd_define(args) -> int:
    _store().define(args.name, capacity=args.capacity,
                    description=args.description or "")
    print(f"defined {model.normalize(args.name)}"
          + (f" (capacity {args.capacity})" if args.capacity else ""))
    return 0


def cmd_undefine(args) -> int:
    ok = _store().undefine(args.name)
    print("removed" if ok else "no such definition")
    return 0 if ok else 1


def cmd_events(args) -> int:
    for e in reversed(_store().events(args.limit)):
        print(f"{e['at'][:19].replace('T', ' ')}  {e['event']:<15} "
              f"{e['items']:<40} {e['alias']}@{e['host']}  {e['detail']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claude-hooks-res", description="Shared / exclusive / quiet "
        "claims on hosts, GPUs, pods and the cloud account.",
        epilog="Specs: RES[:MODE[:UNITS]], e.g. solidpc/gpu0, solidpc:quiet, "
               "ollama-cloud:shared:2.")
    ap.add_argument("--session", default=None,
                    help="act as this session id (default: "
                         "$CLAUDE_CODE_SESSION_ID)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def claim_opts(p, *, specs_required=True):
        p.add_argument("specs", nargs="+" if specs_required else "*",
                       metavar="RES[:MODE[:UNITS]]")
        p.add_argument("-r", "--reason", default="",
                       help="what it is for (shown to everyone waiting)")
        p.add_argument("--ttl", default="",
                       help="end the claim after this long (e.g. 3h)")
        p.add_argument("--lease", default="",
                       help="how long other hosts trust it without a "
                            "renewal (default 30m)")
        p.add_argument("--follow-pid", type=int, default=None,
                       help="tie the claim to this process (a detached "
                            "job) instead of the session")

    p = sub.add_parser("status", help="who holds what, who waits")
    p.add_argument("--json", action="store_true")
    p.add_argument("--defs", action="store_true", help="also definitions")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("acquire", help="claim now, or exit 1 (busy)")
    claim_opts(p)
    p.add_argument("--queue", action="store_true",
                   help="keep the booking when busy (exit 3)")
    p.set_defaults(fn=cmd_acquire)

    p = sub.add_parser("wait", help="book and block until granted")
    claim_opts(p, specs_required=False)
    p.add_argument("--claim", help="wait on an existing booking")
    p.add_argument("--timeout", default="", help="give up after (e.g. 2h)")
    p.add_argument("--keep", action="store_true",
                   help="keep the booking on timeout / interrupt")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("run", help="claim, run a command, release")
    p.add_argument("specs", nargs="+", metavar="RES[:MODE[:UNITS]]")
    p.add_argument("-r", "--reason", default="")
    p.add_argument("--ttl", default="")
    p.add_argument("--lease", default="")
    p.add_argument("--timeout", default="", help="give up waiting after")
    p.set_defaults(fn=cmd_run, command=[])

    p = sub.add_parser("release", help="release claims")
    p.add_argument("specs", nargs="*", metavar="RES")
    p.add_argument("--claim", action="append", default=[])
    p.add_argument("--all", action="store_true",
                   help="every claim of this session")
    p.add_argument("--force", action="store_true",
                   help="break another session's claim (logged)")
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("check", help="exit 0 if grantable now, books nothing")
    p.add_argument("specs", nargs="+", metavar="RES[:MODE[:UNITS]]")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("quiet-check",
                       help="exit 1 while another session keeps HOST quiet")
    p.add_argument("host", nargs="?", default=None)
    p.set_defaults(fn=cmd_quiet_check)

    p = sub.add_parser("renew", help="renew this session's leases")
    p.add_argument("--host-wide", action="store_true",
                   help="every live claim of this host")
    p.set_defaults(fn=cmd_renew)

    p = sub.add_parser("define", help="give a resource a capacity / text")
    p.add_argument("name")
    p.add_argument("--capacity", type=int, default=None)
    p.add_argument("-d", "--description", default="")
    p.set_defaults(fn=cmd_define)

    p = sub.add_parser("undefine", help="remove a definition")
    p.add_argument("name")
    p.set_defaults(fn=cmd_undefine)

    p = sub.add_parser("events", help="recent history")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(fn=cmd_events)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    from claude_hooks.mcp_stdio import force_utf8_stdio
    force_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    # ``run … -- cmd``: everything after the first ``--`` is the command,
    # never options of ours (argparse would read its -r / --ttl).
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        args.command = command
    try:
        return int(args.fn(args) or 0)
    except SpecError as e:
        print(f"claude-hooks-res: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
