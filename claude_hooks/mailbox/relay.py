"""Mailbox relay for cloud sessions.

A Claude cloud session cannot reach the pgvector MCP, but when it runs in
the desktop app it can read and write a local folder. The relay turns that
folder into a mailbox client: the session writes requests as files, the
daemon executes them through :class:`~claude_hooks.mailbox.tools.MailboxTools`
— the same dispatch the MCP tools use, so there is one implementation of
every rule — and writes the answers back as files.

The protocol the session follows is ``claude_hooks/mailbox/cloud/MAILBOX.md``
(installed into the folder as ``MAILBOX.md``). In short:

* A session takes an alias with a ``mailbox-alias`` request and is then
  ``<alias>@cloud`` to everyone else. That is the only address it needs;
  the registry key behind it (``cloud-<alias>``) is never used to address
  it.
* **Nothing a remote session writes is read until its semaphore says so.**
  Every request is a pair, ``requests/<id>.json`` + ``requests/<id>.sem``.
  The session writes the semaphore as ``writing``, then the payload, then
  the semaphore as ``ready``. The daemon ignores a payload with no
  semaphore, a semaphore that is not valid JSON, and anything not
  ``ready``; when ``bytes`` is given it must match the payload's size on
  disk. Only then is the payload read, and the payload and semaphore are
  deleted before the request runs. The same discipline runs the other
  way: every file the daemon writes gets its semaphore last.

Cost: while idle the relay thread is blocked in the kernel waiting for a
file event (see :mod:`claude_hooks.mailbox.watch`) — no timer, no disk
access, no database connection. The first request after a quiet spell
is handled within about a second; requests right behind one that was
handled are batched into at most one pass per ``interval_seconds``
(30 s, the floor). The inbox
summary needs the database, because new mail arrives there and not in
the folder; that check runs every interval only while some cloud session
has been active within the live window, and never otherwise.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from claude_hooks.mailbox.watch import RESCAN, make_watcher

log = logging.getLogger("claude_hooks.mailbox.relay")

WRITING, READY, CANCELLED = "writing", "ready", "cancelled"
STATUSES = (WRITING, READY, CANCELLED)

#: The one operation that is not a mailbox tool: take an alias.
ALIAS_OP = "mailbox-alias"

INSTRUCTIONS_NAME = "MAILBOX.md"
DEFAULT_HOST = "cloud"

#: The floor, and the default. Asked for by the operator: nothing here
#: is urgent enough to be worth waking the host more often.
MIN_INTERVAL_SECONDS = 30.0
#: A request still ``writing`` (or a semaphore still unparseable, or a
#: ``ready`` whose payload never matches) this long after its semaphore
#: last changed is moved to ``rejected/``.
DEFAULT_WRITING_TIMEOUT_SECONDS = 3600.0
#: A cloud session counts as live — its inbox summary kept current —
#: this long after its last request. Matches the registry's live window.
DEFAULT_LIVE_HOURS = 12.0
#: Session folders idle this long are moved to ``archive/``. Matches the
#: registry's own retention, so the folder outlives the registration.
DEFAULT_ARCHIVE_DAYS = 30.0

#: How long the first request after a quiet spell waits for the rest of
#: its write (semaphore, payload, semaphore) before it is handled.
SETTLE_SECONDS = 1.0

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_REQUESTS_PER_PASS = 50
#: Rejected requests kept per session (payload, semaphore, reason).
MAX_REJECTED_KEPT = 100
INBOX_PAGE = 20

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def valid_name(name: str) -> bool:
    return bool(isinstance(name, str) and _NAME_RE.match(name)
                and not name.endswith((".sem", ".json", ".md")))


def instructions_source() -> Path:
    return Path(__file__).resolve().parent / "cloud" / INSTRUCTIONS_NAME


# ─── settings ───────────────────────────────────────────────────────────

#: Where the relay folder lives when the config names none — a plain
#: local folder on every OS, because the desktop app can only link a
#: local folder (not a network share).
DEFAULT_ROOT_NAME = "claude-mailbox"


def default_root() -> str:
    return str(Path.home() / DEFAULT_ROOT_NAME)


def settings(cfg: Optional[dict]) -> dict:
    """``hooks.mailbox.cloud_relay``, clamped.

    The relay is part of the mailbox: it is on wherever the mailbox is,
    unless ``cloud_relay.enabled`` is explicitly false, and its folder
    defaults to :func:`default_root`. It costs nothing while idle, so
    there is no reason for a host to be without it — a Windows user who
    installs the mailbox gets a folder to link like anyone else."""
    mailbox = {}
    if isinstance(cfg, dict):
        mailbox = (cfg.get("hooks") or {}).get("mailbox") or {}
    section = mailbox.get("cloud_relay") or {}

    def num(key, default, floor=None):
        try:
            value = float(section.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(floor, value) if floor is not None else value

    aliases = section.get("aliases")
    if aliases is not None and not isinstance(aliases, list):
        aliases = None
    root = section.get("root") or default_root()
    return {
        "enabled": bool(mailbox.get("enabled")
                        and section.get("enabled", True)),
        "root": os.path.expanduser(str(root)),
        "host": str(section.get("host") or DEFAULT_HOST),
        "interval": num("interval_seconds", MIN_INTERVAL_SECONDS,
                        MIN_INTERVAL_SECONDS),
        "writing_timeout": num("writing_timeout_seconds",
                               DEFAULT_WRITING_TIMEOUT_SECONDS, 60.0),
        "live_hours": num("live_hours", DEFAULT_LIVE_HOURS, 0.1),
        "archive_days": num("archive_days", DEFAULT_ARCHIVE_DAYS, 1.0),
        "aliases": [str(a) for a in aliases] if aliases else None,
        "watcher": str(section.get("watcher") or "auto"),
    }


# ─── files ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Semaphore:
    status: str
    op: str
    bytes: Optional[int]


def read_semaphore(path: Path) -> Optional[Semaphore]:
    """The semaphore, or None while it is not (yet) a valid one.

    None is not an error: a semaphore caught mid-write is exactly the
    case the protocol exists for, and it is retried, not rejected —
    until the writing timeout says the writer is gone."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status not in STATUSES:
        return None
    size = data.get("bytes")
    if size is not None and (isinstance(size, bool)
                             or not isinstance(size, int) or size < 0):
        return None
    return Semaphore(status=status, op=str(data.get("op") or ""),
                     bytes=size)


