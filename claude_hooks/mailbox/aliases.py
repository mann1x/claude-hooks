"""Durable aliases from a shell (ch-18).

The ``mailbox-durable`` MCP tool registers a durable alias for the session
calling it. This is the operator's side of the same thing, for an
address no session will ever ask for — the file mailbox on pandorum
(``claude-mailbox@pandorum``), which had nothing registered between
sessions, so nothing listed it and a bare ``claude-mailbox`` resolved to
nobody. It writes to the shared store, so it can register an alias for
another host::

    python -m claude_hooks.mailbox.aliases list
    python -m claude_hooks.mailbox.aliases register claude-mailbox@pandorum
    python -m claude_hooks.mailbox.aliases register osync --ttl 30d \\
        --owner claude-hooks@solidpc
    python -m claude_hooks.mailbox.aliases remove osync@cloud

``--owner`` defaults to the alias itself: an address nobody acts as is
its own owner. Registering one held by another owner, or by a live
session, needs ``--force``; one live on another host is always refused.
"""
from __future__ import annotations

import argparse
from typing import Optional

from claude_hooks.mailbox.store import MailboxError, utcnow
from claude_hooks.mailbox.tools import parse_ttl, split_alias


def _store():
    from claude_hooks.config import load_config
    from claude_hooks.mailbox.relay import _store_from_config
    return _store_from_config(load_config())


def main(argv: Optional[list[str]] = None, *, store=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m claude_hooks.mailbox.aliases",
        description="Durable mailbox aliases.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="every durable alias")
    p_list.add_argument("--owner", help="only this owner's (alias@host)")
    p_reg = sub.add_parser("register", help="make (or renew) a durable "
                                            "alias")
    p_reg.add_argument("alias", help="alias@host (bare alias = @cloud)")
    p_reg.add_argument("--ttl", default="", help="30d / 12h / 2w; "
                                                 "default: no expiry")
    p_reg.add_argument("--owner", help="alias@host allowed to act as it "
                                       "(default: the alias itself)")
    p_reg.add_argument("--force", action="store_true",
                       help="take it from another owner or a live session")
    p_reg.add_argument("--os", dest="os_label",
                       help="linux / windows / darwin, as mailbox-sessions "
                            "shows it (default: this machine's for its own "
                            "host, else none)")
    p_rm = sub.add_parser("remove", help="end a durable alias")
    p_rm.add_argument("alias")
    p_rm.add_argument("--owner")
    p_rm.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    store = store if store is not None else _store()

    if args.cmd == "list":
        rows = store.durable_rows(owner=args.owner)
        if not rows:
            print("no durable aliases")
        for r in rows:
            until = r.get("durable_until")
            print(f"{r['alias']}@{r['host']}  owner {r.get('owner')}  "
                  + (f"until {str(until)[:16]}" if until else "no expiry"))
        return 0

    alias, host = split_alias(args.alias)
    owner = args.owner or f"{alias}@{host}"
    try:
        if args.cmd == "remove":
            if not store.release_durable(alias, host, owner=owner,
                                         force=args.force):
                print(f"{alias}@{host} is not a durable alias")
                return 1
            print(f"removed {alias}@{host}")
            return 0
        ttl = parse_ttl(args.ttl)
        res = store.register_durable(
            alias, host, owner=owner, force=args.force,
            os_label=args.os_label,
            until=(utcnow() + ttl) if ttl else None)
    except MailboxError as e:
        print(f"refused: {e}")
        return 1
    print(f"{'renewed' if res['renewed'] else 'registered'} "
          f"{alias}@{host}, owner {owner}, "
          + (f"until {res['until']:%Y-%m-%d %H:%M} UTC" if res["until"]
             else "no expiry"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
