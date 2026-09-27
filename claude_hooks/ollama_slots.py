"""Connection slots for Ollama calls, shared by every process on the host.

The Ollama Cloud plan caps **concurrent connections per account** (free
1, pro 3, max 10), and the cap covers every relay that carries the
calls: eleven2go's Ollama, the solidpc proxy and ollama.com itself all
count against the same account. Going over it does not queue on the
server; calls fail or stall. Until 2026-09-23 the only guard was a
convention ("benchmarks run at most 2 at once") plus the council's lane
cap, and neither sees the other: an engine fan-out and a benchmark
running side by side overran the plan.

So each HTTP attempt holds a slot:

- **Scopes.** ``cloud`` is one scope for the whole account, whatever
  base URL the call goes to. Each local Ollama is its own scope,
  ``local-<host>_<port>``: its limit is that server's parallelism, not
  the account's.
- **Cloud or local** is decided by the server: a model listed with a
  ``remote_host`` in ``/api/tags`` is cloud. The ``:cloud`` /
  ``-cloud`` name rule covers a server that cannot be asked.
- **Limits.** Cloud = the plan's connections (read from
  ``ollama.com/api/me``, the ``Plan`` field only, cached a day) minus
  ``reserve_for_hooks`` (1): the recall hooks run in their own
  short-lived processes, must not queue behind a benchmark, and so keep
  a connection this limiter never hands out. ``cloud_limit`` in config
  (or ``CLAUDE_HOOKS_OLLAMA_CLOUD_SLOTS``) overrides; set it when another
  host shares the account, since each host counts only its own calls.
- **Cross-process.** A slot is an OS file lock under
  ``~/.claude/ollama-slots/<scope>/``. The OS releases it when the
  process dies, so a killed benchmark cannot leak one.
- **Order.** Within a process, waiters are served first come, first
  served; between processes the next free slot goes to whichever
  process polls first.
- **Fail-open.** A lock that cannot be taken for a reason other than
  "busy", or a wait past ``max_wait_s``, lets the call through with a
  warning: the limiter protects the plan, it must never be the thing
  that stops work.

Config (``config/claude-hooks.json``)::

    "ollama_slots": {"enabled": true, "cloud_limit": null,
                     "reserve_for_hooks": 1, "local_limit": 2,
                     "max_wait_s": 1800}
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger(__name__)

PLAN_CONNECTIONS = {"free": 1, "pro": 3, "max": 10}
DEFAULT_PLAN = "pro"
DEFAULTS = {"enabled": True, "cloud_limit": None, "reserve_for_hooks": 1,
            "local_limit": 2, "max_wait_s": 1800.0}
ENV_CLOUD = "CLAUDE_HOOKS_OLLAMA_CLOUD_SLOTS"
ENV_DISABLE = "CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE"
_TAGS_TTL_S = 600.0
_PLAN_TTL_S = 86400.0


def slot_dir() -> Path:
    return Path.home() / ".claude" / "ollama-slots"


def settings(cfg: Optional[dict] = None) -> dict:
    if cfg is None:
        try:
            # mtime-cached: this runs on every HTTP attempt.
            from claude_hooks.model_sampling import _cached_json
            cfg = _cached_json("user")
        except Exception:  # noqa: BLE001 — no config is the defaults
            cfg = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in ((cfg or {}).get("ollama_slots") or {}).items()
                if k in DEFAULTS})
    return out


# ------------------------------------------------------------ cloud or local

_tags_cache: dict[str, tuple[float, Optional[dict]]] = {}
_tags_lock = threading.Lock()


def _remote_tags(base_url: str) -> Optional[dict]:
    """``{model: is_remote}`` from ``/api/tags``, cached; ``None`` when
    the server cannot be asked."""
    now = time.monotonic()
    with _tags_lock:
        hit = _tags_cache.get(base_url)
        if hit and now - hit[0] < _TAGS_TTL_S:
            return hit[1]
    table: Optional[dict] = None
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=3.0) as r:
            data = json.loads(r.read())
        table = {m.get("name") or m.get("model"): bool(m.get("remote_host"))
                 for m in data.get("models") or []}
    except Exception:  # noqa: BLE001 — fall back to the name rule
        table = None
    with _tags_lock:
        _tags_cache[base_url] = (now, table)
    return table


def is_cloud(base_url: str, model: str) -> bool:
    table = _remote_tags(base_url)
    if table and model in table:
        return table[model]
    return model.endswith(":cloud") or model.endswith("-cloud")


# ------------------------------------------------------------ the plan

def _plan_cache_path() -> Path:
    return Path.home() / ".claude" / "ollama-plan.json"


def _fetch_plan() -> Optional[str]:
    """The account's plan from ``ollama.com/api/me``. That response
    carries personal data; only ``Plan`` is read, and only it is kept."""
    key = Path.home() / ".ollama" / "id_ed25519"
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_ssh_private_key)
        priv = load_ssh_private_key(key.read_bytes(), password=None)
        pub = (key.with_suffix(".pub")).read_text().split()[1]
        ts = str(int(time.time()))
        sig = base64.b64encode(priv.sign(f"POST,/api/me?ts={ts}".encode()))
        req = urllib.request.Request(
            f"https://ollama.com/api/me?ts={ts}", data=b"{}", method="POST",
            headers={"Authorization": f"{pub}:{sig.decode()}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10.0) as r:
            plan = json.loads(r.read()).get("Plan")
    except Exception as e:  # noqa: BLE001 — no key, no lib, offline
        log.debug("ollama_slots: plan lookup failed: %s", e)
        return None
    return str(plan).lower() if plan else None


def plan() -> str:
    path = _plan_cache_path()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached["at"]) < _PLAN_TTL_S:
            return cached["plan"]
    except Exception:  # noqa: BLE001 — missing or stale
        pass
    got = _fetch_plan()
    if got is None:
        return DEFAULT_PLAN
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"plan": got, "at": time.time()}),
                        encoding="utf-8")
    except OSError:
        pass
    return got


def cloud_limit(cfg: Optional[dict] = None) -> int:
    s = settings(cfg)
    env = os.environ.get(ENV_CLOUD, "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            log.warning("ollama_slots: %s=%r is not an integer", ENV_CLOUD, env)
    if s["cloud_limit"] is not None:
        return max(0, int(s["cloud_limit"]))
    conns = PLAN_CONNECTIONS.get(plan(), PLAN_CONNECTIONS[DEFAULT_PLAN])
    return max(1, conns - int(s["reserve_for_hooks"]))


def scope_for(base_url: str, model: str,
              cfg: Optional[dict] = None) -> Optional[tuple[str, int]]:
    """``(scope, limit)``, or ``None`` when the call is not limited."""
    if os.environ.get(ENV_DISABLE):
        return None
    s = settings(cfg)
    if not s["enabled"]:
        return None
    if is_cloud(base_url, model):
        n = cloud_limit(cfg)
        return ("cloud", n) if n > 0 else None
    n = int(s["local_limit"] or 0)
    if n <= 0:
        return None
    host = re.sub(r"^\w+://", "", base_url).rstrip("/")
    return ("local-" + re.sub(r"[^\w.-]", "_", host), n)


# ------------------------------------------------------------ file locks

if os.name == "nt":
    import msvcrt

    def _try_lock(f) -> bool:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in (13, 33, 36):  # EACCES / lock violation / EDEADLOCK
                return False
            raise

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class _Queue:
    """First come, first served inside one process: only the head of
    the queue polls the lock files."""

    def __init__(self):
        self.cond = threading.Condition()
        self.next_ticket = 0
        self.serving = 0
        # Tickets whose caller left (cancelled) before its turn: skipped
        # when reached, or the queue would wait on them forever.
        self.gone: set[int] = set()


_queues: dict[str, _Queue] = {}
_queues_lock = threading.Lock()


def _queue(scope: str) -> _Queue:
    with _queues_lock:
        return _queues.setdefault(scope, _Queue())


def _try_any(d: Path, limit: int):
    for i in range(limit):
        f = open(d / f"slot-{i}.lock", "a+b")
        try:
            if _try_lock(f):
                return f
        except Exception:
            f.close()
            raise
        f.close()
    return None


@contextlib.contextmanager
def slot(base_url: str, model: str, *,
         cancel_check: Optional[Callable[[], bool]] = None,
         cfg: Optional[dict] = None) -> Iterator[Optional[str]]:
    """Hold a connection slot for one HTTP attempt. Yields the scope
    (``None`` when unlimited or when the limiter failed open). Raises
    ``Cancelled`` if ``cancel_check`` turns true while waiting."""
    sc = scope_for(base_url, model, cfg)
    if sc is None:
        yield None
        return
    scope, limit = sc
    max_wait = float(settings(cfg)["max_wait_s"])
    q = _queue(scope)
    with q.cond:
        ticket = q.next_ticket
        q.next_ticket += 1
    held = None
    t0 = time.monotonic()
    warned = t0
    try:
        d = slot_dir() / scope
        d.mkdir(parents=True, exist_ok=True)
        delay = 0.05
        while True:
            with q.cond:
                while q.serving != ticket:
                    q.cond.wait(timeout=0.5)
                    if cancel_check and cancel_check():
                        raise Cancelled(scope)
            held = _try_any(d, limit)
            if held is not None:
                break
            if cancel_check and cancel_check():
                raise Cancelled(scope)
            now = time.monotonic()
            if now - t0 > max_wait:
                log.warning("ollama_slots: waited %.0fs for a %s slot "
                            "(limit %d); proceeding without one",
                            now - t0, scope, limit)
                break
            if now - warned > 60:
                log.info("ollama_slots: %s × %s queued %.0fs (limit %d)",
                         model, scope, now - t0, limit)
                warned = now
            time.sleep(delay)
            delay = min(0.5, delay * 1.5)
    except Cancelled:
        _leave(q, ticket)
        raise
    except Exception as e:  # noqa: BLE001 — fail open, never block work
        log.warning("ollama_slots: limiter failed (%s); proceeding", e)
        held = None
    _advance(q, ticket)
    waited = time.monotonic() - t0
    if waited > 1.0:
        log.info("ollama_slots: %s got a %s slot after %.1fs", model, scope,
                 waited)
    try:
        yield scope if held is not None else None
    finally:
        if held is not None:
            try:
                _unlock(held)
            finally:
                held.close()


def _advance(q: _Queue, ticket: int) -> None:
    with q.cond:
        if q.serving == ticket:
            q.serving += 1
            while q.serving in q.gone:
                q.gone.discard(q.serving)
                q.serving += 1
        q.cond.notify_all()


def _leave(q: _Queue, ticket: int) -> None:
    """A waiter gives up its place: advance now if it is the head,
    otherwise mark it so the queue steps over it."""
    with q.cond:
        if q.serving != ticket:
            q.gone.add(ticket)
            q.cond.notify_all()
            return
    _advance(q, ticket)


class Cancelled(RuntimeError):
    """The caller cancelled while its call was still queued for a slot."""


def status(cfg: Optional[dict] = None) -> dict:
    """``{scope: {"limit", "busy"}}`` for the scopes this host has used
    — for ``claude-hooks`` diagnostics and tests."""
    out = {}
    root = slot_dir()
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        busy = 0
        files = sorted(d.glob("slot-*.lock"))
        for p in files:
            with open(p, "a+b") as f:
                try:
                    if _try_lock(f):
                        _unlock(f)
                    else:
                        busy += 1
                except OSError:
                    busy += 1
        out[d.name] = {"slots_seen": len(files), "busy": busy}
    return out