def _write_text(path: Path, text: str) -> int:
    data = text.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
    return len(data)


def write_pair(directory: Path, stem: str, ext: str, text: str,
               op: str) -> Path:
    """Write ``<stem><ext>`` the way the protocol asks a session to:
    semaphore ``writing``, payload, then semaphore ``ready`` with the
    payload's size. A session that follows ``MAILBOX.md`` therefore
    never reads half an answer."""
    directory.mkdir(parents=True, exist_ok=True)
    sem = directory / f"{stem}.sem"
    _write_text(sem, json.dumps({"op": op, "status": WRITING}))
    size = _write_text(directory / f"{stem}{ext}", text)
    _write_text(sem, json.dumps({"op": op, "status": READY,
                                 "bytes": size}))
    return directory / f"{stem}{ext}"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ─── the relay ──────────────────────────────────────────────────────────

class RelayCore:
    """Everything the relay does, without threads or watchers, so every
    rule is testable with a temp dir and an injected clock."""

    def __init__(self, root: Path, opts: dict, *,
                 store_factory: Callable[[], object],
                 clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.opts = opts
        self.host = opts["host"]
        self._store_factory = store_factory
        self._store = None
        self._clock = clock
        self._tools: dict[str, object] = {}
        self._activity: dict[str, float] = {}
        self._pending: set[tuple[str, str]] = set()
        self._inbox_sig: dict[str, tuple] = {}
        self._last_archive = 0.0
        self.instructions = "unknown"
        self._load_activity()

    # -- paths
    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    def _dir(self, alias: str) -> Path:
        return self.sessions / alias

    def session_id(self, alias: str) -> str:
        return f"{self.host}-{alias}"

    # -- state
    def _status_path(self, alias: str) -> Path:
        return self._dir(alias) / "status.json"

    def read_status(self, alias: str) -> Optional[dict]:
        try:
            data = json.loads(self._status_path(alias).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def registered(self, alias: str) -> bool:
        st = self.read_status(alias)
        return bool(st and st.get("alias") == alias
                    and st.get("host") == self.host)

    def _load_activity(self) -> None:
        try:
            dirs = [p for p in self.sessions.iterdir() if p.is_dir()]
        except OSError:
            return
        for d in dirs:
            st = self.read_status(d.name)
            if st and isinstance(st.get("last_activity"), (int, float)):
                self._activity[d.name] = float(st["last_activity"])

    def live_aliases(self, now: Optional[float] = None) -> list[str]:
        now = self._clock() if now is None else now
        window = self.opts["live_hours"] * 3600.0
        return sorted(a for a, t in self._activity.items()
                      if now - t < window)

    def next_wakeup(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds until the next timed pass, or None: block until a file
        event. Only a live cloud session or a request still waiting for
        its writer earns a timer."""
        if self._pending or self.live_aliases(now):
            return self.opts["interval"]
        return None

    def store(self):
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def _tools_for(self, alias: str):
        tools = self._tools.get(alias)
        if tools is None:
            from claude_hooks.mailbox.tools import MailboxTools
            tools = MailboxTools(self.store(), alias=alias,
                                 session_id=self.session_id(alias),
                                 host=self.host)
            # Registration is the alias request's job, with the cwd it
            # was given; a tool call only refreshes last_seen.
            tools._registered = True
            self._tools[alias] = tools
        return tools

    # -- passes
    def check_instructions(self) -> str:
        target = self.root / INSTRUCTIONS_NAME
        try:
            want = instructions_source().read_bytes()
        except OSError:
            state = "source-missing"
        else:
            try:
                state = ("current" if target.read_bytes() == want
                         else "stale")
            except FileNotFoundError:
                state = "missing"
            except OSError:
                state = "unreadable"
        if state != self.instructions and state != "current":
            log.warning("relay: %s is %s — run install.py or "
                        "scripts/deploy.py to install it", target, state)
        self.instructions = state
        return state

    def full_scan(self) -> int:
        self.check_instructions()
        try:
            aliases = sorted(p.name for p in self.sessions.iterdir()
                             if p.is_dir())
        except OSError:
            return 0
        done = 0
        for alias in aliases:
            done += self.process_alias(alias)
        return done

    def handle_changes(self, changes: Iterable[tuple]) -> int:
        """One pass over what the watcher reported, plus anything still
        waiting for its writer."""
        changes = set(changes)
        if RESCAN in changes:
            return self.full_scan() + self._retry_pending()
        by_alias: dict[str, Optional[set]] = {}
        for ch in changes:
            if len(ch) < 2 or ch[0] != "sessions":
                continue
            alias = ch[1]
            if len(ch) == 4 and ch[2] == "requests" and ch[3].endswith(
                    ".sem"):
                if by_alias.get(alias, set()) is not None:
                    by_alias.setdefault(alias, set()).add(ch[3][:-4])
            else:
                by_alias[alias] = None          # new dir: scan all of it
        done = 0
        for alias, rids in sorted(by_alias.items()):
            done += self.process_alias(alias, rids)
        return done + self._retry_pending()

    def tick(self) -> int:
        """The timed pass: requests waiting for their writer, and the
        inbox summary of every live session."""
        done = self._retry_pending()
        now = self._clock()
        for alias in self.live_aliases(now):
            self.refresh_inbox(alias)
        if now - self._last_archive > 86400.0:
            self._last_archive = now
            self.archive_idle(now)
        return done

    def _retry_pending(self) -> int:
        if not self._pending:
            return 0
        by_alias: dict[str, set] = {}
        for alias, rid in self._pending:
            by_alias.setdefault(alias, set()).add(rid)
        done = 0
        for alias, rids in by_alias.items():
            done += self.process_alias(alias, rids)
        return done

    def process_alias(self, alias: str, rids: Optional[set] = None) -> int:
        if not valid_name(alias):
            return 0
        req = self._dir(alias) / "requests"
        if rids is None:
            try:
                rids = {e.name[:-4] for e in os.scandir(req)
                        if e.name.endswith(".sem")}
            except OSError:
                rids = set()
            for alias_, rid in list(self._pending):
                if alias_ == alias:
                    rids.add(rid)
        # The alias request first, so requests that arrived with it run
        # as the identity it establishes; then oldest semaphore first.
        def order(rid):
            try:
                mtime = (req / f"{rid}.sem").stat().st_mtime
            except OSError:
                mtime = 0.0
            sem = read_semaphore(req / f"{rid}.sem")
            return (0 if sem and sem.op == ALIAS_OP else 1, mtime, rid)

        done = 0
        for n, rid in enumerate(sorted(rids, key=order)):
            if n >= MAX_REQUESTS_PER_PASS:
                self._pending.add((alias, rid))
                continue
            try:
                outcome = self._process_request(alias, rid)
            except OSError as e:
                log.warning("relay: %s/%s: %s", alias, rid, e)
                outcome = "pending"
            if outcome == "pending":
                self._pending.add((alias, rid))
            else:
                self._pending.discard((alias, rid))
                if outcome == "done":
                    done += 1
        return done

    def _process_request(self, alias: str, rid: str) -> str:
        """``done`` (acted on or rejected), ``pending`` (wait for the
        writer) or ``gone`` (nothing there)."""
        req = self._dir(alias) / "requests"
        sem_path = req / f"{rid}.sem"
        payload = req / f"{rid}.json"
        if not valid_name(rid):
            return "gone"
        try:
            sem_mtime = sem_path.stat().st_mtime
        except FileNotFoundError:
            return "gone"            # no semaphore: never touched
        except OSError:
            return "pending"
        expired = (self._clock() - sem_mtime
                   > self.opts["writing_timeout"])
        sem = read_semaphore(sem_path)
        if sem is None:
            if expired:
                self.reject(alias, rid, "the semaphore never became valid "
                            "JSON with a known status")
                return "done"
            return "pending"
        if sem.status == CANCELLED:
            _unlink(payload)
            _unlink(sem_path)
            return "gone"
        if sem.status == WRITING:
            if expired:
                self.reject(alias, rid, "still `writing` after "
                            f"{int(self.opts['writing_timeout'])} s")
                return "done"
            return "pending"
        # ready
        try:
            size = payload.stat().st_size
        except FileNotFoundError:
            if expired:
                self.reject(alias, rid, "`ready`, but the payload "
                            f"{rid}.json never appeared")
                return "done"
            return "pending"
        if size > MAX_PAYLOAD_BYTES:
            self.reject(alias, rid, f"payload is {size} bytes; the limit "
                        f"is {MAX_PAYLOAD_BYTES}")
            return "done"
        if sem.bytes is not None and size != sem.bytes:
            if expired:
                self.reject(alias, rid, f"the semaphore says {sem.bytes} "
                            f"bytes, the payload has {size}")
                return "done"
            return "pending"          # still syncing
        try:
            data = json.loads(payload.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            if expired:
                self.reject(alias, rid, "the payload is not valid JSON")
                return "done"
            return "pending"
        problem = self._invalid(data, sem)
        if problem:
            self.reject(alias, rid, problem)
            return "done"
        # Read, then delete both — only now is the request ours. A
        # request that cannot be removed is not run: it would still be
        # there next pass and run again, and a send would go out twice.
        try:
            _unlink(payload)
            _unlink(sem_path)
        except OSError as e:
            log.warning("relay: cannot remove %s/%s, not running it: %s",
                        alias, rid, e)
            return "pending"
        tool = data["tool"]
        args = data.get("args") or {}
        try:
            if tool == ALIAS_OP:
                answer = self.take_alias(alias, args)
            elif not self.registered(alias):
                answer = (f"REJECTED: `{alias}` has no alias yet. Send a "
                          f"`{ALIAS_OP}` request first (see MAILBOX.md).")
            else:
                answer = self._tools_for(alias).call(tool, args,
                                                     headless=False)
                self._touch(alias)
        except Exception as e:  # never take the daemon down
            log.warning("relay: %s/%s (%s) failed: %s", alias, rid, tool, e,
                        exc_info=True)
            answer = f"ERROR: {tool} failed on the relay: {e}"
        write_pair(self._dir(alias) / "replies", rid, ".md",
                   f"# {tool} — request {rid}\n\n{answer}\n", tool)
        if tool != ALIAS_OP or self.registered(alias):
            self.refresh_inbox(alias)
        return "done"

    @staticmethod
    def _invalid(data, sem: Semaphore) -> Optional[str]:
        from claude_hooks.mailbox.tools import TOOL_NAMES
        if not isinstance(data, dict):
            return "the payload must be a JSON object"
        tool = data.get("tool")
        if tool != ALIAS_OP and tool not in TOOL_NAMES:
            return (f"unknown tool {tool!r}; one of: {ALIAS_OP}, "
                    + ", ".join(TOOL_NAMES))
        if sem.op and sem.op != tool:
            return (f"the semaphore says op {sem.op!r} but the payload "
                    f"asks for {tool!r}")
        args = data.get("args", {})
        if args is not None and not isinstance(args, dict):
            return "`args` must be a JSON object"
        return None

    # -- operations
    def take_alias(self, alias: str, args: dict) -> str:
        wanted = str(args.get("alias") or "").strip()
        if wanted != alias:
            return (f"REJECTED: the request asks for alias {wanted!r} but "
                    f"was written under sessions/{alias}/. The folder "
                    f"name is the alias: write it under "
                    f"sessions/{wanted or '<alias>'}/requests/.")
        allowed = self.opts.get("aliases")
        if allowed and alias not in allowed:
            return (f"REJECTED: alias `{alias}` is not on this relay's "
                    f"allow-list ({', '.join(allowed)}).")
        sid = self.session_id(alias)
        others = self.store().register(sid, alias,
                                       cwd=str(args.get("cwd") or ""),
                                       host=self.host)
        now = self._clock()
        prev = self.read_status(alias) or {}
        self._activity[alias] = now
        self._write_status(alias, {
            "alias": alias, "host": self.host, "session_id": sid,
            "address": f"{alias}@{self.host}",
            "registered_at": prev.get("registered_at") or _iso(now),
            "last_activity": now,
        })
        self._tools.pop(alias, None)
        elsewhere = sorted({f"{s.alias}@{s.host}" for s in others})
        lines = [f"Registered. You are `{alias}@{self.host}`; other "
                 f"sessions reach you at that address."]
        if elsewhere:
            lines.append(
                "The same alias is also registered at: "
                + ", ".join(elsewhere) + ". A bare `" + alias + "` is "
                "therefore ambiguous to senders; they should use `"
                + f"{alias}@{self.host}`.")
        lines.append("Unread mail is summarised in INBOX.md next to this "
                     "folder's requests/ and replies/.")
        return "\n\n".join(lines)

    def _touch(self, alias: str) -> None:
        now = self._clock()
        self._activity[alias] = now
        st = self.read_status(alias) or {}
        st["last_activity"] = now
        self._write_status(alias, st)

    def _write_status(self, alias: str, status: dict) -> None:
        status = dict(status)
        status["last_activity_at"] = _iso(float(
            status.get("last_activity") or self._clock()))
        status["instructions"] = self.instructions
        d = self._dir(alias)
        d.mkdir(parents=True, exist_ok=True)
        write_pair(d, "status", ".json", json.dumps(status, indent=1,
                                                    sort_keys=True),
                   "status")

    def refresh_inbox(self, alias: str) -> bool:
        """Rewrite INBOX.md when the unread set changed. One indexed page
        query; nothing is written when it is unchanged."""
        if not self.registered(alias):
            return False
        try:
            page = self.store().inbox_page(
                alias=alias, session_id=self.session_id(alias),
                host=self.host, page_size=INBOX_PAGE)
        except Exception as e:
            log.debug("relay: inbox of %s unavailable: %s", alias, e)
            return False
        sig = (page.total, tuple(r.get("id") for r in page.rows))
        if self._inbox_sig.get(alias) == sig:
            return False
        self._inbox_sig[alias] = sig
        write_pair(self._dir(alias), "INBOX", ".md",
                   render_inbox(alias, self.host, page, self._clock()),
                   "inbox")
        return True

    def reject(self, alias: str, rid: str, reason: str) -> None:
        d = self._dir(alias)
        rej = d / "rejected"
        rej.mkdir(parents=True, exist_ok=True)
        for ext in (".json", ".sem"):
            src = d / "requests" / f"{rid}{ext}"
            if src.exists():
                try:
                    os.replace(src, rej / f"{rid}{ext}")
                except OSError:
                    _unlink(src)
        (rej / f"{rid}.reason.txt").write_text(reason + "\n",
                                               encoding="utf-8")
        write_pair(d / "replies", rid, ".md",
                   f"# request {rid}\n\nREJECTED: {reason}\n\nThe files "
                   f"are in rejected/. Fix and resend under a new id.\n",
                   "rejected")
        log.info("relay: rejected %s/%s: %s", alias, rid, reason)
        self._trim_rejected(rej)

    @staticmethod
    def _trim_rejected(rej: Path) -> None:
        try:
            reasons = sorted(rej.glob("*.reason.txt"),
                             key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for old in reasons[:-MAX_REJECTED_KEPT]:
            rid = old.name[:-len(".reason.txt")]
            for ext in (".json", ".sem", ".reason.txt"):
                _unlink(rej / f"{rid}{ext}")

    def archive_idle(self, now: Optional[float] = None) -> list[str]:
        """Move session folders idle past ``archive_days`` to
        ``archive/<alias>-<date>``. Moved, never deleted."""
        now = self._clock() if now is None else now
        cutoff = self.opts["archive_days"] * 86400.0
        moved = []
        for alias, last in list(self._activity.items()):
            if now - last <= cutoff:
                continue
            src = self._dir(alias)
            dst = self.root / "archive" / (
                f"{alias}-{datetime.fromtimestamp(now, timezone.utc):%Y%m%d}")
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError as e:
                log.debug("relay: could not archive %s: %s", src, e)
                continue
            self._activity.pop(alias, None)
            self._tools.pop(alias, None)
            self._inbox_sig.pop(alias, None)
            moved.append(alias)
            log.info("relay: archived idle session folder %s -> %s",
                     alias, dst)
        return moved


def render_inbox(alias: str, host: str, page, now: float) -> str:
    lines = [f"# Inbox — {alias}@{host}", "",
             f"Updated {_iso(now)}. **Unread: {page.total}**", ""]
    if not page.rows:
        lines.append("Nothing unread.")
    else:
        lines += ["| id | from | subject | sent | priority |",
                  "|---|---|---|---|---|"]
        for r in page.rows:
            created = r.get("created_at")
            if hasattr(created, "strftime"):
                created = created.strftime("%Y-%m-%d %H:%M")
            subject = str(r.get("subject") or "").replace("|", "\\|")
            lines.append(
                f"| {r.get('id')} | {r.get('from_alias')}@"
                f"{r.get('from_host')} | {subject} | {created} | "
                f"{r.get('priority') or 0} |")
        if page.total > len(page.rows):
            lines += ["", f"Showing {len(page.rows)} of {page.total}; use "
                      "a `mailbox-list` request for the rest."]
        lines += ["", "Listing mail here does not mark it read. Read it "
                  "with a `mailbox-read` request."]
    return "\n".join(lines) + "\n"


# ─── install ────────────────────────────────────────────────────────────

def install_instructions(root: Path, *, dry_run: bool = False) -> str:
    """Copy ``MAILBOX.md`` into the relay folder. Returns ``installed``,
    ``updated`` or ``current``. Used by ``install.py``;
    ``scripts/deploy.py`` does the same copy so it cannot go stale."""
    root = Path(root)
    src = instructions_source()
    dst = root / INSTRUCTIONS_NAME
    want = src.read_bytes()
    try:
        have = dst.read_bytes()
    except FileNotFoundError:
        have = None
    if have == want:
        return "current"
    if not dry_run:
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        dst.write_bytes(want)
    return "installed" if have is None else "updated"


# ─── systemd sandbox ────────────────────────────────────────────────────

DAEMON_UNIT = "claude-hooks-daemon.service"
GRANT_DROPIN = "mailbox-relay.conf"


def relay_rw_paths(root) -> list[str]:
    """The relay folder as spelled and, when a symlink is involved, as
    resolved. ``ProtectSystem=strict`` builds the service's mount
    namespace from the literal ``ReadWritePaths``, so granting only the
    symlinked spelling (``/shared/dev/mailbox``) leaves the real
    directory read-only."""
    spelled = Path(os.path.expanduser(str(root)))
    out: list[str] = []
    for q in (spelled, spelled.resolve()):
        if str(q) not in out:
            out.append(str(q))
    return out


def grant_dropin_text(root) -> str:
    # "-": a missing path must not stop the daemon from starting.
    paths = " ".join("-" + p for p in relay_rw_paths(root))
    return ("# Written by claude-hooks: the cloud-session mailbox relay\n"
            "# writes replies into this folder (hooks.mailbox.cloud_relay).\n"
            f"[Service]\nReadWritePaths={paths}\n")


def daemon_unit_paths() -> list[tuple[Path, str]]:
    """``(unit file, scope)`` for every installed daemon unit."""
    found = []
    for base, scope in ((Path("/etc/systemd/system"), "system"),
                        (Path(os.path.expanduser("~/.config/systemd/user")),
                         "user")):
        unit = base / DAEMON_UNIT
        if unit.is_file():
            found.append((unit, scope))
    return found


def unit_sandboxed(unit: Path) -> bool:
    try:
        text = unit.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.strip().startswith(("ProtectSystem=strict",
                                         "ProtectHome="))
               for line in text.splitlines())


def missing_grants(unit: Path, root) -> list[str]:
    """Relay paths a sandboxed daemon unit (or its drop-ins) does not
    grant. Empty when the unit is not sandboxed."""
    if not unit_sandboxed(unit):
        return []
    granted: set[str] = set()
    for f in [unit] + sorted(Path(f"{unit}.d").glob("*.conf")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ReadWritePaths="):
                granted.update(x.lstrip("-+")
                               for x in line.split("=", 1)[1].split())
    need = relay_rw_paths(root)
    # A grant of a parent covers the child.
    granted_paths = [Path(g) for g in granted if g]
    return [p for p in need if not any(
        Path(p) == g or g in Path(p).parents for g in granted_paths)]


def ensure_unit_grant(root, *, dry_run: bool = False) -> list[str]:
    """Write the drop-in that lets a sandboxed daemon write the relay
    folder, and reload systemd. Returns what it did, for the caller to
    print; raises OSError when the drop-in cannot be written."""
    import subprocess
    notes = []
    for unit, scope in daemon_unit_paths():
        if not missing_grants(unit, root):
            continue
        dropin = Path(f"{unit}.d") / GRANT_DROPIN
        if dry_run:
            notes.append(f"[dry-run] would write {dropin}")
            continue
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(grant_dropin_text(root), encoding="utf-8")
        cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + [
            "daemon-reload"]
        subprocess.run(cmd, capture_output=True, timeout=30)
        notes.append(f"wrote {dropin} (daemon restart applies it)")
    return notes


def writable_problem(root: Path) -> Optional[str]:
    """None when the relay can write the folder; otherwise what to do."""
    probe = Path(root) / "sessions" / ".relay-write-probe"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as e:
        units = daemon_unit_paths()
        hint = ""
        if units and any(missing_grants(u, root) for u, _ in units):
            hint = (" The daemon's systemd unit is sandboxed and does not "
                    "grant this folder: run scripts/deploy.py (it writes "
                    f"{DAEMON_UNIT}.d/{GRANT_DROPIN}), or add "
                    "ReadWritePaths=" + " ".join(relay_rw_paths(root))
                    + " to a drop-in.")
        return f"cannot write {root}: {e}.{hint}"


# ─── the daemon thread ──────────────────────────────────────────────────

def _store_from_config(cfg: dict):
    from claude_hooks.dispatcher import build_providers
    from claude_hooks.mailbox.integration import store_for_provider
    for provider in build_providers(cfg or {}):
        try:
            store = store_for_provider(provider)
        except Exception:
            continue
        if store is not None:
            return store
    raise RuntimeError("no SQL-backed memory provider (pgvector / "
                       "sqlite_vec) for the mailbox")


class MailboxRelayThread(threading.Thread):
    """Blocks on the watcher, batches events into one pass per interval,
    and runs the timed pass only while a cloud session is live.

    Config is read once, at start: a thread that re-read it on a timer
    would wake the host to do so, which is the cost this design exists
    to avoid. Restart the daemon (``scripts/deploy.py`` does) to apply a
    change."""

    def __init__(self, cfg: dict, *, stop_event: threading.Event,
                 name: str = "mailbox-relay"):
        super().__init__(name=name, daemon=True)
        self.opts = settings(cfg)
        self._cfg = cfg
        self._stop_event = stop_event
        self.core: Optional[RelayCore] = None
        self.watcher = None

    def stop(self) -> None:
        self._stop_event.set()
        if self.watcher is not None:
            self.watcher.wake()

    def run(self) -> None:  # pragma: no cover - thread loop
        root = Path(self.opts["root"])
        try:
            (root / "sessions").mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("relay: not started — %s",
                      writable_problem(root) or f"{root} unusable: {e}")
            return
        problem = writable_problem(root)
        if problem:
            # Refuse to start rather than fail every pass: a relay that
            # can read requests but not answer them looks alive to the
            # session and never replies.
            log.error("relay: not started — %s", problem)
            return
        self.core = RelayCore(root, self.opts,
                              store_factory=lambda: _store_from_config(
                                  self._cfg))
        self.watcher = make_watcher(root, self.opts["watcher"],
                                    poll_interval=self.opts["interval"])
        log.info("relay: watching %s (%s, one pass per %.0f s)", root,
                 self.watcher.kind, self.opts["interval"])
        try:
            self.loop()
        finally:
            self.watcher.close()

    def loop(self) -> None:
        core, watcher, interval = self.core, self.watcher, self.opts["interval"]
        try:
            core.full_scan()
        except Exception:
            log.warning("relay: startup scan failed", exc_info=True)
        # When a pass last *handled a request*. Only that starts the
        # throttle: the first request after a quiet spell is answered
        # after a short settle, and only a burst behind it waits for the
        # interval. Counting every pass made a session's first request
        # wait up to 30 s twice over — behind the inbox refresh that runs
        # every interval while a session is live, and behind the pass
        # its own `writing` semaphore triggered, which found nothing
        # ready — and the session concluded it was stuck.
        last_work = float("-inf")
        while not self._stop_event.is_set():
            timeout = core.next_wakeup()
            started = time.monotonic()
            changes = watcher.wait(timeout)
            if self._stop_event.is_set():
                break
            timed_out = (timeout is not None
                         and time.monotonic() - started >= timeout - 0.5)
            if not changes and not timed_out:
                # Woken by something that is not a request — the daemon's
                # own writes into a watched folder, a wake() — so there
                # is nothing to do and no timer has elapsed.
                continue
            if changes:
                # Settle briefly so a three-file write lands in one pass;
                # after real work, batch until the interval has passed.
                wait = max(SETTLE_SECONDS,
                           last_work + interval - time.monotonic())
                deadline = time.monotonic() + wait
                while not self._stop_event.is_set():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    changes |= watcher.wait(left)
            if self._stop_event.is_set():
                break
            done = 0
            try:
                if changes:
                    done += core.handle_changes(changes)
                if timed_out:
                    done += core.tick()
            except Exception:
                log.warning("relay: pass failed", exc_info=True)
            if done:
                last_work = time.monotonic()


def start_relay_thread(cfg: dict, stop_event: threading.Event
                       ) -> Optional[MailboxRelayThread]:
    """Start the relay if this host is configured for it; None if not."""
    if not settings(cfg)["enabled"]:
        return None
    thread = MailboxRelayThread(cfg, stop_event=stop_event)
    thread.start()
    return thread


# ─── status CLI ─────────────────────────────────────────────────────────

def status_report(cfg: dict) -> str:
    opts = settings(cfg)
    root = Path(opts["root"])
    core = RelayCore(root, opts, store_factory=lambda: None)
    core.check_instructions()
    lines = [
        f"cloud relay: {'enabled' if opts['enabled'] else 'DISABLED'}",
        f"  root:         {root}",
        f"  instructions: {root / INSTRUCTIONS_NAME} ({core.instructions})",
        f"  host:         @{opts['host']}",
        f"  interval:     {opts['interval']:.0f} s",
    ]
    try:
        aliases = sorted(p.name for p in core.sessions.iterdir()
                         if p.is_dir())
    except OSError:
        aliases = []
    if not aliases:
        lines.append("  sessions:     none")
    live = set(core.live_aliases())
    for alias in aliases:
        st = core.read_status(alias) or {}
        req = core._dir(alias) / "requests"
        try:
            waiting = sum(1 for e in os.scandir(req)
                          if e.name.endswith(".sem"))
        except OSError:
            waiting = 0
        lines.append(
            f"  {alias}@{opts['host']}: "
            f"{'live' if alias in live else 'idle'}, last activity "
            f"{st.get('last_activity_at', 'never')}, "
            f"{waiting} request(s) waiting"
            + ("" if st else " (no alias taken yet)"))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m claude_hooks.mailbox.relay",
        description="Mailbox relay for cloud sessions.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="configuration, instructions and "
                                  "the sessions in the folder")
    p_inst = sub.add_parser("install-instructions",
                            help=f"copy {INSTRUCTIONS_NAME} into the folder")
    p_inst.add_argument("--root", help="folder (default: from config)")
    args = ap.parse_args(argv)
    from claude_hooks.config import load_config
    cfg = load_config()
    if args.cmd == "status":
        print(status_report(cfg))
        return 0
    root = args.root or settings(cfg)["root"]
    print(f"{INSTRUCTIONS_NAME}: {install_instructions(Path(root))}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
