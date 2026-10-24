"""PolyKV control-plane client (plan F1), stdlib only.

Ported from opencoti's ``opencoti_langgraph/client.py`` (its payload
builders, error mapping and :class:`Capacity`; not imported, since it pins
a newer langchain/langgraph and httpx), and extended to the whole surface
``docs/research/cerebriline-map/03-polykv.md`` §1 maps:

- pools: create / fork with an owner ``session_id``, ``shared`` (find or
  create an unowned root), ``unowned``, ``tokens`` / ``from_session`` /
  ``from_slot`` / ``expect_len``, copy-on-write ``branch_pos``; pin,
  unpin, release (``DELETE`` alias), listing with ``?live=1``;
- capacity (with the 0338 window fields), admission policy, ``/kv``,
  ``/polykv/tps``, ``/metrics``, ``/elastic``, ``/polykv/sampling``;
- sessions: close (``keep_kv`` / ``keep_running``, the body form for any
  id the path route cannot carry, ``found`` read back), resize, slot
  erase;
- prompt building: ``/apply-template`` (``continue_pool`` too) and
  ``/tokenize`` with the serving tokenizer's ``add_special``;
- ``boot_id`` from ``/health``.

Two transports share every method. **Direct** talks to the engine's own
routes. **xOllama** goes through ``/api/engine?model=M&endpoint=<route>``
and unwraps its ``{status, body|text}`` envelope, so the engine's status
is the one a caller sees (a 404 inside a 200 envelope is a 404).

Pool ids are handed back and accepted as **strings**: pool 0 is a real
pool, and ``if pool_id`` drops the first pool of every server.

:func:`observe` reads what a completion says about its placement
(``X-Context-Window``, ``X-OpenCoti-Boot-Id``, the ``opencoti`` block) and
warns when a request named a pool and shared nothing — the miss
Cerebriline still reports as success.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Iterable, Optional, Union

log = logging.getLogger("claude_hooks.polykv")

READ_TIMEOUT_S = 30.0
#: Pool creates prefill the prefix; a long grounding takes minutes.
CONTROL_TIMEOUT_S = 600.0

#: What the engine and xOllama accept in a path segment
#: (``api/xollama`` id rule); anything else goes through a body form.
_ID_OK = re.compile(r"^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,255}$")
_ID_BAD_CHAR = re.compile(r"[^A-Za-z0-9._~-]")

PoolId = Union[str, int]


# ----------------------------------------------------------------- errors

class OpencotiError(RuntimeError):
    """The engine answered with an error status."""

    def __init__(self, message: str, status: Optional[int] = None,
                 body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class PoolSaturatedError(OpencotiError):
    """A 429: an admission or window refusal, or a worker whose owner's
    window is full. ``worker_full`` is a queue, never global pressure:
    wait ``retry_after`` (waiting workers hold nothing)."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 largest_admissible: Optional[int] = None,
                 pressure: Any = None, worker_full: bool = False,
                 body: Any = None):
        super().__init__(message, status=429, body=body)
        self.retry_after = retry_after
        self.largest_admissible = largest_admissible
        self.pressure = pressure
        self.worker_full = worker_full


# -------------------------------------------------------------- transport

@dataclass
class Reply:
    status: int
    body: Any = None
    text: str = ""
    #: Header names lower-cased.
    headers: dict = field(default_factory=dict)


Fetch = Callable[..., Reply]


def http_fetch(method: str, url: str, *, data: Any = None,
               headers: Optional[dict] = None,
               timeout: float = READ_TIMEOUT_S) -> Reply:
    """One HTTP call. Raises only on a network failure (``OSError``), so a
    caller can tell "the engine said no" from "the engine is not there"."""
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
            got = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = e.code
        got = {k.lower(): v for k, v in (e.headers or {}).items()}
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        body = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = None
    return Reply(status=status, body=body, text=text, headers=got)


def _query(params: Optional[dict]) -> str:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


