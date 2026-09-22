"""Mailbox persistence, over whichever backend the host already has.

Deliberately **not** its own connection. On pgvector the provider's
``psycopg`` handle is not thread-safe and is already ``RLock``-guarded,
and recall is fanned out in parallel by ``_parallel.py``; a second
unguarded consumer would race cursor state. So this borrows the
provider's connection and its lock, and every soft-failure path rolls
back — a leaked aborted transaction makes the *next* caller fail with
"transaction is aborted", which surfaces as a recall returning nothing,
i.e. a memory-loss bug that looks like an empty store.

The two dialects differ in three ways and no more: the parameter
placeholder, the returning-id idiom, and the fact that SQLite stores
times as ISO-8601 UTC text. Text timestamps sort correctly, which is
what lets one set of queries serve both.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from claude_hooks.mailbox import schema
from claude_hooks.mailbox.addressing import (
    Address, AddressError, Session, parse_address, resolve,
)

log = logging.getLogger("claude_hooks.mailbox")

DEFAULT_EXPIRY_DAYS = 180
DEFAULT_REGISTRY_DAYS = 30

#: How long since ``last_seen`` a registration still counts as a live
#: session. ``touch()`` runs once per turn, so an *open but idle* session
#: goes untouched for as long as its user is away — which is why nothing
#: may be lost by falling outside this window: a stale row is ignored for
#: addressing, and :meth:`touch` re-creates one that has been evicted.
DEFAULT_LIVE_HOURS = 12

#: Grace before a stale registration is physically deleted. Deliberately
#: longer than the window above, so a row stops being *used* before it
#: stops being *readable* — the ten dead rows behind the eleven-fold
#: delivery were the only evidence of what had happened.
DEFAULT_EVICT_HOURS = 24


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def host_name() -> str:
    return (os.environ.get("CLAUDE_HOOKS_HOST")
            or socket.gethostname().split(".")[0].lower())


def os_name() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")


def _chunks(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


@contextmanager
def _cursor(conn):
    """A cursor that closes, on either driver.

    psycopg cursors are context managers; ``sqlite3`` cursors are not —
    ``with conn.cursor()`` raises ``TypeError`` there. Wrapping keeps
    every query in this module written once for both.
    """
    cur = conn.cursor()
    try:
        yield cur
    finally:
        try:
            cur.close()
        except Exception:      # pragma: no cover — driver-dependent
            pass


class MailboxError(RuntimeError):
    pass


class MailboxStore:
    """Mailbox operations against a borrowed connection.

    ``connect`` returns a live DB-API connection and is called on every
    operation, so the provider's own dead-handle detection and reconnect
    logic applies here too. That matters: a Postgres restart used to
    brick long-lived processes permanently, and a mailbox that answered
    "no messages" through an outage would be the same bug with a new
    face.
    """

    def __init__(self, connect, lock, *, dialect: str = "postgres",
                 expiry_days: int = DEFAULT_EXPIRY_DAYS):
        self._connect = connect
        self._lock = lock
        self.dialect = dialect
        self._expiry_days = expiry_days
        self._ready = False

    # ─── plumbing ────────────────────────────────────────────────────

    @property
    def _ph(self) -> str:
        return "%s" if self.dialect == "postgres" else "?"

    def _q(self, sql: str) -> str:
        """Rewrite the portable ``?`` placeholder for the dialect."""
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _now(self):
        """Postgres takes datetimes; SQLite takes ISO text."""
        now = utcnow()
        return now if self.dialect == "postgres" else iso(now)

    def _at(self, dt: datetime):
        return dt if self.dialect == "postgres" else iso(dt)

    def ensure_schema(self) -> None:
        if self._ready:
            return
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    for stmt in schema.statements(self.dialect):
                        cur.execute(stmt)
                conn.commit()
                self._ready = True
            except Exception:
                self._rollback(conn)
                raise

    @staticmethod
    def _rollback(conn) -> None:
        try:
            conn.rollback()
        except Exception:      # pragma: no cover — already broken
            log.debug("mailbox rollback failed", exc_info=True)

    def _rows(self, cur, columns: Sequence[str]) -> list[dict]:
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    # ─── registry ────────────────────────────────────────────────────

    def _claim_alias_host(self, cur, alias: str, host: str,
                          session_id: str) -> int:
        """Make ``(alias, host)`` this session's, evicting any other.

        A restarted or upgraded client comes back with a new
        ``session_id``, and ``session_id`` is the primary key — so the
        old row survived and the alias accumulated one registration per
        restart. ``xollama@solidpc`` had five.

        Evicting is right because ``(alias, host)`` is the unit the rest
        of the mailbox already addresses: ``send()`` collapses recipients
        to distinct ``(alias, host)`` pairs, so a second row was never a
        second addressee — only a second *claim* about who is alive
        there, and the older claim is the false one.

        The consequence to know about: two genuinely concurrent sessions
        in the same cwd on the same host now take turns owning the row,
        each reclaiming it on its next action. Their mail is unaffected —
        an inbox is read by alias, not by registration — but
        ``mailbox-sessions`` shows one of them, and a message addressed
        to the evicted ``session_id`` has nowhere to resolve.
        """
        cur.execute(self._q(
            "DELETE FROM session_registry "
            "WHERE alias = ? AND host = ? AND session_id <> ?"),
            (alias, host, session_id))
        return cur.rowcount or 0

    def register(self, session_id: str, alias: str, *, cwd: str = "",
                 host: Optional[str] = None) -> list[Session]:
        """Record this session and return the *other* live sessions that
        share its alias.

        Returning the collisions is what lets ``SessionStart`` say so
        once, in the only place a sender can later be surprised by it.
        """
        self.ensure_schema()
        h = host or host_name()
        now = self._now()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "DELETE FROM session_registry WHERE session_id = ?"),
                        (session_id,))
                    evicted = self._claim_alias_host(cur, alias, h,
                                                     session_id)
                    cur.execute(self._q(
                        "INSERT INTO session_registry "
                        "(session_id, alias, host, os, cwd, started_at, "
                        " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                        (session_id, alias, h, os_name(), cwd, now, now))
                    # Same-host duplicates are gone by construction now,
                    # so this only ever returns *other hosts* — which is
                    # the collision actually worth warning about, since
                    # ``xollama@solidpc`` and ``xollama@pandorum`` really
                    # are two different correspondents.
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.SESSION_COLUMNS) +
                        " FROM session_registry WHERE alias = ? "
                        "AND session_id <> ?"), (alias, session_id))
                    others = self._rows(cur, schema.SESSION_COLUMNS)
                if evicted:
                    log.info("mailbox: %s@%s reclaimed from %d stale "
                             "registration(s)", alias, h, evicted)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return [_as_session(r) for r in others]

    def registered_alias(self, session_id: str) -> Optional[str]:
        """The alias this session registered under, if it has one.

        The default alias is the *current directory's* name, recomputed
        from the event on every call — so a session that changed
        directory changed its name, registered under the new one, and
        left the old registration behind. Two rows, one session, and
        everything addressed to the name it started with parks on an
        alias nobody is listening to.

        A session's identity is decided once, when it registers, and
        then remembered. Directory is a property of the session, not the
        key to it. The derived name is only a *default* for a session
        that has no registration yet, which also repairs the MCP-side
        binding, where there is no event to take a cwd from and the
        server process's own cwd was standing in for one.

        The cost of remembering is that renaming a live session — via
        ``.claude-hooks/mailbox.toml`` — takes effect at its next
        SessionStart rather than its next turn. That is the right way
        round: a rename that took effect mid-session would strand
        everything already addressed to the old name.
        """
        if not session_id:
            return None
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT alias FROM session_registry "
                        "WHERE session_id = ?"), (session_id,))
                    row = cur.fetchone()
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return row[0] if row else None

    def forget(self, session_id: str) -> bool:
        """Drop this session's registration. Called at SessionEnd.

        Without it a registration lived until the 30-day sweep, so every
        session that had *ever* run in a directory stayed listed under
        its alias. Ten short sessions in ten minutes left ten dead rows
        beside the live one, which is how ``xollama@solidpc`` came to
        have eleven registrations — visible in ``mailbox-sessions``, and
        counted as ten peers by the SessionStart collision warning.

        A session that ends and is later resumed re-registers at
        SessionStart, so forgetting here loses nothing. A session that
        dies without SessionEnd still falls to the sweep.
        """
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "DELETE FROM session_registry WHERE session_id = ?"),
                        (session_id,))
                    gone = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return gone > 0

    def touch(self, session_id: str, *, alias: Optional[str] = None,
              host: Optional[str] = None, cwd: str = "") -> None:
        """Refresh ``last_seen``, re-registering if the row is gone.

        Called once per turn off the hook path. The re-registration is
        what makes eviction safe: a session open long enough to fall
        outside the live window is not dead, it is quiet, and its next
        turn must put it back rather than leave it unaddressable for the
        rest of its life. Without ``alias`` there is nothing to rebuild
        from, so the refresh is best-effort as before.
        """
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "UPDATE session_registry SET last_seen = ? "
                        "WHERE session_id = ?"), (self._now(), session_id))
                    missing = (cur.rowcount or 0) == 0
                    if missing and alias:
                        now = self._now()
                        h = host or host_name()
                        # Claim first. Re-inserting blind would violate
                        # the one-row-per-(alias, host) index the moment
                        # anything else holds the slot — and this path
                        # exists precisely for the case where something
                        # does: the row was evicted, by the sweep or by a
                        # newer session, while this one was quiet.
                        self._claim_alias_host(cur, alias, h, session_id)
                        cur.execute(self._q(
                            "INSERT INTO session_registry "
                            "(session_id, alias, host, os, cwd, started_at, "
                            " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                            (session_id, alias, h, os_name(), cwd, now, now))
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise

    def sessions(self, *, alias: Optional[str] = None,
                 os_filter: Optional[str] = None,
                 include_stale: bool = False,
                 live_hours: Optional[float] = None) -> list[Session]:
        """Live registrations, newest-seen first within an alias.

        Stale rows are excluded by default, because a registration is
        evidence that a session *was* running and addressing needs to
        know which ones still are. Until this filter existed, an alias
        accumulated a row per session that had ever run in its directory
        — `xollama@solidpc` reached eleven — and every one of them was
        treated as a live addressee.

        Pass ``include_stale=True`` to see everything, which is what an
        operator listing the registry wants; delivery never does.
        """
        self.ensure_schema()
        sql = ("SELECT " + ", ".join(schema.SESSION_COLUMNS) +
               " FROM session_registry")
        where, params = [], []
        if alias:
            where.append("alias = ?")
            params.append(alias)
        if os_filter:
            where.append("os = ?")
            params.append(os_filter)
        if not include_stale:
            hours = (DEFAULT_LIVE_HOURS if live_hours is None else live_hours)
            where.append("last_seen >= ?")
            params.append(self._at(utcnow() - timedelta(hours=hours)))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY alias, host"
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(sql), tuple(params))
                    rows = self._rows(cur, schema.SESSION_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return [_as_session(r) for r in rows]

    def sweep_registry(self, *, days: int = DEFAULT_REGISTRY_DAYS) -> int:
        """Forget sessions nobody has seen for ``days``.

        Messages already addressed to them are untouched and keep their
        own expiry, so forgetting a session never loses its mail.
        """
        self.ensure_schema()
        cutoff = self._at(utcnow() - timedelta(days=days))
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "DELETE FROM session_registry WHERE last_seen < ?"),
                        (cutoff,))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return n

    def evict_stale(self, *, hours: float = DEFAULT_EVICT_HOURS) -> int:
        """Physically remove registrations nobody has touched in ``hours``.

        The counterpart of :meth:`sweep_registry`, which keeps a 30-day
        horizon for the archive pass. Thirty days is the wrong scale for
        *addressing*: ten sessions that ran and ended inside ten minutes
        left ten rows that a send then fanned out over, and they would
        have sat there for a month.

        Nothing is lost. Messages already addressed to a forgotten
        session keep their own expiry, an alias's mail is addressed to
        the alias rather than to a row, and a session still running
        re-creates its registration on the next :meth:`touch`.
        """
        self.ensure_schema()
        cutoff = self._at(utcnow() - timedelta(hours=hours))
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "DELETE FROM session_registry WHERE last_seen < ?"),
                        (cutoff,))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        if n:
            log.info("mailbox: evicted %d stale registration(s)", n)
        return n

    def dedupe_messages(self) -> dict:
        """Collapse the duplicate rows the old fan-out left behind.

        A repair, not a routine: ``send()`` can no longer produce these.
        It exists because the rows it removes are already in every
        mailbox that ran the old code — 30 of 65 on solidpc, three sends
        of eleven copies each — and an upgrade that fixes the cause
        without clearing the effect leaves the recipient re-reading the
        same message eleven times.

        Two rows are the same message when the sender, destination,
        subject, body **and** ``created_at`` all match. The timestamp
        carries microseconds on both dialects, so two deliberate sends of
        identical text cannot collide; only rows written by one
        ``INSERT`` loop can.

        Which copy survives is the whole difficulty. Read state lives on
        the row, and the copies do not share it: on solidpc one set had
        one read copy in eleven, so keeping the lowest id had a ~91%
        chance of resurfacing a message the recipient had already read.
        A read copy wins, then an acked one, then the original.

        A ``broadcast_group`` is cleared only when the group is left with
        a single row. A genuine broadcast fans out across *hosts*, whose
        rows differ in ``to_host`` and so are never duplicates of each
        other — but a broadcast sent while the fan-out bug was live has
        both kinds of multiplicity at once, and only the second is
        spurious.
        """
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT id, created_at, from_alias, to_alias, "
                        "to_host, subject, body, read_at, ack_body, "
                        "broadcast_group FROM session_messages ORDER BY id"))
                    rows = cur.fetchall()

                groups: dict = {}
                for r in rows:
                    key = (str(r[1]), r[2], r[3], r[4], r[5], r[6])
                    groups.setdefault(key, []).append(r)

                doomed: list = []
                touched_groups: set = set()
                for members in groups.values():
                    if len(members) < 2:
                        continue
                    # read first, then acked, then the original row
                    members = sorted(
                        members,
                        key=lambda m: (m[7] is None, m[8] is None, m[0]))
                    for m in members[1:]:
                        doomed.append(m[0])
                        if m[9]:
                            touched_groups.add(m[9])
                    if members[0][9]:
                        touched_groups.add(members[0][9])

                if not doomed:
                    return {"removed": 0, "kept": 0, "groups_cleared": 0}

                with _cursor(conn) as cur:
                    for chunk in _chunks(doomed, 500):
                        # Portable ``?`` — ``_q`` rewrites it per dialect.
                        ph = ", ".join(["?"] * len(chunk))
                        cur.execute(self._q(
                            f"DELETE FROM session_messages "
                            f"WHERE id IN ({ph})"), tuple(chunk))
                    cleared = 0
                    for grp in sorted(touched_groups):
                        cur.execute(self._q(
                            "SELECT id FROM session_messages "
                            "WHERE broadcast_group = ?"), (grp,))
                        left = [row[0] for row in cur.fetchall()]
                        if len(left) == 1:
                            # One destination is not a broadcast; the
                            # group id was an artefact of counting
                            # registrations rather than mailboxes.
                            cur.execute(self._q(
                                "UPDATE session_messages "
                                "SET broadcast_group = NULL WHERE id = ?"),
                                (left[0],))
                            cleared += 1
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        log.info("mailbox: removed %d duplicate row(s), cleared %d "
                 "broadcast group(s)", len(doomed), cleared)
        return {"removed": len(doomed),
                "kept": sum(1 for m in groups.values() if len(m) > 1),
                "groups_cleared": cleared}

    # ─── sending ─────────────────────────────────────────────────────

    def send(self, to: str, subject: str, body: str, *,
             from_alias: str, from_session: Optional[str] = None,
             priority: int = 0,
             expires_days: Optional[int] = None) -> dict:
        """Resolve, then insert one row per recipient.

        Resolution happens *before* anything is written, so an ambiguous
        address leaves no partial delivery behind.
        """
        if not subject or not subject.strip():
            raise MailboxError("subject is required — it is the whole of "
                               "what the recipient sees before reading.")
        if not body or not body.strip():
            raise MailboxError("body is required.")
        address = parse_address(to)
        recipients = resolve(address, self.sessions())

        # One row per DESTINATION, never per registration.
        #
        # The row that gets written carries ``to_alias`` and ``to_host``
        # and nothing that distinguishes one session from another, so N
        # registrations of one alias on one host produced N rows that
        # were byte-identical apart from their id — and ``inbox()``
        # reads by alias, so the recipient saw the same message N times.
        # Observed live: ``xollama@solidpc`` had 11 registrations (one
        # live session plus ten from sessions that had ended minutes
        # apart), and a single send was delivered eleven times.
        #
        # A registration is not an addressee. The mailbox belongs to the
        # alias — which is also why parking mail on an alias nobody has
        # registered works at all — so the destination set is the
        # distinct ``(alias, host)`` pairs, and a broadcast is a message
        # reaching more than one of *those*, not more than one process.
        destinations: list[tuple[str, Optional[str]]] = []
        for r in recipients:
            key = (r.alias, r.host)
            if key not in destinations:
                destinations.append(key)

        group = str(uuid.uuid4()) if len(destinations) > 1 else None
        expires = self._at(utcnow() + timedelta(
            days=expires_days if expires_days is not None
            else self._expiry_days))
        now = self._now()
        host = host_name()

        rows: list[tuple] = []
        if address.is_session:
            rows.append((now, from_alias, from_session, host, None,
                         address.session_id, None, None, subject, body,
                         int(priority), expires))
        elif destinations:
            for to_alias, to_host in destinations:
                rows.append((now, from_alias, from_session, host, to_alias,
                             None, to_host, group, subject, body,
                             int(priority), expires))
        else:
            # Nobody registered: park it on the alias. This is the point
            # of a mailbox — the session you have something for is
            # usually the one that is closed.
            rows.append((now, from_alias, from_session, host, address.alias,
                         None, address.host, None, subject, body,
                         int(priority), expires))

        sql = self._q(
            "INSERT INTO session_messages "
            "(created_at, from_alias, from_session, from_host, to_alias, "
            " to_session, to_host, broadcast_group, subject, body, "
            " priority, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        ids: list[int] = []
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    for row in rows:
                        if self.dialect == "postgres":
                            cur.execute(sql + " RETURNING id", row)
                            ids.append(cur.fetchone()[0])
                        else:
                            cur.execute(sql, row)
                            ids.append(cur.lastrowid)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return {"ids": ids, "address": address, "recipients": recipients,
                "destinations": destinations, "broadcast_group": group}

    # ─── reading ─────────────────────────────────────────────────────

    def inbox(self, *, alias: str, session_id: Optional[str] = None,
              host: Optional[str] = None, include_read: bool = False,
              since: Optional[datetime] = None) -> list[dict]:
        """Messages for this session.

        A message addressed to ``alias@host`` is only for that host;
        one addressed to a bare alias is for whoever picks it up. Both
        are matched here so a session sees its mail regardless of which
        form the sender used.
        """
        self.ensure_schema()
        h = host or host_name()
        sql = ("SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
               " FROM session_messages WHERE cancelled_at IS NULL AND (")
        clauses = ["(to_alias = ? AND (to_host IS NULL OR to_host = ?))"]
        params: list[Any] = [alias, h]
        if session_id:
            clauses.append("to_session = ?")
            params.append(session_id)
        sql += " OR ".join(clauses) + ")"
        if not include_read:
            sql += " AND read_at IS NULL"
        if since is not None:
            sql += " AND created_at > ?"
            params.append(self._at(since))
        sql += " ORDER BY priority DESC, created_at ASC"
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(sql), tuple(params))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return rows

    def read(self, ids: Sequence[int], *, reader_session: str,
             alias: str, host: Optional[str] = None) -> list[dict]:
        """Return full bodies and stamp ``read_at``.

        Only stamps rows that were actually addressed to this reader, so
        reading someone else's id neither leaks it nor marks it read.
        """
        if not ids:
            return []
        self.ensure_schema()
        h = host or host_name()
        marks = ", ".join("?" for _ in ids)
        sql = self._q(
            "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
            f" FROM session_messages WHERE id IN ({marks}) "
            "AND cancelled_at IS NULL AND ("
            "(to_alias = ? AND (to_host IS NULL OR to_host = ?)) "
            "OR to_session = ?)")
        params = list(ids) + [alias, h, reader_session]
        now = self._now()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(sql, tuple(params))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                    fresh = [r["id"] for r in rows if not r["read_at"]]
                    if fresh:
                        m = ", ".join("?" for _ in fresh)
                        cur.execute(self._q(
                            "UPDATE session_messages SET read_at = ?, "
                            f"read_by = ? WHERE id IN ({m})"),
                            tuple([now, reader_session] + fresh))
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        for r in rows:
            if not r["read_at"]:
                r["read_at"], r["read_by"] = now, reader_session
        return rows

    # ─── sender-side ─────────────────────────────────────────────────
    #
    # Every query here is scoped by ``from_alias`` **and** ``from_host``,
    # because the alias alone is not an identity. The default alias is
    # the project directory name, so two hosts checking out the same
    # repo are both ``claude-hooks`` — which the addressing layer
    # already knows, since that is exactly when it refuses a bare alias
    # and demands ``claude-hooks@solidpc``.
    #
    # Scoping on the alias alone gave one host authority over another's
    # mail. The expensive case was ``mark_receipts_seen``: pandorum
    # calling ``mailbox-sent`` marked solidpc's unseen receipts as seen,
    # so solidpc's announcement — the entire point of the receipt — never
    # fired, and nothing anywhere recorded that it had been swallowed.
    # ``edit`` and ``cancel`` were the same hole pointed at a message
    # body, with ``_own_message`` approving the rewrite.

    def sent(self, *, from_alias: str, from_host: Optional[str] = None,
             limit: int = 50) -> list[dict]:
        self.ensure_schema()
        host = from_host if from_host is not None else host_name()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
                        " FROM session_messages WHERE from_alias = ? "
                        "AND from_host = ? "
                        "ORDER BY created_at DESC LIMIT ?"),
                        (from_alias, host, int(limit)))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return rows

    def pending_receipts(self, *, from_alias: str,
                         from_host: Optional[str] = None) -> list[dict]:
        """Acks the sender has not seen yet — the announcement gate.

        A read with no ack is deliberately invisible: a bare "your
        message was read" is a notification about something that needs
        no action, which is why plain acknowledgement was dropped.
        """
        self.ensure_schema()
        host = from_host if from_host is not None else host_name()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
                        " FROM session_messages WHERE from_alias = ? "
                        "AND from_host = ? "
                        "AND ack_body IS NOT NULL AND receipt_read_at IS NULL "
                        "ORDER BY ack_at ASC"), (from_alias, host))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return rows

    def mark_receipts_seen(self, ids: Sequence[int], *,
                           from_alias: str,
                           from_host: Optional[str] = None) -> int:
        if not ids:
            return 0
        self.ensure_schema()
        host = from_host if from_host is not None else host_name()
        marks = ", ".join("?" for _ in ids)
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "UPDATE session_messages SET receipt_read_at = ? "
                        f"WHERE id IN ({marks}) AND from_alias = ? "
                        "AND from_host = ? "
                        "AND receipt_read_at IS NULL"),
                        tuple([self._now()] + list(ids)
                              + [from_alias, host]))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return n

    def edit(self, message_id: int, *, from_alias: str,
             from_host: Optional[str] = None,
             subject: Optional[str] = None, body: Optional[str] = None,
             priority: Optional[int] = None) -> dict:
        """Change an unread message you sent.

        Refused once read, and the refusal names the reader and the
        time — which is the failure this whole design exists to prevent,
        except now it is an error rather than a document changing
        silently under someone's reply.
        """
        host = from_host if from_host is not None else host_name()
        row = self._own_message(message_id, from_alias, host)
        self._require_editable(row, message_id, verb="edited")
        sets, params = [], []
        if subject is not None:
            sets.append("subject = ?")
            params.append(subject)
        if body is not None:
            sets.append("body = ?")
            params.append(body)
        if priority is not None:
            sets.append("priority = ?")
            params.append(int(priority))
        if not sets:
            raise MailboxError("nothing to change — pass subject, body "
                               "and/or priority.")
        sets.append("edited_at = ?")
        params.append(self._now())

        # A broadcast is one message; editing it means editing every
        # copy that has not been read, and saying how many were skipped.
        group = row["broadcast_group"]
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    if group:
                        cur.execute(self._q(
                            "UPDATE session_messages SET " + ", ".join(sets) +
                            " WHERE broadcast_group = ? AND from_alias = ? "
                            "AND from_host = ? "
                            "AND read_at IS NULL AND cancelled_at IS NULL"),
                            tuple(params + [group, from_alias, host]))
                    else:
                        cur.execute(self._q(
                            "UPDATE session_messages SET " + ", ".join(sets) +
                            " WHERE id = ? AND from_alias = ? "
                            "AND from_host = ? "
                            "AND read_at IS NULL AND cancelled_at IS NULL"),
                            tuple(params + [message_id, from_alias, host]))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return {"updated": n, "broadcast_group": group}

    def cancel(self, message_id: int, *, from_alias: str,
               from_host: Optional[str] = None) -> dict:
        host = from_host if from_host is not None else host_name()
        row = self._own_message(message_id, from_alias, host)
        self._require_editable(row, message_id, verb="withdrawn")
        group = row["broadcast_group"]
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    if group:
                        cur.execute(self._q(
                            "UPDATE session_messages SET cancelled_at = ? "
                            "WHERE broadcast_group = ? AND from_alias = ? "
                            "AND from_host = ? AND read_at IS NULL"),
                            (self._now(), group, from_alias, host))
                    else:
                        cur.execute(self._q(
                            "UPDATE session_messages SET cancelled_at = ? "
                            "WHERE id = ? AND from_alias = ? "
                            "AND from_host = ? AND read_at IS NULL"),
                            (self._now(), message_id, from_alias, host))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return {"cancelled": n, "broadcast_group": group}

    # ─── recipient-side ──────────────────────────────────────────────

    def ack(self, message_id: int, note: str, *, session_id: str,
            alias: str, host: Optional[str] = None) -> dict:
        """Attach or replace the short note the sender will see.

        Not tied to the moment of reading: a session can read, start
        work, discover it is a day of work, and say so then. Frozen once
        the sender has seen it — same ownership rule as a message, in
        the other direction.
        """
        if not note or not note.strip():
            raise MailboxError("the ack note is the whole payload — "
                               "an empty one would announce nothing.")
        self.ensure_schema()
        h = host or host_name()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
                        " FROM session_messages WHERE id = ? AND ("
                        "(to_alias = ? AND (to_host IS NULL OR to_host = ?)) "
                        "OR to_session = ?)"),
                        (message_id, alias, h, session_id))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                    if not rows:
                        raise MailboxError(
                            f"Message {message_id} is not addressed to you, "
                            f"or does not exist.")
                    row = rows[0]
                    if not row["read_at"]:
                        raise MailboxError(
                            f"Message {message_id} has not been read yet. "
                            f"Read it first — an ack on an unread message "
                            f"would be a receipt for something nobody "
                            f"received.")
                    if row["receipt_read_at"]:
                        raise MailboxError(
                            f"The sender already read your note (at "
                            f"{row['receipt_read_at']}), so it is frozen. "
                            f"Send a new message with mailbox-send.")
                    now = self._now()
                    if row["ack_body"]:
                        cur.execute(self._q(
                            "UPDATE session_messages SET ack_body = ?, "
                            "ack_edited_at = ? WHERE id = ?"),
                            (note, now, message_id))
                        action = "replaced"
                    else:
                        cur.execute(self._q(
                            "UPDATE session_messages SET ack_body = ?, "
                            "ack_at = ? WHERE id = ?"),
                            (note, now, message_id))
                        action = "attached"
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return {"action": action, "id": message_id}

    # ─── expiry ──────────────────────────────────────────────────────

    def expired(self, *, limit: int = 1000) -> list[dict]:
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
                        " FROM session_messages WHERE expires_at < ? "
                        "ORDER BY expires_at ASC LIMIT ?"),
                        (self._now(), int(limit)))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return rows

    def delete(self, ids: Sequence[int]) -> int:
        """Remove rows. Called *only* after the archive write returns —
        the same write-the-durable-copy-first ordering the distillation
        reaper uses, for the same reason."""
        if not ids:
            return 0
        self.ensure_schema()
        marks = ", ".join("?" for _ in ids)
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        f"DELETE FROM session_messages WHERE id IN ({marks})"),
                        tuple(ids))
                    n = cur.rowcount or 0
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return n

    # ─── internals ───────────────────────────────────────────────────

    def _require_editable(self, row: dict, message_id: int, *,
                          verb: str) -> None:
        """A message is the sender's until it is read.

        A **broadcast is one message**, so being read on one host does
        not freeze the copies nobody has opened — refusing there would
        mean a two-host broadcast becomes uncorrectable the moment the
        faster host looks at it, which is most of the time. Only when
        every copy has been read is there nothing left to change.
        """
        group = row["broadcast_group"]
        if group:
            if self._group_has_unread(group, row["from_alias"],
                                      row["from_host"]):
                return
            raise MailboxError(
                f"Every copy of broadcast {group} has been read, so it "
                f"can no longer be {verb} — send a correction with "
                f"mailbox-send instead.")
        if row["read_at"]:
            raise MailboxError(
                f"Message {message_id} was read by "
                f"{row['read_by'] or 'the recipient'} at {row['read_at']}. "
                f"It can no longer be {verb} — send a correction with "
                f"mailbox-send instead.")

    def _group_has_unread(self, group: str, from_alias: str,
                          from_host: Optional[str] = None) -> bool:
        host = from_host if from_host is not None else host_name()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT COUNT(*) FROM session_messages "
                        "WHERE broadcast_group = ? AND from_alias = ? "
                        "AND from_host = ? "
                        "AND read_at IS NULL AND cancelled_at IS NULL"),
                        (group, from_alias, host))
                    n = cur.fetchone()[0]
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return bool(n)

    def _own_message(self, message_id: int, from_alias: str,
                     from_host: Optional[str] = None) -> dict:
        self.ensure_schema()
        host = from_host if from_host is not None else host_name()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.MESSAGE_COLUMNS) +
                        " FROM session_messages WHERE id = ?"), (message_id,))
                    rows = self._rows(cur, schema.MESSAGE_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        if not rows:
            raise MailboxError(f"No message with id {message_id}.")
        row = rows[0]
        if row["from_alias"] != from_alias:
            raise MailboxError(
                f"Message {message_id} was sent by {row['from_alias']!r}, "
                f"not you — one session cannot rewrite another's message.")
        if (row["from_host"] or "") != host:
            # Same alias, different host. Worth its own message: with the
            # default alias being the directory name, this is the *likely*
            # collision, and "sent by 'claude-hooks', not you" would be
            # baffling to a session that is also called claude-hooks.
            raise MailboxError(
                f"Message {message_id} was sent by "
                f"{row['from_alias']}@{row['from_host'] or '?'}, and you are "
                f"{from_alias}@{host} — the alias is shared but the session "
                f"is not. One host cannot rewrite another's message.")
        if row["cancelled_at"]:
            raise MailboxError(f"Message {message_id} was already withdrawn.")
        return row


def _as_session(row: dict) -> Session:
    return Session(session_id=row["session_id"], alias=row["alias"],
                   host=row["host"], os=row.get("os") or "",
                   cwd=row.get("cwd") or "",
                   last_seen=str(row.get("last_seen") or ""))
