"""The nine mailbox MCP tools.

Added to the existing memory MCP servers rather than shipped as a skill,
so they are present in every client with no skill load and no slash
command — which is what "sessions should exchange messages directly"
requires. A skill the model has to remember to invoke is the same
failure the hooks exist to fix.

Catalog and dispatch live here so both the pgvector and sqlite_vec
servers expose an identical surface; the only difference between them
is which connection the store borrows.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Optional

from claude_hooks.mailbox.addressing import AddressError, describe_recipients
from claude_hooks.mailbox.announce import ago
from claude_hooks.mailbox.filters import (
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ORDERS, FilterError, ListFilter,
    page_number, page_size, parse_when,
)
from claude_hooks.mailbox.store import (
    MailboxError, MailboxStore, describe_skipped, host_name, utcnow,
)

log = logging.getLogger("claude_hooks.mailbox.tools")

TOOL_NAMES = (
    "mailbox-send", "mailbox-list", "mailbox-read", "mailbox-ack",
    "mailbox-edit", "mailbox-cancel", "mailbox-sent", "mailbox-sessions",
    "mailbox-durable",
)

#: The host of a durable alias given with no ``@host``: what a linked
#: cloud thread is everywhere else.
CLOUD_HOST = "cloud"

#: Tools that act as one identity and so accept ``as`` (``from`` on
#: send): a durable alias this session owns.
AS_TOOLS = frozenset({"mailbox-list", "mailbox-read", "mailbox-ack",
                      "mailbox-edit", "mailbox-cancel", "mailbox-sent"})

_AS_PROPERTY = {"as": {
    "type": "string",
    "description": "act as a durable alias you registered with "
                   "mailbox-durable (`osync` or `osync@cloud`)"}}


def _listing_properties(party: str, party_desc: str, *,
                        include_read: bool = True) -> dict:
    props: dict = {
        "page": {"type": "integer", "default": 1,
                 "description": "1-based page number"},
        "limit": {"type": "integer", "default": DEFAULT_PAGE_SIZE,
                  "description": f"messages per page, at most {MAX_PAGE_SIZE}"},
        "query": {"type": "string",
                  "description": "keywords; every one must match"},
        party: {"type": "string", "description": party_desc},
        "since": {"type": "string",
                  "description": "newer than: 3d / 12h / 30m / 2w, "
                                 "2026-09-27 or 2026-09-27T14:30"},
        "until": {"type": "string",
                  "description": "older than, same forms; a bare date "
                                 "includes that day"},
        "order": {"type": "string", "enum": list(ORDERS)},
        **_AS_PROPERTY,
    }
    if include_read:
        props = {"include_read": {"type": "boolean", "default": False},
                 **props}
    return props


def tool_catalog() -> list[dict]:
    return [
        {
            "name": "mailbox-send",
            "description": (
                "Send a message to another Claude session. `to` is an alias "
                "(`osync`), an alias on a host (`osync@solidpc`), a broadcast "
                "(`osync*`), or a session id. A bare alias registered on more "
                "than one host is refused with the candidates listed — "
                "nothing is sent. Sending to an alias with no live session is "
                "fine; the message waits."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string",
                           "description": "alias, alias@host, alias*, or "
                                          "session id"},
                    "subject": {"type": "string",
                                "description": "One line. This is all the "
                                               "recipient sees before "
                                               "choosing to read."},
                    "body": {"type": "string"},
                    "priority": {"type": "integer", "default": 0,
                                 "description": "0 normal, 1+ raises it in "
                                                "the recipient's "
                                                "announcement"},
                    "from": {"type": "string",
                             "description": "send as a durable alias you "
                                            "registered with mailbox-durable "
                                            "(`osync` or `osync@cloud`); "
                                            "replies then reach that "
                                            "alias's inbox"},
                },
                "required": ["to", "subject", "body"],
            },
        },
        {
            "name": "mailbox-list",
            "description": (
                "Messages addressed to this session — subject, sender, time "
                "and priority only, one page at a time (20 by default). "
                "Unread only unless include_read; unread comes most urgent "
                "then oldest first, a listing with read mail newest first. "
                "Narrow it with query (keywords, all must match subject, "
                "body or sender; \"quotes\" keep a phrase), from, since and "
                "until. The page says how many matched and how to get the "
                "next one. Use mailbox-read for bodies."),
            "inputSchema": {
                "type": "object",
                "properties": _listing_properties("from", "sender alias"),
            },
        },
        {
            "name": "mailbox-read",
            "description": (
                "Read full message bodies by id. This marks them read, which "
                "is what stops them being announced again."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    **_AS_PROPERTY,
                },
                "required": ["ids"],
            },
        },
        {
            "name": "mailbox-ack",
            "description": (
                "Attach a short note to a message you have read, which the "
                "sender will be shown. Optional by design: with no note the "
                "sender is told nothing, because a bare 'it was read' needs "
                "no action. Use it for the cheap reply that does not justify "
                "a whole message — 'confirmed, ~2h' or 'already fixed'. Can "
                "be called later than the read, and replaced until the sender "
                "sees it."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "ids": {"type": "array", "items": {"type": "integer"},
                            "description": "Several messages, one note."},
                    "note": {"type": "string"},
                    **_AS_PROPERTY,
                },
                "required": ["note"],
            },
        },
        {
            "name": "mailbox-edit",
            "description": (
                "Change the subject, body or priority of a message you sent "
                "that has not been read yet. Refused once read — send a "
                "correction instead."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "priority": {"type": "integer"},
                    **_AS_PROPERTY,
                },
                "required": ["id"],
            },
        },
        {
            "name": "mailbox-cancel",
            "description": (
                "Withdraw an unread message you sent. Refused once read."),
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, **_AS_PROPERTY},
                "required": ["id"],
            },
        },
        {
            "name": "mailbox-sent",
            "description": (
                "Messages you sent, newest first, one page at a time (20 by "
                "default), with whether and when each was read and any note "
                "the recipient attached. Narrow it with query, to, since and "
                "until, as for mailbox-list."),
            "inputSchema": {
                "type": "object",
                "properties": _listing_properties("to", "recipient alias",
                                                  include_read=False),
            },
        },
        {
            "name": "mailbox-sessions",
            "description": (
                "Who is registered: alias, host, OS and when last seen. Use "
                "this to find a Windows session for 'run this on Windows', or "
                "to check an alias before sending to it."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string"},
                    "os": {"type": "string",
                           "description": "linux | windows | darwin"},
                },
            },
        },
        {
            "name": "mailbox-durable",
            "description": (
                "Durable aliases: an address that lasts without a session "
                "behind it — what a cloud thread linked to this machine uses "
                "to be `osync@cloud` across days. `register` makes (or "
                "renews) one you own; it stays until `remove` or its `ttl`. "
                "Then pass `from` on mailbox-send and `as` on mailbox-list / "
                "-read / -ack / -sent / -edit / -cancel to act as it; "
                "replies land in its inbox. `list` shows yours. An alias "
                "held by a live session, or live on another host, is "
                "refused."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["register", "remove", "list"],
                               "default": "list"},
                    "alias": {"type": "string",
                              "description": "`osync` (= osync@cloud) or "
                                             "alias@host for this host"},
                    "ttl": {"type": "string",
                            "description": "how long it lasts: 30d / 12h "
                                           "/ 2w; empty = until removed"},
                },
            },
        },
    ]


class MailboxTools:
    """Dispatch for the nine tools.

    ``identity`` supplies who *this* session is. It is resolved once per
    server process rather than per call: a tool that had to be told its
    own alias every time would make every caller's first mistake a
    silent misdelivery.
    """

    def __init__(self, store: MailboxStore, *, alias: str,
                 session_id: Optional[str] = None,
                 host: Optional[str] = None):
        self.store = store
        self.alias = alias
        self.session_id = session_id or ""
        self.host = host or host_name()
        self._registered = False
        #: Re-derives ``(alias, session_id, note)`` before every call, so
        #: a ``/rename`` reaches the tools without restarting the server.
        #: Set by ``integration.tools_for_provider``; None keeps the
        #: identity this object was built with.
        self.resolver = None
        #: A one-line identity note not yet shown to the session.
        self.identity_note = ""

    def handles(self, name: str) -> bool:
        return name in TOOL_NAMES

    #: What a headless run may still do: send (a script asking ``claude -p``
    #: to mail someone is legitimate) and look. Reading marks mail read and
    #: acking answers it — both belong to the session the mail is for.
    HEADLESS_REFUSED = frozenset({"mailbox-read", "mailbox-ack"})

    def call(self, name: str, args: dict, *,
             headless: Optional[bool] = None) -> str:
        """``headless`` defaults to this process's own kind. The cloud
        relay passes False: it runs inside the daemon, whose environment
        says nothing about the session the request came from."""
        from claude_hooks.mailbox.integration import is_headless
        if headless is None:
            headless = is_headless()
        note = self.refresh_identity()
        out = self._call(name, args, headless=headless)
        return f"{note}\n\n{out}" if note else out

    def refresh_identity(self) -> str:
        """Re-resolve who this session is; return a note to show, or "".

        Every operation checks the session's name, so a ``/rename``
        takes effect at the next one. A resolver that fails keeps the
        identity already held: a mailbox that stops working because a
        transcript was unreadable would be the worse failure.
        """
        note, self.identity_note = self.identity_note, ""
        if self.resolver is None:
            return note
        try:
            alias, sid, fresh = self.resolver()
        except Exception:
            log.debug("mailbox: identity refresh failed", exc_info=True)
            return note
        if alias:
            self.alias = alias
        if sid:
            self.session_id = sid
        return " ".join(n for n in (note, fresh) if n)

    @property
    def owner(self) -> str:
        """Who owns the durable aliases this session registers."""
        return f"{self.alias}@{self.host}"

    def _as(self, name: str, args: dict) -> Optional[str]:
        """The durable alias this call acts as (``from`` on send, ``as``
        elsewhere), or None for this session's own identity."""
        key = "from" if name == "mailbox-send" else "as"
        if name != "mailbox-send" and name not in AS_TOOLS:
            return None
        raw = str(args.get(key) or "").strip()
        return raw or None

    def as_identity(self, raw: str) -> "MailboxTools":
        """A dispatcher acting as durable alias ``raw``, which this
        session must own. MailboxError otherwise."""
        alias, host = split_alias(raw)
        if (alias, host) == (self.alias, self.host):
            return self
        row = self.store.owned(alias, host, self.owner)
        if row is None:
            raise MailboxError(
                f"{alias}@{host} is not a durable alias of {self.owner}. "
                "Register it with mailbox-durable first (action register).")
        view = MailboxTools(self.store, alias=alias,
                            session_id=row["session_id"], host=host)
        view._registered = True     # the durable row is the registration
        return view

    def _call(self, name: str, args: dict, *, headless: bool) -> str:
        raw = self._as(name, args)
        if raw:
            try:
                view = self.as_identity(raw)
            except (MailboxError, AddressError) as e:
                return str(e)
            if view is not self:
                rest = {k: v for k, v in args.items()
                        if k not in ("from", "as")}
                return view._call(name, rest, headless=headless)
        if headless:
            if name in self.HEADLESS_REFUSED:
                return (f"{name} is not available in a non-interactive run "
                        "(claude -p / SDK): reading would mark "
                        f"{self.alias}@{self.host}'s mail read for a session "
                        "nobody is watching. The interactive session for "
                        "this project reads its own mail.")
            try:
                return getattr(self, "_" + name.replace("-", "_"))(args)
            except (MailboxError, AddressError) as e:
                return str(e)
        self._mark_active()
        try:
            return getattr(self, "_" + name.replace("-", "_"))(args)
        except (MailboxError, AddressError) as e:
            # Both are the caller's to fix, so they go back as prose.
            return str(e)

    def _mark_active(self) -> None:
        """Register once, and prove liveness on *every* tool call.

        Registration otherwise happens only in the SessionStart hook,
        which leaves a session using the MCP tools without the hooks —
        Cline, Cursor, a bare MCP client — able to send and read while
        never appearing in ``mailbox-sessions``. Its mail still works,
        because an alias with no live session simply parks, but nobody
        can *discover* it, and a correspondent reading the session list
        concludes it is not there.

        The refresh is the part that has to happen every time. The hook
        path touches ``last_seen`` once per turn, which is only an
        approximation of activity and a bad one: a session that spends
        fifteen minutes inside a single turn checking, reading and
        sending mail did all of it without moving the timestamp, so it
        read as idle for a quarter of an hour while it was the busiest
        thing in the registry. Anything that decides who is alive —
        eviction, the live-session window, ``mailbox-sessions`` — was
        answering from that. Using the mailbox is the strongest possible
        evidence a session is alive, and it was the one signal not
        recorded.

        One indexed UPDATE on a primary key, and soft-fail throughout:
        being unlisted, or looking stale, is a smaller problem than a
        mailbox that refuses to work.
        """
        if not self.session_id:
            # No id to key on — Claude Code does not give an MCP child
            # one. Refresh by alias instead, which the unique index
            # makes unambiguous. See ``MailboxStore.touch_alias``.
            try:
                self.store.touch_alias(self.alias, self.host)
            except Exception:
                log.debug("mailbox: could not refresh %s@%s by alias",
                          self.alias, self.host, exc_info=True)
            return
        if not self._registered and self.resolver is None:
            # With a resolver the registration is already made, by
            # ``MailboxStore.claim`` — which, unlike ``register``, never
            # takes an alias from a live session.
            self._registered = True
            try:
                self.store.register(self.session_id, self.alias,
                                    host=self.host)
                return          # register() already stamps last_seen
            except Exception:
                log.debug("mailbox: could not self-register %s@%s",
                          self.alias, self.host, exc_info=True)
        try:
            # Carry the identity: if this row was evicted while the
            # session was quiet, the touch rebuilds it rather than
            # leaving a live session unaddressable.
            self.store.touch(self.session_id, alias=self.alias,
                             host=self.host)
        except Exception:
            log.debug("mailbox: could not refresh %s@%s",
                      self.alias, self.host, exc_info=True)

    # ─── tools ───────────────────────────────────────────────────────

    def _mailbox_send(self, args: dict) -> str:
        res = self.store.send(
            str(args.get("to") or ""),
            str(args.get("subject") or ""),
            str(args.get("body") or ""),
            from_alias=self.alias,
            from_session=self.session_id or None,
            from_host=self.host,
            priority=int(args.get("priority") or 0),
        )
        ids = ", ".join(str(i) for i in res["ids"])
        out = (f"Sent (id {ids}) — "
               + describe_recipients(res["recipients"], res["address"]))
        skipped = res.get("skipped") or []
        if skipped:
            # A partial send has to say what it did *not* do, or the
            # confirmation is a claim about recipients that never got it.
            out += f" Skipped {describe_skipped(skipped)}."
        return out

    def _mailbox_list(self, args: dict) -> str:
        try:
            f, when, size, page = _listing_args(args, party_key="from")
        except FilterError as e:
            return str(e)
        pg = self.store.inbox_page(
            alias=self.alias, session_id=self.session_id or None,
            host=self.host, include_read=f.include_read,
            since=when[0], until=when[1], query=f.query, sender=f.party,
            page=page, page_size=size, order=f.order or None)
        what = "message(s)" if f.include_read else "unread message(s)"
        scope = f.describe("from")
        hint = self._durable_hint()
        if not pg.total:
            return (f"No messages for {self.alias}@{self.host}"
                    + ("" if f.include_read else " (unread)")
                    + (f" {scope}" if scope else "") + "." + hint)
        out = [_page_header(pg, what, f"{self.alias}@{self.host}", scope)]
        if not pg.rows:
            return _past_the_end(pg, out[0])
        for m in pg.rows:
            state = "read" if m["read_at"] else "unread"
            pri = f" [priority {m['priority']}]" if m["priority"] else ""
            out.append(
                f"  #{m['id']} [{state}]{pri} {m['subject']!r} — from "
                f"{m['from_alias']}"
                + (f"@{m['from_host']}" if m["from_host"] else "")
                + f", {ago(m['created_at'])}")
        out.append("")
        out.append("Use mailbox-read with the ids to see the bodies.")
        out += _page_footer(pg, "mailbox-list", f)
        return "\n".join(out) + hint

    def _durable_hint(self) -> str:
        """A line naming this session's durable aliases with unread mail:
        their inbox is not this one, and a thread that forgot it owned
        one would never look."""
        try:
            rows = self.store.durable_rows(owner=self.owner)
            waiting = []
            for r in rows:
                n = self.store.inbox_count(alias=r["alias"],
                                           session_id=r["session_id"],
                                           host=r["host"])
                if n:
                    waiting.append(f"{r['alias']}@{r['host']} ({n})")
        except Exception:
            log.debug("mailbox: durable hint failed", exc_info=True)
            return ""
        if not waiting:
            return ""
        return ("\n\nUnread for your durable alias(es): "
                + ", ".join(waiting) + " — list it with as=<alias@host>.")

    def _mailbox_read(self, args: dict) -> str:
        ids = _int_list(args.get("ids"))
        if not ids:
            return "Pass ids — a list of message ids from mailbox-list."
        rows = self.store.read(ids, reader_session=self.session_id or "?",
                               alias=self.alias, host=self.host)
        if not rows:
            return ("No such message addressed to you. (mailbox-read only "
                    "returns mail addressed to this session.)")
        out = []
        for m in rows:
            out.append(f"── #{m['id']} {m['subject']} ──")
            out.append(f"from {m['from_alias']}"
                       + (f"@{m['from_host']}" if m["from_host"] else "")
                       + f", {ago(m['created_at'])}"
                       + (f" (edited {ago(m['edited_at'])})"
                          if m["edited_at"] else ""))
            out.append("")
            out.append(m["body"])
            out.append("")
        out.append("Marked read. If a short note is enough of a reply, use "
                   "mailbox-ack; otherwise mailbox-send.")
        return "\n".join(out)

    def _mailbox_ack(self, args: dict) -> str:
        # ``ids`` too: mailbox-read takes a list, and a session that
        # passed one here got a bare ``KeyError: 'id'`` (2026-10-24).
        ids = _int_list(args.get("ids")) + _int_list(args.get("id"))
        if not ids:
            return "Pass id (or ids) — the message(s) the note answers."
        note = str(args.get("note") or "")
        out = []
        for mid in dict.fromkeys(ids):
            try:
                res = self.store.ack(
                    mid, note, session_id=self.session_id or "?",
                    alias=self.alias, host=self.host)
            except MailboxError as e:
                out.append(f"#{mid}: {e}")
                continue
            verb = "Attached" if res["action"] == "attached" else "Replaced"
            out.append(f"{verb} the note on #{res['id']}. The sender will "
                       f"see it next time they check in.")
        return "\n".join(out)

    def _mailbox_edit(self, args: dict) -> str:
        res = self.store.edit(
            _one_id(args), from_alias=self.alias, from_host=self.host,
            subject=args.get("subject"), body=args.get("body"),
            priority=(int(args["priority"])
                      if args.get("priority") is not None else None))
        if res["broadcast_group"]:
            return (f"Updated {res['updated']} unread copy/copies of that "
                    f"broadcast. Copies already read were left alone.")
        return ("Updated." if res["updated"]
                else "Nothing changed — it may have just been read.")

    def _mailbox_cancel(self, args: dict) -> str:
        res = self.store.cancel(_one_id(args), from_alias=self.alias,
                                from_host=self.host)
        if res["broadcast_group"]:
            return (f"Withdrew {res['cancelled']} unread copy/copies of that "
                    f"broadcast.")
        return ("Withdrawn." if res["cancelled"]
                else "Nothing withdrawn — it may have just been read.")

    def _mailbox_sent(self, args: dict) -> str:
        try:
            f, when, size, page = _listing_args(args, party_key="to",
                                                include_read=False)
        except FilterError as e:
            return str(e)
        pg = self.store.sent_page(
            from_alias=self.alias, from_host=self.host, since=when[0],
            until=when[1], query=f.query, to=f.party, page=page,
            page_size=size, order=f.order or None)
        scope = f.describe("to")
        if not pg.total:
            return ("You have not sent any messages"
                    + (f" {scope}" if scope else "") + ".")
        head = _page_header(pg, "message(s) you sent", "", scope)
        if not pg.rows:
            return _past_the_end(pg, head)
        out = [head]
        pending: list[int] = []
        for m in pg.rows:
            to = m["to_alias"] or m["to_session"] or "?"
            if m["to_host"]:
                to = f"{to}@{m['to_host']}"
            if m["cancelled_at"]:
                state = "withdrawn"
            elif m["read_at"]:
                state = f"read by {m['read_by'] or '?'} {ago(m['read_at'])}"
            else:
                state = "unread"
            line = (f"  #{m['id']} → {to}: {m['subject']!r} — {state}, "
                    f"sent {ago(m['created_at'])}")
            if m["ack_body"]:
                line += f"\n      note: \"{m['ack_body'].strip()}\""
                if not m["receipt_read_at"]:
                    pending.append(m["id"])
            out.append(line)
        if pending:
            self.store.mark_receipts_seen(pending, from_alias=self.alias,
                                          from_host=self.host)
        out += _page_footer(pg, "mailbox-sent", f, leading_blank=True)
        return "\n".join(out)

    def _mailbox_sessions(self, args: dict) -> str:
        rows = self.store.sessions(alias=args.get("alias"),
                                   os_filter=args.get("os"))
        if not rows:
            return "No sessions registered."
        out = [f"{len(rows)} session(s):"]
        for s in rows:
            out.append(f"  {s.alias}@{s.host} [{s.os}] — last seen "
                       f"{ago(s.last_seen)}"
                       + (f", in {s.cwd}" if s.cwd else "")
                       + (f" [durable, {s.owner}]" if s.durable else ""))
        by_alias: dict[str, set] = {}
        for s in rows:
            by_alias.setdefault(s.alias, set()).add(s.host)
        dupes = {a: h for a, h in by_alias.items() if len(h) > 1}
        if dupes:
            out.append("")
            for a, hosts in sorted(dupes.items()):
                out.append(f"  `{a}` spans {len(hosts)} hosts — a bare "
                           f"`{a}` will be refused; use {a}@<host> or {a}*.")
        return "\n".join(out)

    def _mailbox_durable(self, args: dict) -> str:
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            rows = self.store.durable_rows(owner=self.owner)
            if not rows:
                return (f"{self.owner} owns no durable aliases. Register one "
                        "with action register, alias osync.")
            out = [f"{len(rows)} durable alias(es) of {self.owner}:"]
            for r in rows:
                n = self.store.inbox_count(alias=r["alias"],
                                           session_id=r["session_id"],
                                           host=r["host"])
                until = r.get("durable_until")
                out.append(f"  {r['alias']}@{r['host']} — "
                           + (f"until {str(until)[:16]}" if until
                              else "no expiry")
                           + f", {n} unread")
            out.append("")
            out.append("Act as one with `from` on mailbox-send, `as` on the "
                       "others.")
            return "\n".join(out)
        if action not in ("register", "remove"):
            return "action is register, remove or list."
        alias, host = split_alias(str(args.get("alias") or ""))
        if not alias:
            return "Pass alias — `osync` (osync@cloud) or alias@host."
        if host not in (CLOUD_HOST, self.host):
            return (f"A durable alias lives at {CLOUD_HOST} or at this "
                    f"session's own host ({self.host}), not {host}.")
        if action == "remove":
            if not self.store.release_durable(alias, host, owner=self.owner):
                return f"{alias}@{host} is not a durable alias."
            return (f"Removed {alias}@{host}. Mail already sent to it stays "
                    "and waits for whoever takes the alias next.")
        ttl = parse_ttl(args.get("ttl"))
        res = self.store.register_durable(
            alias, host, owner=self.owner,
            until=(utcnow() + ttl) if ttl else None)
        verb = "Renewed" if res["renewed"] else "Registered"
        last = (f"until {res['until'].strftime('%Y-%m-%d %H:%M')} UTC"
                if res["until"] else "until you remove it")
        return (f"{verb} {alias}@{host}, owned by {self.owner}, {last}. "
                f"Send as it with from={alias}@{host}; read its mail with "
                f"as={alias}@{host} on mailbox-list / mailbox-read.")