class DirectTransport:
    """The engine's own routes at ``root`` (a trailing ``/v1`` dropped)."""

    kind = "direct"

    def __init__(self, root: str, *, fetch: Fetch = http_fetch,
                 headers: Optional[dict] = None):
        self.root = re.sub(r"/v1/?$", "", root.rstrip("/"))
        self._fetch = fetch
        self._headers = dict(headers or {})

    def call(self, method: str, route: str, *, data: Any = None,
             params: Optional[dict] = None, timeout: float) -> Reply:
        url = f"{self.root}/{route.lstrip('/')}{_query(params)}"
        return self._fetch(method, url, data=data, headers=self._headers,
                           timeout=timeout)


class XollamaTransport:
    """The engine behind xOllama's ``/api/engine`` passthrough for one
    loaded ``model``. Inference is not proxied there; control and reads
    are. The envelope's ``status`` is the engine's."""

    kind = "xollama"

    def __init__(self, base_url: str, model: str, *, fetch: Fetch = http_fetch,
                 headers: Optional[dict] = None):
        self.base = base_url.rstrip("/")
        self.model = model
        self._fetch = fetch
        self._headers = dict(headers or {})

    def call(self, method: str, route: str, *, data: Any = None,
             params: Optional[dict] = None, timeout: float) -> Reply:
        q = {"model": self.model, "endpoint": route.lstrip("/"), **(params or {})}
        outer = self._fetch(method, f"{self.base}/api/engine{_query(q)}",
                            data=data, headers=self._headers, timeout=timeout)
        env = outer.body
        if not isinstance(env, dict) or "status" not in env:
            # xOllama itself refused (unknown model, not an engine model).
            return outer
        inner = env.get("body")
        text = env.get("text") or (json.dumps(inner) if inner is not None else "")
        try:
            status = int(env["status"])
        except (TypeError, ValueError):
            status = outer.status
        return Reply(status=status, body=inner, text=text, headers=outer.headers)


Transport = Union[DirectTransport, XollamaTransport]


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_message(reply: Reply) -> str:
    body = reply.body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if body.get("message"):
            return str(body["message"])
    return (reply.text or "")[:500]


def raise_for(reply: Reply) -> Any:
    """The body of a success; a typed error otherwise (from
    ``opencoti_langgraph._raise_for_response``, with the window fields)."""
    if reply.status == 429:
        msg = _error_message(reply)
        body = reply.body if isinstance(reply.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        largest = (err.get("largest_admissible") or body.get("largest_admissible")
                   or reply.headers.get("x-context-largest-admissible"))
        raise PoolSaturatedError(
            f"admission refused: {msg}",
            retry_after=_num(reply.headers.get("retry-after")),
            largest_admissible=int(_num(largest)) if _num(largest) is not None else None,
            pressure=err.get("pressure", body.get("pressure")),
            worker_full="worker of" in msg,
            body=reply.body)
    if reply.status >= 400:
        raise OpencotiError(
            f"opencoti answered {reply.status}: {_error_message(reply)}",
            status=reply.status, body=reply.body)
    return reply.body if reply.body is not None else {}


# --------------------------------------------------------------- identity

def wire_id(text: str) -> str:
    """A session id every route can carry: ``[A-Za-z0-9._~-]``, at most
    256 characters, not starting with ``.`` (Cerebriline maps the rest to
    ``~``: ``/`` in an id 404ed the path close and left the window held)."""
    s = _ID_BAD_CHAR.sub("~", text or "")[:256]
    return ("~" + s[1:]) if s.startswith(".") else (s or "~")


def pool_ref(created: Any) -> Optional[str]:
    """The pool id of a create/fork answer, as a string (pool 0 is real)."""
    if isinstance(created, dict) and created.get("pool_id") is not None:
        return str(created["pool_id"])
    return None


# --------------------------------------------------------------- capacity

@dataclass
class Capacity:
    """``GET /polykv/pools/{id}/capacity``. Fields as opencoti_langgraph's
    ``Capacity``, plus the 0338 window view (``pressure``,
    ``window_cells``, ``pressure_scope``, ``owner``): on an owned pool the
    ``kv_*`` fields describe the owner's window, not the server."""

    pool_id: str
    can_admit: bool
    reason: str = ""
    projected_mean_tps_if_admitted: Optional[float] = None
    mean_active_tps: Optional[float] = None
    n_active_sessions: int = 0
    headroom_sessions: int = -1
    kv_headroom_pct: float = 0.0
    kv_cells_free: int = 0
    kv_cells_total: int = 0
    marginal_tokens_mean: int = 0
    compaction_pressure: float = 0.0
    expected_tokens: Optional[int] = None
    admission: dict = field(default_factory=dict)
    settling: bool = False
    settle_remaining_ms: int = 0
    guaranteed: bool = False
    n_warming: int = 0
    n_pool_sessions: int = 0
    projected_mean_tps_model: Optional[float] = None
    projected_mean_tps_measured: Optional[float] = None
    drop_per_admit_ewma: Optional[float] = None
    known_session: bool = False
    n_prefilling: int = 0
    prefill_backlog_tokens: int = 0
    n_soon_sessions: int = 0
    slots_live: Optional[int] = None
    slots_max: Optional[int] = None
    slots_free: Optional[int] = None
    vram_free_mib: Optional[float] = None
    elastic_reason: str = ""
    swa_active: bool = False
    swa_cells_total: Optional[int] = None
    swa_cells_free: Optional[int] = None
    swa_cells_needed: Optional[int] = None
    swa_seq_budget: Optional[int] = None
    swa_window: Optional[int] = None
    swa_capacity_sessions: Optional[int] = None
    folded: bool = False
    pressure: Optional[float] = None
    window_cells: Optional[int] = None
    pressure_scope: str = ""
    owner: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict) -> "Capacity":
        known = {f.name for f in fields(cls)} - {"raw", "pool_id", "can_admit"}
        kw = {k: d[k] for k in known if k in d and d[k] is not None}
        return cls(pool_id=str(d["pool_id"]), can_admit=bool(d["can_admit"]),
                   raw=d, **kw)


