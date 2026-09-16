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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def host_name() -> str:
    return (os.environ.get("CLAUDE_HOOKS_HOST")
            or socket.gethostname().split(".")[0].lower())


def os_name() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")


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
                    cur.execute(self._q(
                        "INSERT INTO session_registry "
                        "(session_id, alias, host, os, cwd, started_at, "
                        " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                        (session_id, alias, h, os_name(), cwd, now, now))
                    cur.execute(self._q(
                        "SELECT " + ", ".join(schema.SESSION_COLUMNS) +
                        " FROM session_registry WHERE alias = ? "
                        "AND session_id <> ?"), (alias, session_id))
                    others = self._rows(cur, schema.SESSION_COLUMNS)
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise
        return [_as_session(r) for r in others]

    def touch(self, session_id: str) -> None:
        """Refresh ``last_seen``. Called off the hook path."""
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    cur.execute(self._q(
                        "UPDATE session_registry SET last_seen = ? "
                        "WHERE session_id = ?"), (self._now(), session_id))
                conn.commit()
            except Exception:
                self._rollback(conn)
                raise

    def sessions(self, *, alias: Optional[str] = None,
                 os_filter: Optional[str] = None) -> list[Session]:
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

        group = str(uuid.uuid4()) if len(recipients) > 1 else None
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
        elif recipients:
            for r in recipients:
                rows.append((now, from_alias, from_session, host, r.alias,
                             None, r.host, group, subject, body,
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
                "broadcast_group": group}

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