def split_alias(raw: str) -> tuple[str, str]:
    """``osync`` → (osync, cloud); ``osync@cloud`` → (osync, cloud)."""
    alias, _, host = (raw or "").strip().partition("@")
    return alias.strip(), (host.strip() or CLOUD_HOST)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*$", re.I)
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_ttl(raw: Any) -> Optional[timedelta]:
    """``30d`` / ``12h`` / ``90m`` / ``2w``; empty or ``never`` = None.
    MailboxError for anything else."""
    text = str(raw or "").strip().lower()
    if text in ("", "never", "none", "0"):
        return None
    m = _DURATION.match(text)
    if not m:
        raise MailboxError(f"ttl {raw!r}: use a number and m / h / d / w "
                           "(30d, 12h), or leave it empty for no expiry.")
    return timedelta(seconds=float(m.group(1)) * _UNIT_S[m.group(2)])


def _listing_args(args: dict, *, party_key: str, include_read: bool = True):
    """Validate a listing call; FilterError carries a fixable message."""
    order = (args.get("order") or "").strip().lower()
    if order and order not in ORDERS:
        raise FilterError(f"order must be one of {', '.join(ORDERS)}.")
    f = ListFilter(
        query=(args.get("query") or "").strip(),
        since=str(args.get("since") or "").strip(),
        until=str(args.get("until") or "").strip(),
        party=(args.get(party_key) or "").strip(),
        include_read=bool(args.get("include_read")) if include_read else False,
        order=order)
    if f.party:
        f.extra[party_key] = f.party
    size = page_size(args.get("limit"))
    if args.get("limit") is not None and size != DEFAULT_PAGE_SIZE:
        f.extra["limit"] = size
    when = (parse_when(f.since), parse_when(f.until, end=True))
    if when[0] and when[1] and when[0] >= when[1]:
        raise FilterError(f"since ({f.since}) is not before until ({f.until}).")
    return f, when, size, page_number(args.get("page"))