# --------------------------------------------------------- payload builders

def create_body(*, prompt: Optional[str] = None,
                tokens: Optional[list] = None,
                from_session: Optional[str] = None,
                from_slot: Optional[int] = None,
                session_id: Optional[str] = None,
                pin: bool = False, ephemeral: Optional[bool] = None,
                expect_len: Optional[int] = None, shared: bool = False,
                unowned: bool = False) -> dict:
    sources = [x for x in (prompt, tokens, from_session, from_slot)
               if x is not None]
    if len(sources) != 1:
        raise ValueError("a pool needs exactly one of prompt / tokens / "
                         "from_session / from_slot")
    if shared and session_id:
        raise ValueError("a shared root has no owner (the engine 400s)")
    if unowned and session_id:
        raise ValueError("an unowned pool takes no session_id (the engine 400s)")
    body: dict[str, Any] = {}
    for key, val in (("prompt", prompt), ("tokens", tokens),
                     ("from_session", from_session), ("from_slot", from_slot),
                     ("session_id", session_id), ("expect_len", expect_len),
                     ("ephemeral", ephemeral)):
        if val is not None:
            body[key] = val
    if pin:
        body["pin"] = True
    if shared:
        body["shared"] = True
    if unowned:
        body["unowned"] = True
    return body


def fork_body(*, branch_pos: Optional[int] = None,
              prompt: Optional[str] = None, tokens: Optional[list] = None,
              from_session: Optional[str] = None,
              from_slot: Optional[int] = None,
              session_id: Optional[str] = None, pin: bool = False,
              ephemeral: Optional[bool] = None,
              unowned: bool = False) -> dict:
    """``prompt``/``tokens`` are the child's **full** prefix ``[0, L)``,
    checked token-exact against the parent over ``[0, branch_pos)``.
    ``branch_pos`` below the parent's length is a copy-on-write branch
    (attention-only models)."""
    if unowned and session_id:
        raise ValueError("an unowned pool takes no session_id (the engine 400s)")
    if branch_pos is not None and branch_pos < 0:
        raise ValueError("branch_pos must be >= 0")
    body: dict[str, Any] = {}
    for key, val in (("branch_pos", branch_pos), ("prompt", prompt),
                     ("tokens", tokens), ("from_session", from_session),
                     ("from_slot", from_slot), ("session_id", session_id),
                     ("ephemeral", ephemeral)):
        if val is not None:
            body[key] = val
    if pin:
        body["pin"] = True
    if unowned:
        body["unowned"] = True
    return body


