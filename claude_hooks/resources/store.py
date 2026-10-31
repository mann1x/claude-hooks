"""The claim table and the one place decisions are made.

Every change goes through :meth:`ResourceStore._run`: one transaction,
serialized across every process and host (a Postgres advisory lock, or
``BEGIN IMMEDIATE`` on SQLite), that first **settles** the table — drops
claims whose holder is gone or whose lease or TTL ran out, grants what can
now be granted in queue order (:func:`model.decide`) — then does the
caller's change and settles again. On Postgres a settle that changed
anything sends a ``NOTIFY``, which is what wakes a waiter the moment a
resource is released instead of at its next poll.

Unlike the mailbox, the store owns its connection rather than borrowing
the memory provider's: the waiter needs a dedicated ``LISTEN``
connection anyway, the decision transaction must not interleave with a
recall on the same handle, and a CLI waiter is its own process.

**Liveness.** A claim records who holds it: the session, the host, and a
process — the session's Claude Code process, or the ``run`` wrapper's own
— with its start time, so a reused pid is not mistaken for the holder.

- On the holder's own host the process decides: gone means released
  (``holder_gone``), at the next settle anywhere on that host.
- Another host cannot look at that process, so it trusts the claim's
  **lease**: ``renewed_at + lease_s``. The holder's host renews every live
  local claim from its hooks, its waiters, ``run`` wrappers and the
  daemon's maintenance thread; a host that goes dark has its claims
  dropped when the lease lapses (``lease_lapsed``).
- A TTL (``expires_at``), when given, ends a claim regardless.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from claude_hooks.resources import model, schema
from claude_hooks.resources.model import Block, Claim, Item

log = logging.getLogger("claude_hooks.resources")

#: How long another host trusts a claim without a renewal. Renewals come
#: from hooks, waiters, ``run`` and the daemon (every
#: :data:`MAINTENANCE_INTERVAL_S`), so 30 min only ever lapses for a host
#: that is down or cut off.
DEFAULT_LEASE_S = 1800
MAINTENANCE_INTERVAL_S = 300
#: How often a waiter re-checks with no notification (expiries and dead
#: holders on other hosts produce no NOTIFY until someone settles).
POLL_S = 15.0
SQLITE_POLL_S = 0.5
EVENT_RETENTION_DAYS = 30
ENV_DB = "CLAUDE_HOOKS_RESOURCES_DB"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(text: str) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Owner:
    """Who makes a claim. ``pid``/``pid_started`` is the process whose life
    the claim follows on its own host."""
    session_id: str = ""
    alias: str = ""
    host: str = ""
    pid: Optional[int] = None
    pid_started: Optional[float] = None
    cwd: str = ""


@dataclass
class Outcome:
    claim: Claim
    granted: bool
    reused: bool = False
    block: Optional[Block] = None
    position: int = 0                 # 1 = next in line among waiters

    @property
    def self_blocked(self) -> bool:
        """Waiting only on a claim of the same session that follows the same
        process: it will never be granted until that claim is released. A
        blocker following another process (a detached job) ends on its own."""
        by = self.block.by if self.block else None
        return bool(by is not None and self.claim.session_id
                    and by.session_id == self.claim.session_id
                    and _same_process(by, self.claim.pid,
                                      self.claim.pid_started))


def _same_process(c: Claim, pid: Optional[int],
                  pid_started: Optional[float]) -> bool:
    return c.pid == pid and c.pid_started == pid_started


def backend_from_config(config: Optional[dict] = None) -> tuple[str, str]:
    """``("postgres", dsn)`` when the memory store is pgvector — shared by
    every host on it — else ``("sqlite", path)``: the sqlite_vec store's
    file, or a local one. ``$CLAUDE_HOOKS_RESOURCES_DB`` (a path or a
    ``postgresql://`` DSN) overrides."""
    env = os.environ.get(ENV_DB, "").strip()
    if env:
        if env.startswith(("postgres://", "postgresql://")):
            return "postgres", env
        return "sqlite", env
    if config is None:
        try:
            from claude_hooks.config import load_config
            config = load_config()
        except Exception:
            config = {}
    providers = (config or {}).get("providers") or {}
    pg = providers.get("pgvector") or {}
    if pg.get("enabled") and (pg.get("dsn") or pg.get("mcp_url")):
        return "postgres", pg.get("dsn") or pg.get("mcp_url")
    sv = providers.get("sqlite_vec") or {}
    if sv.get("enabled") and sv.get("db_path"):
        return "sqlite", sv["db_path"]
    return "sqlite", str(Path.home() / ".claude" / "claude-hooks-resources.db")


def host_name() -> str:
    from claude_hooks.mailbox.store import host_name as _h
    return _h()


def _process_alive(pid, started) -> bool:
    from claude_hooks.mailbox.identity import process_alive
    return process_alive(pid, started)


class ResourceStore:
    def __init__(self, dialect: str, target: str, *,
                 host: Optional[str] = None,
                 alive: Callable[[int, float], bool] = _process_alive,
                 clock: Callable[[], float] = time.time):
        if dialect not in ("postgres", "sqlite"):
            raise ValueError(f"unknown dialect {dialect!r}")
        self.dialect = dialect
        self.target = target
        self.host = host or host_name()
        self._alive = alive
        self._clock = clock
        self._conn = None
        self._ready = False
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: Optional[dict] = None, **kw) -> "ResourceStore":
        dialect, target = backend_from_config(config)
        return cls(dialect, target, **kw)

    # ─── plumbing ────────────────────────────────────────────────────

    def _connect(self, *, autocommit: bool = False):
        if self.dialect == "postgres":
            import psycopg
            return psycopg.connect(self.target, autocommit=autocommit,
                                   connect_timeout=10)
        import sqlite3
        from claude_hooks.config import expand_user_path
        path = Path(expand_user_path(self.target))
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: this module issues BEGIN IMMEDIATE itself,
        # which takes the write lock up front — two deciders can then
        # never both read the same table and grant the same resource.
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _run(self, fn):
        """``fn(cur)`` inside the serialized decision transaction, with one
        reconnect if the held connection turns out to be dead."""
        with self._lock:
            for attempt in (0, 1):
                if self._conn is None:
                    self._conn = self._connect()
                conn = self._conn
                began = False
                try:
                    cur = conn.cursor()
                    if self.dialect == "postgres":
                        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                                    (schema.ADVISORY_KEY,))
                    else:
                        cur.execute("BEGIN IMMEDIATE")
                    began = True
                    if not self._ready:
                        for stmt in schema.statements(self.dialect):
                            cur.execute(stmt)
                        for stmt in schema.migrations(
                                self.dialect, self._columns(cur)):
                            cur.execute(stmt)
                    out = fn(cur)
                    if self.dialect == "postgres":
                        conn.commit()
                    else:
                        cur.execute("COMMIT")
                    self._ready = True
                    return out
                except Exception as e:
                    self._rollback(conn, began)
                    if attempt == 0 and self._connection_error(e):
                        log.info("resources: reconnecting after %s", e)
                        self.close()
                        continue
                    raise

    def _rollback(self, conn, began: bool) -> None:
        try:
            if self.dialect == "postgres":
                conn.rollback()
            elif began:
                conn.execute("ROLLBACK")
        except Exception:
            pass

    def _connection_error(self, e: Exception) -> bool:
        name = type(e).__name__
        return name in ("OperationalError", "InterfaceError")

    # ─── rows ────────────────────────────────────────────────────────

    def _columns(self, cur) -> set[str]:
        if self.dialect == "postgres":
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'resource_claims' "
                        "AND table_schema = current_schema()")
            return {r[0] for r in cur.fetchall()}
        cur.execute("PRAGMA table_info(resource_claims)")
        return {r[1] for r in cur.fetchall()}

    def _claims(self, cur,
                columns: Sequence[str] = schema.CLAIM_COLUMNS) -> list[Claim]:
        cur.execute("SELECT " + ", ".join(columns)
                    + " FROM resource_claims ORDER BY seq")
        out = []
        for row in cur.fetchall():
            r = dict(zip(columns, row))
            try:
                items = model.items_from(json.loads(r["items"]))
            except (ValueError, model.SpecError):
                log.warning("resources: unreadable claim %s dropped", r["id"])
                cur.execute(self._q("DELETE FROM resource_claims WHERE id = ?"),
                            (r["id"],))
                continue
            out.append(Claim(
                id=r["id"], seq=int(r["seq"]), items=items, state=r["state"],
                session_id=r["session_id"] or "", alias=r["alias"] or "",
                host=r["host"] or "",
                pid=int(r["pid"]) if r["pid"] is not None else None,
                pid_started=(float(r["pid_started"])
                             if r["pid_started"] is not None else None),
                reason=r["reason"] or "", cwd=r["cwd"] or "",
                created_at=r["created_at"] or "",
                granted_at=r["granted_at"] or "",
                renewed_at=r["renewed_at"] or "",
                expires_at=r["expires_at"] or "",
                lease_s=int(r["lease_s"] or 0),
                ttl_s=int(r.get("ttl_s") or 0)))
        return out

    def _capacity(self, cur) -> dict[str, int]:
        cur.execute("SELECT name, capacity FROM resource_defs "
                    "WHERE capacity IS NOT NULL")
        return {n: int(c) for n, c in cur.fetchall()}

    def _event(self, cur, claim: Optional[Claim], event: str,
               detail: str = "") -> None:
        cur.execute(self._q(
            "INSERT INTO resource_events (at, claim_id, event, items, alias, "
            "host, detail) VALUES (?, ?, ?, ?, ?, ?, ?)"),
            (iso(utcnow()), claim.id if claim else "", event,
             claim.label() if claim else "", claim.alias if claim else "",
             claim.host if claim else "", detail[:500]))

    def _now_iso(self) -> str:
        return iso(datetime.fromtimestamp(self._clock(), timezone.utc))

    # ─── liveness + decisions ────────────────────────────────────────

    def ended(self, c: Claim, now: float) -> Optional[str]:
        """Why claim ``c`` no longer stands, or None."""
        exp = parse_iso(c.expires_at)
        if exp is None and c.ttl_s > 0 and c.state == "granted":
            # Granted by code that predates ``ttl_s`` and set no expiry.
            granted = parse_iso(c.granted_at)
            exp = granted + c.ttl_s if granted is not None else None
        if exp is not None and exp <= now:
            return "expired"
        if c.host == self.host and c.pid:
            if not self._alive(c.pid, c.pid_started):
                return "holder_gone"
            return None
        if c.lease_s > 0:
            renewed = parse_iso(c.renewed_at) or parse_iso(c.created_at)
            if renewed is not None and renewed + c.lease_s <= now:
                return "lease_lapsed"
        return None

    def _settle(self, cur) -> tuple[list[Claim], dict[str, int], bool]:
        """Drop ended claims, grant what can be granted. Returns the live
        claims, the capacities and whether anything changed."""
        now = self._clock()
        claims = self._claims(cur)
        changed = False
        live = []
        for c in claims:
            why = self.ended(c, now)
            if why is None:
                live.append(c)
                continue
            cur.execute(self._q("DELETE FROM resource_claims WHERE id = ?"),
                        (c.id,))
            self._event(cur, c, why, f"was {c.state}")
            changed = True
        capacity = self._capacity(cur)
        granted = [c for c in live if c.state == "granted"]
        waiting = [c for c in live if c.state == "waiting"]
        now_iso = self._now_iso()
        for c in model.decide(granted, waiting, capacity):
            # The TTL bounds the hold, so it starts here, not at the request.
            exp = (iso(datetime.fromtimestamp(now + c.ttl_s, timezone.utc))
                   if c.ttl_s > 0 else c.expires_at)
            cur.execute(self._q(
                "UPDATE resource_claims SET state = 'granted', granted_at = ?, "
                "renewed_at = ?, expires_at = ? WHERE id = ?"),
                (now_iso, now_iso, exp, c.id))
            c.state, c.granted_at, c.renewed_at = "granted", now_iso, now_iso
            c.expires_at = exp
            self._event(cur, c, "granted", c.reason)
            changed = True
        if changed:
            self._notify(cur)
        return live, capacity, changed

    def _notify(self, cur, payload: str = "") -> None:
        if self.dialect == "postgres":
            cur.execute("SELECT pg_notify(%s, %s)", (schema.CHANNEL, payload))

    # ─── public operations ───────────────────────────────────────────

    def request(self, items: Sequence[Item], owner: Owner, *,
                reason: str = "", ttl_s: float = 0,
                lease_s: int = DEFAULT_LEASE_S,
                reuse: bool = True) -> Outcome:
        """Queue a claim and grant it at once if it can be. With ``reuse``,
        a granted claim of the same session that follows the same process
        and already covers ``items`` is returned instead of queueing a
        second one behind it. A claim following another process (a ``run``
        or ``--follow-pid`` job) is never reused: it ends with that
        process, and the new claim would be left holding nothing."""
        items = tuple(items)
        if not items:
            raise model.SpecError("a claim needs at least one resource")

        def go(cur):
            live, capacity, _ = self._settle(cur)
            if reuse and owner.session_id:
                for c in live:
                    if (c.state == "granted" and c.session_id == owner.session_id
                            and c.host == owner.host
                            and _same_process(c, owner.pid, owner.pid_started)
                            and model.covers(c, items)):
                        return Outcome(c, True, reused=True)
            now_iso = self._now_iso()
            # No expiry while queued: the grant sets it from ``ttl_s``.
            cid = uuid.uuid4().hex[:12]
            cur.execute(self._q(
                "INSERT INTO resource_claims (id, items, state, session_id, "
                "alias, host, pid, pid_started, reason, cwd, created_at, "
                "renewed_at, expires_at, lease_s, ttl_s) VALUES "
                "(?, ?, 'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)"),
                (cid, json.dumps([i.as_list() for i in items]),
                 owner.session_id, owner.alias, owner.host or self.host,
                 owner.pid, owner.pid_started, reason[:500], owner.cwd,
                 now_iso, now_iso, int(lease_s),
                 int(ttl_s) if ttl_s and ttl_s > 0 else 0))
            live, capacity, _ = self._settle(cur)
            mine = next((c for c in live if c.id == cid), None)
            if mine is None:
                # Settled away at once: the process it follows is gone.
                raise model.SpecError(
                    "the claim ended as soon as it was made: the process it "
                    "follows is not running")
            if mine.state != "granted":
                self._event(cur, mine, "queued", reason)
                self._notify(cur)
            return self._outcome(mine, live, capacity)

        return self._run(go)

    def _outcome(self, c: Claim, live: list[Claim],
                 capacity: dict[str, int]) -> Outcome:
        if c.state == "granted":
            return Outcome(c, True)
        granted = [x for x in live if x.state == "granted"]
        waiting = [x for x in live if x.state == "waiting"]
        pos = 1 + sum(1 for w in waiting if w.seq < c.seq)
        return Outcome(c, False, block=model.why_waiting(
            c, granted, waiting, capacity), position=pos)

    def get(self, claim_id: str) -> Optional[Outcome]:
        """Settle, then where ``claim_id`` stands; None once it is gone."""
        def go(cur):
            live, capacity, _ = self._settle(cur)
            c = next((x for x in live if x.id == claim_id), None)
            return None if c is None else self._outcome(c, live, capacity)
        return self._run(go)

    def release(self, claim_ids: Iterable[str] = (), *,
                session_id: str = "", resources: Iterable[str] = (),
                force: bool = False, event: str = "released",
                by: str = "") -> tuple[list[Claim], list[Claim]]:
        """Release claims. ``claim_ids`` must belong to ``session_id``
        unless ``force``; with ``resources`` (and no ids) every claim of
        ``session_id`` that names one of them; with neither, every claim
        of ``session_id``. Returns ``(released, refused)``."""
        ids = set(claim_ids or ())
        names = {model.normalize(r) for r in (resources or ())}

        def go(cur):
            live, _, _ = self._settle(cur)
            done, refused = [], []
            for c in live:
                if ids:
                    if c.id not in ids:
                        continue
                    if not force and c.session_id != session_id:
                        refused.append(c)
                        continue
                elif not session_id or c.session_id != session_id:
                    continue
                elif names and not names & {i.resource for i in c.items}:
                    continue
                cur.execute(self._q(
                    "DELETE FROM resource_claims WHERE id = ?"), (c.id,))
                self._event(cur, c, "force_released" if force and
                            c.session_id != session_id else event,
                            by or c.reason)
                done.append(c)
            if done:
                self._notify(cur)
                self._settle(cur)
            return done, refused

        return self._run(go)

    def renew(self, *, session_id: str = "", claim_ids: Iterable[str] = ()
              ) -> int:
        """Renew the leases of this host's live claims — all of them, or
        one session's, or the given ids. Ended ones are dropped by the
        settle that runs first."""
        ids = set(claim_ids or ())

        def go(cur):
            live, _, _ = self._settle(cur)
            now_iso = self._now_iso()
            n = 0
            for c in live:
                if c.host != self.host:
                    continue
                if ids and c.id not in ids:
                    continue
                if session_id and c.session_id != session_id:
                    continue
                cur.execute(self._q("UPDATE resource_claims SET renewed_at = ? "
                                    "WHERE id = ?"), (now_iso, c.id))
                n += 1
            return n

        return self._run(go)

    def extend(self, claim_ids: Iterable[str] = (), *, session_id: str = "",
               resources: Iterable[str] = (), ttl_s: float = 0,
               by_s: float = 0, force: bool = False
               ) -> tuple[list[tuple[Claim, str]], list[Claim]]:
        """Move claims' expiry in place: the claim keeps its id, its grant
        and its place in the queue, which a release + acquire loses (a FIFO
        waiter is granted in between). ``ttl_s`` ends it that long from now,
        ``by_s`` that much after its current end; an extension never
        shortens a claim. A queued claim gets the longer TTL for when it is
        granted. Selects like :meth:`release`. Returns ``([(claim, was)],
        refused)``; waiters are woken (``NOTIFY``) so they see the new end."""
        if ttl_s <= 0 and by_s <= 0:
            raise model.SpecError("extend needs a duration (--ttl or --by)")
        ids = set(claim_ids or ())
        names = {model.normalize(r) for r in (resources or ())}

        def go(cur):
            live, _, _ = self._settle(cur)
            now = self._clock()
            done, refused = [], []
            for c in live:
                if ids:
                    if c.id not in ids:
                        continue
                    if not force and c.session_id != session_id:
                        refused.append(c)
                        continue
                elif not session_id or c.session_id != session_id:
                    continue
                elif names and not names & {i.resource for i in c.items}:
                    continue
                was = c.expires_at
                if c.state != "granted":
                    if c.ttl_s <= 0:
                        continue             # no end once granted either
                    ttl = max(c.ttl_s, int(ttl_s or 0), c.ttl_s + int(by_s or 0))
                    if ttl == c.ttl_s:
                        continue
                    cur.execute(self._q("UPDATE resource_claims SET ttl_s = ? "
                                        "WHERE id = ?"), (ttl, c.id))
                    c.ttl_s = ttl
                    self._event(cur, c, "extended",
                                f"for {ttl // 60} min once granted")
                    done.append((c, was))
                    continue
                current = parse_iso(c.expires_at)
                if current is None:
                    continue                 # no end: nothing to extend
                end = max(current, now + ttl_s if ttl_s > 0 else 0,
                          current + by_s if by_s > 0 else 0)
                if end <= current:
                    continue
                exp = iso(datetime.fromtimestamp(end, timezone.utc))
                granted = parse_iso(c.granted_at) or now
                cur.execute(self._q(
                    "UPDATE resource_claims SET expires_at = ?, ttl_s = ? "
                    "WHERE id = ?"), (exp, int(end - granted), c.id))
                c.expires_at, c.ttl_s = exp, int(end - granted)
                self._event(cur, c, "extended",
                            f"until {exp[:19].replace('T', ' ')} (was "
                            f"{was[:19].replace('T', ' ')})")
                done.append((c, was))
            if done:
                self._notify(cur, "extended")
            return done, refused

        return self._run(go)

    def status(self) -> tuple[list[Outcome], dict[str, int], list[dict]]:
        """Every live claim (granted first, then the queue in order), the
        capacities, and the definitions."""
        def go(cur):
            live, capacity, _ = self._settle(cur)
            cur.execute("SELECT name, capacity, description, updated_at "
                        "FROM resource_defs ORDER BY name")
            defs = [dict(zip(("name", "capacity", "description",
                              "updated_at"), r)) for r in cur.fetchall()]
            outs = [self._outcome(c, live, capacity) for c in live]
            outs.sort(key=lambda o: (not o.granted, o.claim.seq))
            return outs, capacity, defs
        return self._run(go)

    def snapshot(self) -> list[Claim]:
        """The granted claims that still stand, read without the decision
        lock and without settling — for the hot paths (every Bash call's
        PreToolUse note, the quiet check). Never writes."""
        with self._lock:
            for attempt in (0, 1):
                if self._conn is None:
                    self._conn = self._connect()
                conn = self._conn
                try:
                    cur = conn.cursor()
                    if not self._ready:
                        cur.execute("SELECT 1 FROM resource_claims LIMIT 1")
                    try:
                        claims = self._claims(cur)
                    except Exception as e:
                        if "ttl_s" not in str(e):
                            raise
                        # Not migrated yet; this path never writes.
                        self._rollback(conn, False)
                        cur = conn.cursor()
                        claims = self._claims(
                            cur, schema.LEGACY_CLAIM_COLUMNS)
                    if self.dialect == "postgres":
                        conn.rollback()
                    break
                except Exception as e:
                    self._rollback(conn, False)
                    if attempt == 0 and self._connection_error(e):
                        self.close()
                        continue
                    if "resource_claims" in str(e):
                        return []          # no table yet: nothing held
                    raise
        now = self._clock()
        return [c for c in claims
                if c.state == "granted" and self.ended(c, now) is None]

    def define(self, name: str, *, capacity: Optional[int] = None,
               description: str = "") -> None:
        name = model.normalize(name)
        if capacity is not None and capacity < 1:
            raise model.SpecError("capacity must be >= 1")

        def go(cur):
            cur.execute(self._q("DELETE FROM resource_defs WHERE name = ?"),
                        (name,))
            cur.execute(self._q(
                "INSERT INTO resource_defs (name, capacity, description, "
                "updated_at) VALUES (?, ?, ?, ?)"),
                (name, capacity, description, self._now_iso()))
            self._event(cur, None, "defined",
                        f"{name} capacity={capacity} {description}")
            # A raised capacity may let a waiter in.
            self._notify(cur)
            self._settle(cur)
        self._run(go)

    def undefine(self, name: str) -> bool:
        name = model.normalize(name)

        def go(cur):
            cur.execute(self._q("DELETE FROM resource_defs WHERE name = ?"),
                        (name,))
            n = cur.rowcount
            self._settle(cur)
            return bool(n)
        return self._run(go)

    def events(self, limit: int = 50) -> list[dict]:
        def go(cur):
            cur.execute(self._q(
                "SELECT at, claim_id, event, items, alias, host, detail "
                "FROM resource_events ORDER BY seq DESC LIMIT ?"), (limit,))
            return [dict(zip(("at", "claim_id", "event", "items", "alias",
                              "host", "detail"), r)) for r in cur.fetchall()]
        return self._run(go)

    def prune_events(self, days: int = EVENT_RETENTION_DAYS) -> int:
        cutoff = iso(datetime.fromtimestamp(self._clock() - days * 86400,
                                            timezone.utc))

        def go(cur):
            cur.execute(self._q("DELETE FROM resource_events WHERE at < ?"),
                        (cutoff,))
            return cur.rowcount
        return self._run(go)

    # ─── waiting ─────────────────────────────────────────────────────

    def wait(self, claim_id: str, *, timeout_s: Optional[float] = None,
             renew_every_s: float = 60.0,
             on_update: Optional[Callable[[Outcome], None]] = None,
             on_extended: Optional[Callable[[Outcome, str], bool]] = None,
             stop: Optional[threading.Event] = None) -> Optional[Outcome]:
        """Block until ``claim_id`` is granted (returns its outcome), is
        gone (None: released, expired, its holder died) or ``timeout_s``
        passes (returns the waiting outcome). On Postgres the wait is a
        ``LISTEN``: a release anywhere wakes it within one round trip.

        ``on_extended(outcome, was)`` is called when the claim it waits
        behind is extended (its end moved later, ``was`` the old end);
        returning true ends the wait there with the waiting outcome, the
        booking kept — a background waiter's exit is how a session hears.

        The LISTEN is opened before the first check, so a release that
        lands between "still waiting" and "now listening" is not missed.
        """
        deadline = None if timeout_s is None else self._clock() + timeout_s
        listener = None
        if self.dialect == "postgres":
            listener = self._connect(autocommit=True)
            listener.execute(f"LISTEN {schema.CHANNEL}")
        last_renew = self._clock()
        last_seen = None
        ends: dict[str, str] = {}
        try:
            while True:
                out = self.get(claim_id)
                if out is None or out.granted:
                    return out
                by = out.block.by if out.block else None
                if by is not None:
                    was = ends.get(by.id)
                    ends[by.id] = by.expires_at
                    # ``was`` empty: the blocker had no end yet (a queued
                    # claim gets one at its grant), which is no extension.
                    if (was and on_extended is not None
                            and (parse_iso(by.expires_at) or 0)
                            > (parse_iso(was) or 0)
                            and on_extended(out, was)):
                        return out
                key = (out.position, out.block.describe() if out.block else "")
                if on_update is not None and key != last_seen:
                    on_update(out)
                last_seen = key
                now = self._clock()
                if deadline is not None and now >= deadline:
                    return out
                if now - last_renew >= renew_every_s:
                    self.renew(claim_ids=[claim_id])
                    last_renew = now
                pause = POLL_S if listener is not None else SQLITE_POLL_S
                if deadline is not None:
                    pause = max(0.05, min(pause, deadline - now))
                if stop is not None and stop.is_set():
                    return out
                if listener is not None:
                    for _ in listener.notifies(timeout=pause, stop_after=1):
                        pass
                elif stop is not None:
                    stop.wait(pause)
                else:
                    time.sleep(pause)
        finally:
            if listener is not None:
                try:
                    listener.close()
                except Exception:
                    pass