def _page_header(pg, what: str, owner: str, scope: str) -> str:
    rng = (f"{pg.first}–{pg.last} of {pg.total}" if pg.pages > 1
           else f"{pg.total}")
    head = f"{rng} {what}" + (f" for {owner}" if owner else "")
    if scope:
        head += f" ({scope})"
    if pg.pages > 1:
        head += f" — page {pg.page} of {pg.pages}"
    return head + ":"


def _past_the_end(pg, head: str) -> str:
    return (f"Page {pg.page} is past the end — {pg.total} match(es) make "
            f"{pg.pages} page(s) of {pg.page_size}.")


def _page_footer(pg, tool: str, f: ListFilter, *,
                 leading_blank: bool = False) -> list[str]:
    if not pg.has_more:
        return []
    nxt = json.dumps(f.args(page=pg.page + 1), ensure_ascii=False)
    out = [""] if leading_blank else []
    out.append(f"More: {tool} {nxt} for the next page, or narrow it with "
               "query / since / until.")
    return out


def _one_id(args: dict) -> int:
    ids = _int_list(args.get("id")) or _int_list(args.get("ids"))
    if len(ids) != 1:
        raise MailboxError("Pass id — one message id.")
    return ids[0]


def _int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str) and value.lstrip().startswith("["):
        # A client that serialised the array: '["1882"]'.
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, (int, str)):
        value = [value]
    out: list[int] = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out