def admission_body(*, target_tps_per_session: Optional[float] = None,
                   mode: Optional[str] = None,
                   on_saturation: Optional[str] = None,
                   guarantee_min_sessions: Optional[int] = None,
                   settle_tokens: Optional[int] = None,
                   settle_max_ms: Optional[int] = None,
                   prefill_max_slots: Optional[int] = None) -> dict:
    if mode is not None and mode not in ("advisory", "enforced"):
        raise ValueError("mode must be 'advisory' or 'enforced'")
    if on_saturation is not None and on_saturation not in ("reject", "warn"):
        raise ValueError("on_saturation must be 'reject' or 'warn'")
    for name, val in (("guarantee_min_sessions", guarantee_min_sessions),
                      ("prefill_max_slots", prefill_max_slots)):
        if val is not None and val < 0:
            raise ValueError(f"{name} must be >= 0")
    body = {k: v for k, v in (
        ("target_tps_per_session", target_tps_per_session), ("mode", mode),
        ("on_saturation", on_saturation),
        ("guarantee_min_sessions", guarantee_min_sessions),
        ("settle_tokens", settle_tokens), ("settle_max_ms", settle_max_ms),
        ("prefill_max_slots", prefill_max_slots)) if v is not None}
    if not body:
        raise ValueError("an admission policy needs at least one field")
    return body


# ----------------------------------------------------------------- client

@dataclass
class CloseResult:
    session_id: str
    found: bool
    released: Any = None
    deferred: bool = False
    raw: dict = field(default_factory=dict, repr=False)


