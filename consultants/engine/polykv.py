"""Per-run PolyKV manager (plan F3): which engine session and pool each
council call uses, what the engine reported, and a clean release.

One :class:`KvRunManager` per council run, registered under the run's
``runtime_control.polykv_run`` (seeded only when ``[polykv]`` is on, so
off sends byte-identical requests). It is a dependency like the
recorder, never council state: :class:`PlacedChat` wraps a role's chat
client and asks the manager for a ``placement`` on every call, after
compaction, so the pool always matches the messages actually sent.

Modes (``[polykv] mode``):

- ``off``: nothing.
- ``affinity``: a stable engine session per conversation, so a tool
  loop's turns return to their warm slot.
- ``pooled``: as affinity, and the conversation's leading system run
  (grounding + role prompt + tools) is a **shared** root pool
  (find-or-create by exact tokens, ephemeral), attached by every call
  whose head matches. Every call books its own per-request window.
- ``auto``: ``pooled`` when the engine has pools and shared roots,
  else ``affinity``.

``assured`` (one owner window per run) is the next step of F3 and is
treated as ``pooled`` until then.

A conversation is named the way xOllama derives its own ids: role,
model, the system turns and the first user turn (the plan item) — fixed
across a tool loop, different per lane. Two concurrent calls with the
same identity get distinct sessions: one engine session per agent.

Layers are rendered through the engine's ``/apply-template`` from the
messages as the chat client puts them on the wire, cut at a sentinel
user turn, and checked to be a byte prefix of the full rendering before
any pool is made. A pool the engine says it does not know, or one that
was named and shared nothing, is dropped and rebuilt, at most
:data:`MAX_REBUILDS` times per layer; a changed ``boot_id`` drops all of
them (pool ids restart after an engine restart).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from claude_hooks.polykv import wire
from claude_hooks.polykv.client import (
    OpencotiError, PolykvClient, pool_ref, wire_id,
)

log = logging.getLogger("consultants.polykv")

MODES = ("off", "affinity", "pooled", "assured", "auto")
#: A layer shorter than this is not worth a pool (xOllama's own automatic
#: pooling starts at 256 tokens; the engine warns under 64).
MIN_LAYER_CHARS = 1024
MAX_REBUILDS = 3
#: How long a failed probe waits before it is asked again (the engine
#: behind xOllama answers only once its model is loaded).
REPROBE_S = 10.0
#: Estimating a call's private tokens: headroom on the chars-per-cell
#: estimate, a fixed floor, per-turn template characters, and the
#: alignment of the stated window.
PRIVATE_MARGIN = 1.3
PRIVATE_FLOOR = 256
TURN_OVERHEAD_CHARS = 32
WINDOW_ALIGN = 256
#: How long a lane waits for a sibling of its run to free room before it
#: sends anyway (below the 300 s default stall threshold, which counts a
#: call with no chunk as stalled), and how often it looks again.
WAIT_FOR_ROOM_S = 240.0
ROOM_POLL_S = 5.0
SENTINEL = "⁣POLYKV_SENTINEL⁣"
#: xOllama seats a plain model needs before its client pools are used.
MIN_CLIENT_POOLS = 2


# ------------------------------------------------------------- the roots

@dataclass
class Root:
    """One engine + model the run sends calls to."""

    key: str
    kind: str                      # "direct" | "xollama"
    client: Optional[PolykvClient]
    mode: str = "off"
    reason: str = ""
    boot_id: Optional[str] = None
    generation: int = 0
    probed_at: float = 0.0
    #: The probe answered; until then the root is asked again every
    #: :data:`REPROBE_S`.
    settled: bool = False
    features: frozenset = frozenset()
    #: layer key -> pool id (string)
    pools: dict = field(default_factory=dict)
    rebuilds: dict = field(default_factory=dict)
    #: layer keys that will not be pooled again this run
    given_up: set = field(default_factory=set)
    #: pool id -> (cells, chars) of a layer this run made, for the private
    #: window of the calls that attach it
    sizes: dict = field(default_factory=dict)


@dataclass
class RoleStats:
    calls: int = 0
    placed: int = 0
    pooled: int = 0
    cached_tokens: int = 0
    shared_tokens: int = 0
    prompt_tokens: int = 0
    warnings: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _unwrap(client: Any, model: str) -> Any:
    """The client that will actually serve ``model``."""
    seen = 0
    while seen < 8:
        seen += 1
        inner = getattr(client, "_placed_inner", None)
        if inner is not None:
            client = inner
            continue
        traced = client.__dict__.get("_client") if hasattr(client, "__dict__") else None
        if traced is not None and type(client).__name__ == "TracedChat":
            client = traced
            continue
        if hasattr(client, "_for") and type(client).__name__ == "ModelRoutingChatClient":
            client = client._for(model)
            continue
        break
    return client


def default_root_for(client: Any, model: str) -> Optional[Root]:
    """The PolyKV root behind a chat client, or ``None`` when it has none
    (Ollama cloud, stock llama.cpp, llamafile, plain Ollama)."""
    from claude_hooks.get_advice import chat_client as cc
    real = _unwrap(client, model)
    base = getattr(real, "base_url", None)
    if not base:
        return None
    auth = ({"Authorization": f"Bearer {real.api_key}"}
            if getattr(real, "api_key", None) else None)
    if isinstance(real, cc.OpenAICompatChatClient):
        if getattr(real, "backend", "") != "opencoti":
            return None
        return Root(key=f"direct|{base}", kind="direct",
                    client=PolykvClient.direct(base, headers=auth))
    if isinstance(real, cc.ChatClient):
        from claude_hooks import provider_caps
        feats = provider_caps.probe_xollama(base, headers=auth)
        if not feats or "client_placement_v1" not in feats:
            return None
        served = real._real(model) if hasattr(real, "_real") else model
        return Root(key=f"xollama|{base}|{served}", kind="xollama",
                    client=PolykvClient.via_xollama(base, served, headers=auth))
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------ the manager

class KvRunManager:
    def __init__(self, run_id: str, *, mode: str = "auto",
                 root_for: Callable[[Any, str], Optional[Root]] = default_root_for,
                 show: Optional[Callable[[str, str], Optional[dict]]] = None,
                 min_layer_chars: int = MIN_LAYER_CHARS,
                 clock: Callable[[], float] = time.monotonic):
        if mode not in MODES:
            raise ValueError(f"polykv mode must be one of {MODES}")
        self.run_id = wire_id(run_id)
        self.mode = mode
        self._root_for = root_for
        if show is None:
            from claude_hooks import provider_caps
            show = provider_caps.probe_show
        self._show = show
        self.min_layer_chars = min_layer_chars
        self._clock = clock
        self._lock = threading.RLock()
        #: notified whenever a placed call ends (its window may be idle now)
        self._freed = threading.Condition(self._lock)
        self._layer_locks: dict[str, threading.Lock] = {}
        self.roots: dict[str, Root] = {}
        self._client_roots: dict[tuple, Optional[str]] = {}
        self.sessions: dict[str, str] = {}        # wire id -> root key
        self._busy: set[str] = set()
        self.stats: dict[str, RoleStats] = {}
        self.events: list[dict] = []
        self.released = False
        #: Root state from a snapshot, applied when the root is first seen.
        self._pending: dict[str, dict] = {}
        #: (root key, pool id) of every layer this run created
        self.created: list[tuple[str, str]] = []

    # ---------------------------------------------------------- probing
    def _root(self, client: Any, model: str) -> Optional[Root]:
        ck = (id(client), model)
        with self._lock:
            if ck in self._client_roots:
                rk = self._client_roots[ck]
                return self.roots.get(rk) if rk else None
        try:
            root = self._root_for(client, model)
        except Exception as e:  # noqa: BLE001 — a probe never fails a call
            log.info("polykv: no root for %s (%s)", model, e)
            root = None
        with self._lock:
            if root is None:
                self._client_roots[ck] = None
                return None
            fresh = root.key not in self.roots
            root = self.roots.setdefault(root.key, root)
            if fresh and root.key in self._pending:
                self._apply(root, self._pending.pop(root.key))
            self._client_roots[ck] = root.key
            return root

    def _settle_mode(self, root: Root, model: str) -> None:
        """Decide the root's mode from what the engine advertises."""
        now = self._clock()
        if root.settled or (root.probed_at and now - root.probed_at < REPROBE_S):
            return
        root.probed_at = now
        try:
            props = root.client.props() or {}
            boot = root.client.boot_id()
        except (OpencotiError, OSError) as e:
            # Behind xOllama the engine answers once its model is loaded:
            # the session id still helps, the pools come on a later call.
            root.mode = "affinity" if root.kind == "xollama" else "off"
            root.reason = f"engine not readable yet: {e}"
            return
        feats = set(str(f) for f in (props.get("features") or []))
        pools_on = bool(((props.get("opencoti") or {}).get("polykv") or {})
                        .get("pools_enabled"))
        if root.kind == "xollama":
            show = self._show(root.client.transport.base,
                              root.client.transport.model) or {}
            sess = (show.get("xollama") or {}).get("session") or {}
            council = ((show.get("xollama") or {}).get("council") or {}).get("enabled")
            if council:
                root.mode, root.reason = "off", "an xOllama council model owns its pools"
                return
            if int(sess.get("client_pools") or 0) < MIN_CLIENT_POOLS:
                pools_on = False
                root.reason = "no xOllama client-pool seats for this model"
        wanted = self.mode
        if wanted == "assured":
            wanted = "pooled"
        can_pool = pools_on and "polykv_shared_root_v1" in feats
        if wanted in ("pooled", "auto"):
            root.mode = "pooled" if can_pool else "affinity"
            if not can_pool and not root.reason:
                root.reason = "the engine has no shared pools"
        else:
            root.mode = wanted
        if boot and root.boot_id and boot != root.boot_id:
            self._new_generation(root, boot)
        root.boot_id = boot or root.boot_id
        root.features = frozenset(feats)
        root.settled = True
        self.events.append({"kind": "root", "root": root.key, "mode": root.mode,
                            "reason": root.reason, "boot_id": root.boot_id})
        log.info("polykv: %s mode=%s%s", root.key, root.mode,
                 f" ({root.reason})" if root.reason else "")

    def _new_generation(self, root: Root, boot: str) -> None:
        log.warning("polykv: %s restarted (boot %s -> %s); its pools are gone",
                    root.key, root.boot_id, boot)
        root.generation += 1
        root.pools.clear()
        root.rebuilds.clear()
        root.boot_id = boot
        self.events.append({"kind": "restart", "root": root.key,
                            "generation": root.generation})

    # -------------------------------------------------------- identities
    def _identity(self, role: str, model: str, messages: list) -> str:
        sys_turns = [m.get("content") for m in messages
                     if m.get("role") == "system"]
        first_user = next((m.get("content") for m in messages
                           if m.get("role") == "user"), "")
        h = hashlib.sha256(json.dumps([role, model, sys_turns, first_user],
                                      default=str).encode()).hexdigest()[:10]
        return h

    def session_for(self, role: str, model: str, messages: list,
                    root_key: str) -> str:
        base = wire_id(f"{self.run_id}~{role}~{self._identity(role, model, messages)}")
        with self._lock:
            sid, n = base, 1
            while sid in self._busy:
                n += 1
                sid = f"{base}~{n}"
            self._busy.add(sid)
            self.sessions.setdefault(sid, root_key)
            return sid

    def _done(self, sid: str) -> None:
        with self._lock:
            self._busy.discard(sid)
            self._freed.notify_all()

    # ------------------------------------------------------------ layers
    @staticmethod
    def _wire_messages(client: Any, model: str, messages: list) -> list:
        from claude_hooks.get_advice import chat_client as cc
        real = _unwrap(client, model)
        if isinstance(real, cc.OpenAICompatChatClient):
            return cc.single_system_messages(list(messages))
        return cc._ollama_messages(list(messages))

    def _layer(self, root: Root, client: Any, model: str, messages: list,
               tools: Optional[list]) -> Optional[str]:
        wire_msgs = self._wire_messages(client, model, messages)
        head = []
        for m in wire_msgs:
            if m.get("role") != "system":
                break
            head.append(m)
        if not head:
            return None
        key = hashlib.sha256(json.dumps([head, tools or []], default=str,
                                        sort_keys=True).encode()).hexdigest()[:16]
        with self._lock:
            if key in root.given_up:
                return None
            if key in root.pools:
                return root.pools[key]
            lk = self._layer_locks.setdefault(f"{root.key}|{key}", threading.Lock())
        with lk:
            with self._lock:
                if key in root.pools:
                    return root.pools[key]
            try:
                cut = root.client.render_chat(
                    head + [{"role": "user", "content": SENTINEL}], tools=tools)
                at = cut.find(SENTINEL)
                if at < 0:
                    raise ValueError("the template dropped the sentinel turn")
                cut = cut[:at]
                if len(cut) < self.min_layer_chars:
                    with self._lock:
                        root.given_up.add(key)
                    return None
                full = root.client.render_chat(wire_msgs, tools=tools)
                if not full.startswith(cut):
                    self._give_up(root, key, "the head is not a byte prefix "
                                  "of the full rendering")
                    return None
                # Pinned while the run uses it: the engine sweeps an
                # unpinned, unreferenced pool during any pause (live: the
                # layer went between two lanes and the next attach was
                # charged in full). Unpinned or released at settle.
                made = root.client.create_pool(prompt=cut, shared=True,
                                               ephemeral=True, pin=True)
            except (OpencotiError, OSError, ValueError) as e:
                # Share less, never fail: the call goes unpooled.
                self._give_up(root, key, f"layer refused: {e}",
                              retry=isinstance(e, OpencotiError) and
                              getattr(e, "status", 0) in (429, 503))
                return None
            pid = pool_ref(made)
            if pid is None:
                self._give_up(root, key, "create answered no pool id")
                return None
            cells = _int((made or {}).get("own_len"))
            with self._lock:
                root.pools[key] = pid
                self.created.append((root.key, pid))
                if cells:
                    root.sizes[pid] = (cells, len(cut))
            self.events.append({"kind": "layer", "root": root.key, "key": key,
                                "pool_id": pid, "chars": len(cut),
                                "reused": bool((made or {}).get("reused"))})
            return pid

    def _private_window(self, root: Root, pid: str, client: Any,
                        payload: dict) -> Optional[int]:
        """The window to state for a call attaching layer ``pid``: its
        private tokens (the prompt past the layer) plus its output.

        With no window stated the engine charges the whole prompt again
        on top of the pool (live: 26,775 cells asked with a 16,603-cell
        pool, 16,165 free, every lane refused); a stated ``num_ctx``
        counts only private tokens. The prompt past the layer is not
        rendered per call, so it is estimated from its characters at the
        layer's own chars-per-cell, with :data:`PRIVATE_MARGIN` on top —
        an estimate short of the real count is refused (400), never cut."""
        size = root.sizes.get(pid)
        if not size:
            return None
        cells, chars = size
        msgs = self._wire_messages(client, payload.get("model") or "",
                                   list(payload.get("messages") or []))
        rest = msgs[next((i for i, m in enumerate(msgs)
                          if m.get("role") != "system"), len(msgs)):]
        rest_chars = sum(len(str(m.get("content") or ""))
                         + len(json.dumps(m.get("tool_calls") or "", default=str))
                         + TURN_OVERHEAD_CHARS for m in rest)
        opts = payload.get("options") or {}
        out = _int(opts.get("num_predict")) or _int(payload.get("max_tokens"))
        if not out:
            return None          # no stated output: let the engine size it
        private = int(rest_chars * cells / max(chars, 1) * PRIVATE_MARGIN)
        need = private + PRIVATE_FLOOR + out
        return -(-need // WINDOW_ALIGN) * WINDOW_ALIGN

    def _give_up(self, root: Root, key: str, why: str, *, retry: bool = False) -> None:
        log.warning("polykv: %s layer %s: %s", root.key, key, why)
        self.events.append({"kind": "layer_refused", "root": root.key,
                            "key": key, "why": why})
        if not retry:
            with self._lock:
                root.given_up.add(key)

    # ------------------------------------------------------------- place
    def place(self, client: Any, payload: dict, *, role: str) -> Optional[dict]:
        """The ``placement`` for one call, or ``None`` (send it as is)."""
        if self.mode == "off" or self.released:
            return None
        model = payload.get("model") or ""
        root = self._root(client, model)
        if root is None:
            return None
        self._settle_mode(root, model)
        if root.mode == "off":
            return None
        messages = list(payload.get("messages") or [])
        placement: dict = {"session_id": self.session_for(role, model, messages,
                                                           root.key)}
        if root.mode == "pooled":
            pid = self._layer(root, client, model, messages,
                              payload.get("tools") or None)
            if pid is not None:
                placement["pool_id"] = pid
                placement["_layer_root"] = root.key
                window = self._private_window(root, pid, client, payload)
                if window:
                    placement["num_ctx"] = window
        if root.kind == "direct" and "stream_keepalive_v1" in root.features:
            placement["keepalive"] = True
            placement["return_progress"] = True
        return placement

    def observe(self, placement: Optional[dict], out: Optional[dict], *,
                role: str, error: Optional[BaseException] = None) -> None:
        """Read what the engine said about a placed call."""
        st = self.stats.setdefault(role, RoleStats())
        st.calls += 1
        if not placement:
            return
        self._done(placement.get("session_id") or "")
        st.placed += 1
        if error is not None or not out:
            return
        rec = ((out.get("timings") or {}).get("polykv")) or {}
        usage = out.get("usage") or {}
        st.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        st.cached_tokens += int(rec.get("cache_n") or 0)
        if placement.get("pool_id") is not None:
            st.pooled += 1
            st.shared_tokens += int(rec.get("n_pool_shared") or rec.get("cache_n") or 0)
        root = self.roots.get(self.sessions.get(placement.get("session_id") or "", ""))
        boot = rec.get("boot_id")
        if root is not None and boot and root.boot_id and boot != root.boot_id:
            with self._lock:
                self._new_generation(root, boot)
        if rec.get("warnings"):
            st.warnings += len(rec["warnings"])
            if root is not None and placement.get("pool_id") is not None:
                self._drop_pool(root, str(placement["pool_id"]), rec["warnings"][0])

    def make_room(self, placement: Optional[dict], *,
                  wait_s: Optional[float] = None) -> Optional[bool]:
        """Before a call that states a window: if the engine has fewer free
        cells than it asks, close this run's idle sessions; if that is not
        enough while sibling lanes of this run are mid-call, wait for one
        to end (at most ``wait_s``) and try again. ``True`` when the engine
        was seen to have the room, ``False`` when it has not, ``None`` when
        there is nothing to judge (no stated window, an unreadable ``/kv``). Lanes of one run queue on each other this way instead of
        each spending the chat client's Retry-After loop (live: 60 s per
        lane) and then failing."""
        window = _int((placement or {}).get("num_ctx"))
        sid = (placement or {}).get("session_id") or ""
        with self._lock:
            rk = self.sessions.get(sid)
            root = self.roots.get(rk or "")
        if not window or root is None or root.client is None:
            return None
        deadline = self._clock() + (WAIT_FOR_ROOM_S if wait_s is None else wait_s)
        waited = False
        while True:
            try:
                free = _int((root.client.kv(live=True) or {}).get("cells_free"))
            except (OpencotiError, OSError):
                return None              # unreadable: let the engine decide
            if free >= window:
                if waited:
                    self.events.append({"kind": "room", "root": rk,
                                        "waited": True, "window": window})
                return True
            if self.reclaim_idle(placement):
                continue
            with self._lock:
                others = [s for s in self._busy
                          if s != sid and self.sessions.get(s) == rk]
                left = deadline - self._clock()
                if not others or left <= 0:
                    self.events.append({"kind": "no_room", "root": rk,
                                        "free": free, "window": window})
                    return False
                waited = True
                self._freed.wait(timeout=min(left, ROOM_POLL_S))

    def reclaim_idle(self, placement: Optional[dict]) -> int:
        """Close this run's idle sessions on the root ``placement`` went to.

        A worker session keeps its window booked after its call ends, so a
        lane that finished holds cells the next one is refused for (live:
        a 16,603-cell layer plus one finished lane's 10,496 left 5,669 of
        32,768 free, and every later lane got a 429). An idle session's
        KV is only a cache: closing it costs that lane a re-prefill of its
        private tokens if it calls again, never the shared layer."""
        if not placement:
            return 0
        sid = placement.get("session_id") or ""
        with self._lock:
            rk = self.sessions.get(sid)
            root = self.roots.get(rk or "")
            idle = [s for s, k in self.sessions.items()
                    if k == rk and s not in self._busy]   # the caller is busy
        if root is None or root.client is None or not idle:
            return 0
        n = 0
        for s in idle:
            try:
                n += int(root.client.close_session(s).found)
            except (OpencotiError, OSError) as e:
                log.info("polykv: reclaim %s: %s", s, e)
        self.events.append({"kind": "reclaimed", "root": rk, "sessions": n})
        return n

    def layer_lost(self, placement: Optional[dict]) -> bool:
        """Forget the layer a refused call attached; ``True`` when there
        was one (the next :meth:`place` builds it again)."""
        pid = (placement or {}).get("pool_id")
        root = self.roots.get((placement or {}).get("_layer_root") or "")
        if pid is None or root is None:
            return False
        self._drop_pool(root, str(pid), "the engine no longer has the layer")
        return True

    def _drop_pool(self, root: Root, pid: str, why: str) -> None:
        with self._lock:
            key = next((k for k, v in root.pools.items() if v == pid), None)
            if key is None:
                return
            del root.pools[key]
            root.rebuilds[key] = root.rebuilds.get(key, 0) + 1
            if root.rebuilds[key] >= MAX_REBUILDS:
                root.given_up.add(key)
        self.events.append({"kind": "pool_dropped", "root": root.key,
                            "pool_id": pid, "why": why})

    # ----------------------------------------------------------- release
    def release_all(self) -> dict:
        """Close every session this run used, then audit the engines for
        anything still carrying the run's prefix. Shared roots are left to
        the engine's ephemeral sweep: another council may be attached."""
        with self._lock:
            if self.released:
                return {"released": True, "already": True}
            self.released = True
            sessions = dict(self.sessions)
        report: dict = {"closed": 0, "found": 0, "close_errors": [],
                        "leaked_allocations": [], "leaked_pools": [],
                        "stats": {r: s.as_dict() for r, s in self.stats.items()}}
        for sid, rk in sessions.items():
            root = self.roots.get(rk)
            if root is None or root.client is None:
                continue
            try:
                r = root.client.close_session(sid)
                report["closed"] += 1
                report["found"] += int(r.found)
            except (OpencotiError, OSError) as e:
                report["close_errors"].append(f"{sid}: {e}")
        # A layer nobody attached is never swept (the engine sweeps an
        # ephemeral pool only once it has been attached): release it here.
        # One that was attached is unpinned and left to the sweep — another
        # council may share it.
        report["released_pools"] = []
        report["unpinned_pools"] = []
        mine: dict[str, set] = {}
        for rk, pid in list(self.created):
            root = self.roots.get(rk)
            if root is None or root.client is None:
                continue
            mine.setdefault(rk, set()).add(str(pid))
            try:
                if (root.client.pool(pid) or {}).get("attached_ever"):
                    root.client.pin_pool(pid, False)
                    report["unpinned_pools"].append(pid)
                    continue
                root.client.release_pool(pid)
                report["released_pools"].append(pid)
            except (OpencotiError, OSError) as e:
                if getattr(e, "status", None) != 404:
                    report["close_errors"].append(f"pool {pid}: {e}")
        for root in self.roots.values():
            if root.client is None or root.mode == "off":
                continue
            try:
                kv = root.client.kv(live=True) or {}
                report["leaked_allocations"] += [
                    a.get("session_id") for a in kv.get("allocations") or []
                    if str(a.get("session_id") or "").startswith(self.run_id + "~")]
                pools = root.client.pools(live=True) or {}
                report["leaked_pools"] += [
                    p.get("pool_id") for p in pools.get("pools") or []
                    if self._leaked(root, p, mine.get(root.key, ()))]
            except (OpencotiError, OSError) as e:
                report["close_errors"].append(f"audit {root.key}: {e}")
        if report["leaked_allocations"] or report["leaked_pools"]:
            log.warning("polykv: run %s LEAKED=%s %s", self.run_id,
                        report["leaked_allocations"], report["leaked_pools"])
        report["roots"] = {k: {"mode": r.mode, "reason": r.reason,
                               "generation": r.generation,
                               "layers": len(r.pools)}
                           for k, r in self.roots.items()}
        return report

    def _leaked(self, root: Root, listed: dict, mine) -> bool:
        """A listed pool this run should have let go: one it owns, or one
        it made that is still pinned or was never attached. The engine's
        list keeps pools it has dropped (live: pool 9 listed after
        ``GET /polykv/pools/9`` answered "Invalid pool ID"), so a candidate
        is asked about by id before it counts."""
        pid = str(listed.get("pool_id"))
        if not (str(listed.get("owner") or listed.get("source_session") or "")
                .startswith(self.run_id + "~") or pid in mine):
            return False
        try:
            p = root.client.pool(pid) or {}
        except OpencotiError as e:
            if e.status in (400, 404):
                return False             # gone
            raise
        owned = str(p.get("owner") or p.get("source_session") or "") \
            .startswith(self.run_id + "~")
        return owned or bool(p.get("pinned")) or not p.get("attached_ever")

    # -------------------------------------------------------- durability
    def snapshot(self) -> dict:
        with self._lock:
            return {"run_id": self.run_id, "mode": self.mode,
                    "roots": {k: {"kind": r.kind, "mode": r.mode,
                                  "boot_id": r.boot_id,
                                  "generation": r.generation,
                                  "pools": dict(r.pools),
                                  "sizes": {p: list(v) for p, v in r.sizes.items()}}
                              for k, r in self.roots.items()},
                    "sessions": dict(self.sessions),
                    "created": [list(c) for c in self.created]}

    def restore(self, snap: dict) -> None:
        """Take back sessions and pool refs from :meth:`snapshot`. A root
        not seen yet gets its state when it is (roots are made lazily, on
        the first call that reaches them). Pools come back with the
        ``boot_id`` they were made under; the first probe after a resume
        drops them if the engine restarted in between."""
        with self._lock:
            self.sessions.update(snap.get("sessions") or {})
            self.created += [tuple(c) for c in snap.get("created") or []
                             if tuple(c) not in self.created]
            for k, r in (snap.get("roots") or {}).items():
                root = self.roots.get(k)
                if root is None:
                    self._pending[k] = r
                else:
                    self._apply(root, r)

    @staticmethod
    def _apply(root: Root, r: dict) -> None:
        root.boot_id = r.get("boot_id")
        root.generation = int(r.get("generation") or 0)
        root.pools.update(r.get("pools") or {})
        root.sizes.update({p: tuple(v) for p, v in (r.get("sizes") or {}).items()})


# -------------------------------------------------------------- registry

_RUNS: dict[str, KvRunManager] = {}
_RUNS_LOCK = threading.Lock()


def register(mgr: KvRunManager) -> KvRunManager:
    with _RUNS_LOCK:
        _RUNS[mgr.run_id] = mgr
    return mgr


def manager_for(run_id: Optional[str]) -> Optional[KvRunManager]:
    if not run_id:
        return None
    with _RUNS_LOCK:
        return _RUNS.get(wire_id(run_id))


def drop(run_id: str) -> Optional[KvRunManager]:
    with _RUNS_LOCK:
        return _RUNS.pop(wire_id(run_id), None)


# --------------------------------------------------------- run lifecycle

SNAPSHOT_NAME = "polykv.json"


def snapshot_path(cwd: str, sid: str):
    from pathlib import Path
    return Path(cwd) / ".claude-hooks" / "consultants" / sid / SNAPSHOT_NAME


def start_run(sid: str, *, mode: str, cwd: str, resume: bool = False,
              **kw) -> KvRunManager:
    """A run's manager, registered; a resumed run takes back its snapshot."""
    mgr = register(KvRunManager(sid, mode=mode, **kw))
    if resume:
        p = snapshot_path(cwd, sid)
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            snap = None
        if isinstance(snap, dict) and snap.get("suspended"):
            mgr.restore(snap)
            log.info("polykv: run %s resumed with %d session(s)", sid,
                     len(snap.get("sessions") or {}))
    return mgr


def settle_run(sid: str, *, cwd: str, status: str) -> Optional[dict]:
    """End a run's placement: a suspended run keeps its sessions and pools
    and writes a snapshot for the resume (the engine's own TTL is the net
    if no resume comes); any other end releases everything and writes the
    release report. Never raises."""
    mgr = drop(sid)
    if mgr is None:
        return None
    try:
        if status == "suspended":
            out = {**mgr.snapshot(), "suspended": True}
        else:
            out = {**mgr.release_all(), "run_id": mgr.run_id, "status": status,
                   "events": mgr.events[-200:]}
        p = snapshot_path(cwd, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        return out
    except Exception:  # noqa: BLE001 — settling never fails a run
        log.exception("polykv: settling run %s failed", sid)
        return None


def placed(client: Any, mgr: Optional[KvRunManager], role: str) -> Any:
    """``client`` placed by ``mgr``, or ``client`` itself when there is
    none (PolyKV off: nothing wraps anything)."""
    if mgr is None:
        return client
    inner = getattr(client, "_placed_inner", client)
    return PlacedChat(inner, mgr, role=role)


def _admission_refused(e: BaseException) -> bool:
    """The engine refused the call for room (429), not for its content."""
    return getattr(e, "status", None) == 429 or (
        "HTTP 429" in str(e) and "admission" in str(e))


def _layer_gone(e: BaseException, placement: dict) -> bool:
    """A call that attached a layer was refused for its size with no share
    counted: the engine no longer has the pool. It names the share when it
    does ("…, N of them shareable from pool P"), and a missing pool id is
    not an error there — the request is just charged in full."""
    text = str(e)
    return (placement.get("pool_id") is not None
            and getattr(e, "fault_class", "") == "context_overflow"
            and "shareable" not in text)


# ------------------------------------------------------- the chat wrapper

class PlacedChat:
    """A role's chat client with its calls placed by a run's manager.
    Everything else is the inner client's (``__getattr__``)."""

    def __init__(self, client: Any, manager: KvRunManager, *, role: str):
        self._placed_inner = client
        self._placed_manager = manager
        self._placed_role = role

    def __getattr__(self, name: str) -> Any:
        return getattr(self._placed_inner, name)

    def _call(self, method: str, payload: dict, *args, **kwargs) -> dict:
        mgr = self._placed_manager
        try:
            placement = mgr.place(self._placed_inner, payload,
                                  role=self._placed_role)
        except Exception:  # noqa: BLE001 — placement never fails a call
            log.exception("polykv: placement failed; sending unplaced")
            placement = None
        if not placement:
            out = getattr(self._placed_inner, method)(payload, *args, **kwargs)
            mgr.observe(None, out, role=self._placed_role)
            return out
        sent = {**payload, wire.KEY: {k: v for k, v in placement.items()
                                      if not k.startswith("_")}}
        try:
            mgr.make_room(placement)
        except Exception:  # noqa: BLE001 — room is advice, never a failure
            log.exception("polykv: make_room failed; sending as is")
        try:
            try:
                out = getattr(self._placed_inner, method)(sent, *args, **kwargs)
            except Exception as e:  # noqa: BLE001 — re-raised unless recovered
                if _admission_refused(e) and (mgr.reclaim_idle(placement)
                                              or mgr.make_room(placement)):
                    # Refused for room: this run's idle lanes freed theirs,
                    # or a sibling ended while this one waited.
                    pass
                elif _layer_gone(e, placement) and mgr.layer_lost(placement):
                    mgr.observe(placement, None, role=self._placed_role, error=e)
                    placement = mgr.place(self._placed_inner, payload,
                                          role=self._placed_role) or {}
                    sent = {**payload, wire.KEY: {
                        k: v for k, v in placement.items()
                        if not k.startswith("_")}} if placement else payload
                else:
                    raise
                out = getattr(self._placed_inner, method)(sent, *args, **kwargs)
        except BaseException as e:
            mgr.observe(placement, None, role=self._placed_role, error=e)
            raise
        mgr.observe(placement, out, role=self._placed_role)
        return out

    def chat(self, payload: dict, *args, **kwargs) -> dict:
        return self._call("chat", payload, *args, **kwargs)

    def chat_streamed(self, payload: dict, *args, **kwargs) -> dict:
        return self._call("chat_streamed", payload, *args, **kwargs)


__all__ = ["KvRunManager", "MODES", "PlacedChat", "Root", "default_root_for",
           "drop", "manager_for", "placed", "register", "settle_run",
           "snapshot_path", "start_run"]
