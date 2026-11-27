"""The engine wakes the session that started a run (backup_models #2414).

csl-2026-11-27-0656-503b sat 20 min at the adversary checkpoint: the
caller's waiter blocked until the run ended, and nothing else told the
session the council wanted an answer. A waiter is only as good as the
caller's choice of one, so the engine pushes too.

``consult`` / ``follow-up`` send ``notify`` from the caller's environment
(``caller_notify()`` in consultants/cli.py): Claude Code's inbox socket
for that session (``CLAUDE_CODE_MESSAGING_SOCKET``, its token, the
session id, the host). One :class:`AttentionWatcher` per run reads the
run's own status record every ``POLL_S`` through the same
``attention_of()`` the CLI waiter uses, and new ``slow_tool`` guard events
from the run's transcript, and posts once per distinct state:

* **inbox** — one line into the session's socket (Claude Code's own
  injection format: an auth line, then a ``{"type":"user",...}`` record).
  An idle session starts a turn with it; a busy one reads it between tool
  calls. Claude Code labels it as another session's message, never the
  user's, so it can never answer a permission prompt.
* **mail** — when the socket is on another host or does not answer: a
  priority-1 mailbox message to the alias the session registered.

Nothing here ever fails a run: every step is soft and logged.
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from consultants.attention import RUN_STILL_GOING, attention_of

log = logging.getLogger("consultants.server.attention_push")

POLL_S = 5.0
#: A tool still running this long is worth a word (the lane can be
#: stopped with wrap-up or cancel; c8ba719).
SLOW_TOOL_PUSH_S = 600.0
INBOX_TIMEOUT_S = 5.0
NOTIFY_FIELDS = ("socket", "token", "session_id", "host")


def notify_from_body(body: dict) -> Optional[dict]:
    """The caller's push target from a request body, or None. Only the
    known fields, as strings; a socket must be an absolute path."""
    raw = body.get("notify") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return None
    out = {k: str(raw[k]) for k in NOTIFY_FIELDS
           if isinstance(raw.get(k), str) and raw.get(k)}
    if out.get("socket") and not out["socket"].startswith("/"):
        out.pop("socket")
    return out if (out.get("socket") or out.get("session_id")) else None


def local_host() -> str:
    from claude_hooks.mailbox.store import host_name
    return host_name()


def post_inbox(socket_path: str, token: str, text: str, *,
               timeout: float = INBOX_TIMEOUT_S) -> bool:
    """Post ``text`` into a Claude Code session's inbox socket."""
    family = getattr(socket, "AF_UNIX", None)
    if family is None or not socket_path:
        return False
    frames = []
    if token:
        frames.append(json.dumps({"type": "auth", "token": token}))
    frames.append(json.dumps({"type": "user", "message": {
        "role": "user", "content": text}}))
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall(("\n".join(frames) + "\n").encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            try:
                s.recv(1024)          # the inbox closes after reading
            except OSError:
                pass
        return True
    except OSError as e:
        log.info("attention push: inbox %s unreachable (%s)", socket_path, e)
        return False


def _mail_store():
    from claude_hooks.config import load_config
    from claude_hooks.mailbox.relay import _store_from_config
    return _store_from_config(load_config())


def mail_fallback(notify: dict, subject: str, body: str, *,
                  store_fn: Callable = _mail_store) -> bool:
    """A priority-1 mail to the alias the calling session registered."""
    sid = notify.get("session_id")
    if not sid:
        return False
    try:
        store = store_fn()
        if store is None:
            return False
        alias = store.registered_alias(sid)
        if not alias:
            return False
        host = notify.get("host") or local_host()
        store.send(f"{alias}@{host}", subject, body,
                   from_alias="consultants", from_host=local_host(),
                   priority=1)
        return True
    except Exception:
        log.info("attention push: mail to session %s failed", sid,
                 exc_info=True)
        return False


def deliver(notify: dict, subject: str, body: str, *,
            here: Optional[str] = None,
            post_fn: Callable = post_inbox,
            mail_fn: Callable = mail_fallback) -> str:
    """``inbox``, ``mail`` or ``none``: the first channel that took it."""
    here = here if here is not None else local_host()
    if notify.get("socket") and notify.get("host", here) == here:
        if post_fn(notify["socket"], notify.get("token", ""),
                   f"{subject}\n\n{body}"):
            return "inbox"
    if mail_fn(notify, subject, body):
        return "mail"
    return "none"


def _clock(ts) -> str:
    try:
        return time.strftime("%H:%M:%S %Z", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


NOT_THE_USER = ("(Automated notice from the /consultants engine for a council "
                "this session started — not a message from the user.)")


def describe(sid: str, why: dict) -> tuple[str, str]:
    """Subject and body for one attention state, terminal or not."""
    reason = why.get("reason")
    rewait = f"claude-consultants wait {sid}"
    if reason == "finished":
        status = why.get("status")
        subject = f"[consultants] {sid} {status}"
        body = (f"The council finished with status {status}. "
                + (f"Fetch it: claude-consultants result {sid}"
                   if status == "completed" else
                   f"See: claude-consultants status {sid}"))
    elif reason == "slow_tool":
        subject = (f"[consultants] {sid}: a {why.get('tool')} call has run "
                   f"{int(why.get('elapsed_s') or 0) // 60} min")
        body = (f"Lane {why.get('lane')} of the council is still inside "
                f"{why.get('tool')}({why.get('args', '')[:200]}). If it is "
                f"stuck, end the lanes gracefully with `claude-consultants "
                f"wrap-up {sid}` (the call is stopped and the synthesizer "
                f"answers on what exists) or cancel it.")
    else:
        dl = why.get("deadline_ts")
        until = (f" until {_clock(dl)} ({why.get('on_timeout')} then)"
                 if dl else "")
        subject = f"[consultants] {sid} needs you: {reason}{until}"
        body = (f"Answer with: {why.get('answer')}\n"
                f"Then re-arm a background waiter: {rewait}")
    return subject, f"{body}\n{NOT_THE_USER}"


class AttentionWatcher(threading.Thread):
    """Pushes each new attention state of one run to its caller."""

    def __init__(self, state, notify: dict, *, cwd: str = "",
                 poll_s: float = POLL_S,
                 deliver_fn: Callable = deliver,
                 clock: Callable[[], float] = time.monotonic,
                 stop_event: Optional[threading.Event] = None):
        super().__init__(name=f"attention-{state.sid}", daemon=True)
        self.state, self.notify, self.cwd = state, dict(notify), cwd
        self.poll_s, self.deliver_fn, self.clock = poll_s, deliver_fn, clock
        self.stop_event = stop_event or threading.Event()
        self.sent: list[tuple[str, str]] = []     # (reason, channel)
        self._last_key: Optional[tuple] = None
        self._pending_since: Optional[float] = None
        self._event_id = 0
        self._slow_seen: set = set()

    def _push(self, why: dict) -> None:
        subject, body = describe(self.state.sid, why)
        try:
            channel = self.deliver_fn(self.notify, subject, body)
        except Exception:
            log.info("attention push failed for %s", self.state.sid,
                     exc_info=True)
            channel = "none"
        self.sent.append((why.get("reason"), channel))
        log.info("attention push %s: %s via %s", self.state.sid,
                 why.get("reason"), channel)

    def _slow_tools(self) -> list[dict]:
        if not self.cwd:
            return []
        db = Path(self.cwd) / ".claude-hooks" / "consultants" / \
            self.state.sid / "transcript.db"
        if not db.is_file():
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                rows = con.execute(
                    "SELECT event_id, lane_idx, payload FROM runtime_events "
                    "WHERE kind = 'guard' AND event_id > ? ORDER BY event_id",
                    (self._event_id,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []
        out = []
        for event_id, lane, payload in rows:
            self._event_id = max(self._event_id, int(event_id))
            try:
                ev = json.loads(payload or "{}")
            except ValueError:
                continue
            if (ev.get("kind") == "slow_tool"
                    and float(ev.get("elapsed_s") or 0) >= SLOW_TOOL_PUSH_S):
                key = (lane, ev.get("tool"), ev.get("args"))
                if key not in self._slow_seen:
                    self._slow_seen.add(key)
                    out.append({"reason": "slow_tool", "lane": lane,
                                "tool": ev.get("tool"),
                                "args": str(ev.get("args") or ""),
                                "elapsed_s": ev.get("elapsed_s")})
        return out

    def check(self) -> bool:
        """One look; False once the run has ended (after its push)."""
        rec = self.state.public_dict()
        status = str(rec.get("status") or "")
        if status not in RUN_STILL_GOING:
            self._push({"reason": "finished", "status": status})
            return False
        if status == "suspended":
            return True                # the next engine resumes it
        now = self.clock()
        if rec.get("pause_state") == "pending":
            self._pending_since = (now if self._pending_since is None
                                   else self._pending_since)
        else:
            self._pending_since = None
        why = attention_of(rec, pending_for=(
            now - self._pending_since if self._pending_since is not None
            else 0.0))
        key = (why.get("reason"), why.get("deadline_ts")) if why else None
        if why is not None and key != self._last_key:
            self._push(why)
        self._last_key = key
        for slow in self._slow_tools():
            self._push(slow)
        return True

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.check():
                    return
            except Exception:
                log.info("attention watcher %s: check failed",
                         self.state.sid, exc_info=True)
            self.stop_event.wait(self.poll_s)


def start(state, notify: Optional[dict], *, cwd: str = "") -> Optional[AttentionWatcher]:
    """Start the watcher for a run that has a push target."""
    if not notify:
        return None
    w = AttentionWatcher(state, notify, cwd=cwd)
    w.start()
    return w