class PolykvClient:
    """One engine's control plane, over either transport."""

    def __init__(self, transport: Transport, *,
                 read_timeout: float = READ_TIMEOUT_S,
                 control_timeout: float = CONTROL_TIMEOUT_S):
        self.transport = transport
        self.read_timeout = read_timeout
        self.control_timeout = control_timeout

    @classmethod
    def direct(cls, root: str, **kw) -> "PolykvClient":
        tkw = {k: kw.pop(k) for k in ("fetch", "headers") if k in kw}
        return cls(DirectTransport(root, **tkw), **kw)

    @classmethod
    def via_xollama(cls, base_url: str, model: str, **kw) -> "PolykvClient":
        tkw = {k: kw.pop(k) for k in ("fetch", "headers") if k in kw}
        return cls(XollamaTransport(base_url, model, **tkw), **kw)

    def _get(self, route: str, params: Optional[dict] = None) -> Any:
        return raise_for(self.transport.call(
            "GET", route, params=params, timeout=self.read_timeout))

    def _post(self, route: str, data: Optional[dict] = None,
              params: Optional[dict] = None) -> Any:
        return raise_for(self.transport.call(
            "POST", route, data=data if data is not None else {},
            params=params, timeout=self.control_timeout))

    # -- discovery
    def props(self) -> dict:
        return self._get("props")

    def features(self) -> frozenset:
        """``/props.features``. Branch on these, never on a version."""
        feats = (self.props() or {}).get("features")
        return frozenset(str(f) for f in feats) if isinstance(feats, list) else frozenset()

    def boot_id(self) -> Optional[str]:
        """``/health.boot_id``: pool ids restart at 0 under a new one."""
        body = self._get("health")
        return str(body["boot_id"]) if isinstance(body, dict) and body.get("boot_id") else None

    # -- pools
    def create_pool(self, **kw) -> dict:
        return self._post("polykv/pools", create_body(**kw))

    def fork_pool(self, parent: PoolId, **kw) -> dict:
        return self._post(f"polykv/pools/{parent}/fork", fork_body(**kw))

    def release_pool(self, pool: PoolId, *, delete: bool = False) -> dict:
        """Release (leaves first: a parent with children refuses).
        ``delete`` uses the ``DELETE`` alias (``pool_delete_alias_v1``)."""
        if delete:
            return raise_for(self.transport.call(
                "DELETE", f"polykv/pools/{pool}", timeout=self.control_timeout))
        return self._post(f"polykv/pools/{pool}/release")

    def pin_pool(self, pool: PoolId, pin: bool = True) -> dict:
        return self._post(f"polykv/pools/{pool}/{'pin' if pin else 'unpin'}")

    def pools(self, *, live: bool = False) -> dict:
        return self._get("polykv/pools", {"live": "1"} if live else None)

    def pool(self, pool: PoolId, *, live: bool = False) -> dict:
        return self._get(f"polykv/pools/{pool}", {"live": "1"} if live else None)

    def capacity(self, pool: PoolId, *, expected_tokens: Optional[int] = None,
                 fold: bool = False) -> Capacity:
        """A plain read is an observation; ``fold`` only from the one
        caller that decides admissions for this pool."""
        params = {"expected_tokens": expected_tokens,
                  "fold": "1" if fold else None}
        return Capacity.from_json(self._get(f"polykv/pools/{pool}/capacity", params))

    def set_admission(self, pool: PoolId, **kw) -> dict:
        return self._post(f"polykv/pools/{pool}/admission", admission_body(**kw))

    def set_sampling(self, mode: str) -> dict:
        if mode not in ("device", "cpu", "auto"):
            raise ValueError("sampling mode must be device / cpu / auto")
        return self._post("polykv/sampling", {"mode": mode})

    # -- status
    def kv(self, *, live: bool = False) -> dict:
        return self._get("kv", {"live": "1"} if live else None)

    def tps(self) -> dict:
        return self._get("polykv/tps", {"once": "1"})

    def elastic(self) -> dict:
        return self._get("elastic")

    def metrics(self) -> dict:
        """The PolyKV gauges of ``/metrics`` as ``{name: value}``."""
        reply = self.transport.call("GET", "metrics", timeout=self.read_timeout)
        raise_for(reply)
        out: dict[str, float] = {}
        for line in (reply.text or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and not line.startswith("#") and "polykv" in parts[0]:
                val = _num(parts[1])
                if val is not None:
                    out[parts[0]] = val
        return out

    # -- sessions
    def close_session(self, session_id: str, *, keep_kv: bool = False,
                      keep_running: bool = False) -> CloseResult:
        """Close a session: its window and every pool it owns go. The
        answer's ``found`` is read back — a close that found nothing did
        not release what the caller thinks it held."""
        body: dict[str, Any] = {}
        if keep_kv:
            body["keep_kv"] = True
        if keep_running:
            body["keep_running"] = True
        if _ID_OK.match(session_id):
            got = self._post(f"sessions/{session_id}/close", body)
        else:
            got = self._post("sessions/close", {"session_id": session_id, **body})
        got = got if isinstance(got, dict) else {}
        return CloseResult(session_id=session_id, found=bool(got.get("found")),
                           released=got.get("released"),
                           deferred=bool(got.get("deferred")), raw=got)

    def resize_session(self, session_id: str, num_ctx: int) -> dict:
        """Grow or shrink a booked window; ``deferred`` answers 202 and
        applies at the session's idle moment."""
        if _ID_OK.match(session_id):
            return self._post(f"kv/sessions/{session_id}/resize", {"num_ctx": num_ctx})
        return self._post("sessions/resize", {"session_id": session_id,
                                              "num_ctx": num_ctx})

    def erase_slot(self, slot_id: int) -> dict:
        """Drop a slot's KV without closing its session."""
        return self._post(f"slots/{int(slot_id)}", params={"action": "erase"})

    # -- prompt building
    def apply_template(self, messages: list, *, tools: Optional[list] = None,
                       chat_template_kwargs: Optional[dict] = None,
                       pool_id: Optional[PoolId] = None,
                       continue_pool: bool = False) -> str:
        """The server's own rendering, without a BOS (so a pool created
        from it tokenizes like a completion prompt)."""
        body: dict[str, Any] = {"messages": messages}
        if tools:
            body["tools"] = tools
        if chat_template_kwargs:
            body["chat_template_kwargs"] = chat_template_kwargs
        if continue_pool:
            if pool_id is None:
                raise ValueError("continue_pool needs a pool_id")
            body["continue_pool"] = True
        if pool_id is not None:
            body["pool_id"] = int(pool_id)
        got = self._post("apply-template", body)
        return str((got or {}).get("prompt") or "")

    def tokenize(self, content: str, *, add_special: bool = False) -> list:
        """Serving tokenizes with ``add_special=true``; ``/tokenize``
        defaults to false. Say which one is meant."""
        got = self._post("tokenize", {"content": content,
                                      "add_special": add_special,
                                      "parse_special": True})
        return list((got or {}).get("tokens") or [])


# ----------------------------------------------------------- observation

@dataclass
class Observation:
    """What one completion says about where it ran."""

    boot_id: Optional[str] = None
    #: ``X-Context-Window``: the granted window; ``None`` = none guaranteed.
    context_window: Optional[int] = None
    sessions_remaining: Optional[int] = None
    settle_waived_ms: Optional[float] = None
    pool_id: Optional[str] = None
    n_pool_shared: Optional[int] = None
    pool_match: Optional[int] = None
    pool_len: Optional[int] = None
    pool_unknown: bool = False
    cache_n: Optional[int] = None
    prompt_n: Optional[int] = None
    warnings: list = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """A pool was named and nothing was shared. Without an ``opencoti``
        block (xOllama's native ``/api/chat``) the cached prompt count
        stands in: a worker's fresh session has no cache of its own."""
        if self.pool_id is None or self.pool_unknown:
            return False
        if self.n_pool_shared is not None:
            return self.n_pool_shared == 0
        return self.cache_n == 0


def _int_or_none(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def observe(headers: Optional[dict], body: Any, *,
            pool_id: Optional[PoolId] = None) -> Observation:
    """Read a completion's placement. ``pool_id`` is the pool the request
    named; without it a missing ``opencoti`` block is normal."""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    b = body if isinstance(body, dict) else {}
    oc = b.get("opencoti") if isinstance(b.get("opencoti"), dict) else {}
    timings = b.get("timings") if isinstance(b.get("timings"), dict) else {}
    named = str(pool_id) if pool_id is not None else (
        str(oc["pool_id"]) if oc.get("pool_id") is not None else None)
    obs = Observation(
        boot_id=h.get("x-opencoti-boot-id") or None,
        context_window=_int_or_none(h.get("x-context-window")),
        sessions_remaining=_int_or_none(h.get("x-sessions-remaining")),
        settle_waived_ms=_num(h.get("x-polykv-settle-waived")),
        pool_id=named,
        n_pool_shared=_int_or_none(oc.get("n_pool_shared")),
        pool_match=_int_or_none(oc.get("pool_match")),
        pool_len=_int_or_none(oc.get("pool_len")),
        pool_unknown=bool(oc.get("pool_unknown")),
        cache_n=_int_or_none(timings.get("cache_n")),
        prompt_n=_int_or_none(timings.get("prompt_n")))
    if named is not None and not oc and obs.cache_n is None:
        obs.warnings.append(f"pool {named} named but the reply has no "
                            f"opencoti block (attach not reported)")
    elif named is not None and not oc and obs.cache_n == 0:
        obs.warnings.append(f"pool {named} named and nothing cached: the "
                            f"attach degraded to a full reprocess")
    elif obs.pool_unknown:
        obs.warnings.append(f"pool {named} unknown to this engine "
                            f"(released, or a restart renumbered it)")
    elif obs.degraded:
        obs.warnings.append(f"pool {named} named and n_pool_shared 0: the "
                            f"attach degraded to a full reprocess")
    elif (obs.pool_match is not None and obs.pool_len is not None
          and obs.pool_match < obs.pool_len):
        obs.warnings.append(f"pool {named}: the prompt matches "
                            f"{obs.pool_match} of its {obs.pool_len} tokens")
    for w in obs.warnings:
        log.warning("polykv: %s", w)
    return obs


def has_all(features: Iterable[str], *needed: str) -> bool:
    have = set(features)
    return all(n in have for n in needed)


__all__ = [
    "CONTROL_TIMEOUT_S", "Capacity", "CloseResult", "DirectTransport",
    "Observation", "OpencotiError", "PolykvClient", "PoolSaturatedError",
    "READ_TIMEOUT_S", "Reply", "XollamaTransport", "admission_body",
    "create_body", "fork_body", "has_all", "http_fetch", "observe",
    "pool_ref", "raise_for", "wire_id",
]
