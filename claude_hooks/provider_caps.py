"""What a chat backend can do, learned by probing it.

One :class:`ProviderCaps` record per ``(base_url, model)``, built from
the backend's own answers and cached with a TTL. Budgets, thinking and
sampling read their numbers from here, so nothing downstream guesses a
window, an output ceiling or a temperature from a model's name or from
the host it happens to run on.

Probed per backend:

- **Ollama** (local and cloud): ``POST /api/show`` (Modelfile
  parameters, trained window, capabilities, family, budget message),
  ``GET /api/tags`` (``remote_host`` marks a cloud model) and
  ``GET /api/experimental/model-recommendations`` (cloud window, max
  output, accepted thinking values, required plan). See
  :mod:`claude_hooks.ollama_account`.
- **xOllama**: ``GET /api/xollama`` answers ``{xollama: true,
  features: [...]}``; a stock Ollama returns 404. Branch on feature
  names, never on the version (a dev build reports ``0.0.0``).
- **opencoti / llama.cpp / llamafile**: ``GET /props`` (``features``,
  default sampler, ``n_ctx``) and ``GET /health`` (``boot_id``).

Every probe is soft: a failure leaves the field ``None`` (unknown),
which callers must treat as "do not guess", never as a small default.
A network failure is not cached; a definite answer (including a 404)
is, for :data:`CAPS_TTL_S`. A changed ``boot_id`` drops the record.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

log = logging.getLogger("claude_hooks.provider_caps")

#: How long a probe result is trusted. Long enough that a council does
#: not re-probe per call, short enough that a model re-pulled with a new
#: Modelfile is noticed the same day.
CAPS_TTL_S = 600.0
PROBE_TIMEOUT_S = 10.0

OLLAMA = "ollama"
XOLLAMA = "xollama"
OPENCOTI = "opencoti"
LLAMACPP = "llamacpp"


@dataclass(frozen=True)
class ProviderCaps:
    base_url: str
    model: str
    backend: str = OLLAMA
    #: A cloud model: served by a remote host, not this machine. From
    #: ``/api/tags`` ``remote_host``; the ``:cloud`` name rule only when
    #: the catalog could not be read.
    cloud: Optional[bool] = None
    remote_host: Optional[str] = None
    remote_model: Optional[str] = None
    capabilities: tuple[str, ...] = ()
    #: ``num_ctx`` the Modelfile declares (local models): the window the
    #: model is loaded with when the request names none.
    declared_num_ctx: Optional[int] = None
    #: ``<arch>.context_length``: what the weights were trained for. A
    #: ceiling, never the window a local model actually runs with.
    trained_ctx: Optional[int] = None
    #: Model recommendations (cloud): the window the service grants and
    #: the most output tokens it accepts per request.
    context_length: Optional[int] = None
    max_output_tokens: Optional[int] = None
    thinking_values: tuple[str, ...] = ()
    thinking_default: Optional[str] = None
    required_plan: Optional[str] = None
    #: Base temperature the Modelfile (or engine default) declares.
    temperature: Optional[float] = None
    family: Optional[str] = None
    budget_message: Optional[str] = None
    #: Feature flags from ``/api/xollama`` or opencoti ``/props``.
    features: tuple[str, ...] = ()
    boot_id: Optional[str] = None
    #: opencoti KV control plane (``/props`` ``opencoti.kv``): the whole
    #: pool shared by every session, the most one session may hold, and
    #: the largest window admissible right now.
    kv_pool_cells: Optional[int] = None
    session_ctx_max: Optional[int] = None
    largest_admissible: Optional[int] = None
    total_slots: Optional[int] = None
    #: opencoti ``/props`` ``opencoti.dca``: whether dual-chunk attention
    #: extends the window, and its YaRN factor. Recorded, never used to
    #: compute a window: ``session_ctx_max`` is the engine's own ceiling.
    dca_enabled: Optional[bool] = None
    dca_yarn_factor: Optional[float] = None
    probed_at: float = field(default_factory=time.time)

    @property
    def thinking(self) -> Optional[bool]:
        """``None`` when the backend did not list capabilities at all."""
        return ("thinking" in self.capabilities) if self.capabilities else None

    @property
    def tools(self) -> Optional[bool]:
        return ("tools" in self.capabilities) if self.capabilities else None

    def has(self, feature: str) -> bool:
        return feature in self.features

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["thinking"] = self.thinking
        out["tools"] = self.tools
        for k, v in list(out.items()):
            if isinstance(v, tuple):
                out[k] = list(v)
        return out


# ------------------------------------------------------------ parsers

def context_length_from_show(data: dict) -> Optional[int]:
    """``model_info["<arch>.context_length"]``; the arch prefix varies,
    so match the suffix."""
    info = data.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if not key.endswith(".context_length"):
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _parameters(data: dict) -> dict[str, str]:
    """``parameters`` as ``{key: last value}``. Ollama sends a text blob,
    one ``key value`` pair per line with values often quoted; some
    builds send a map. A key may repeat (``stop``); the last one wins
    here, which is right for every scalar this module reads."""
    raw = data.get("parameters")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = str(v)
    elif isinstance(raw, str):
        for line in raw.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = _unquote(parts[1].strip())
    return out


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
        value = (value.replace("\\n", "\n").replace("\\t", "\t")
                 .replace("\\r", "\r").replace('\\"', '"'))
    return value


def _int(value: Any) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _float(value: Any) -> Optional[float]:
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


_BUDGET_MESSAGE_KEYS = (
    "thinking_budget_message", "think_budget_message",
    "reasoning_budget_message",
)


def budget_message_from_show(data: dict) -> Optional[str]:
    params = _parameters(data)
    for key in _BUDGET_MESSAGE_KEYS:
        if params.get(key, "").strip():
            return params[key]
    for container in (data.get("model_info"), data.get("details")):
        if isinstance(container, dict):
            for key in _BUDGET_MESSAGE_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return None


def caps_fields_from_show(data: dict) -> dict:
    """The :class:`ProviderCaps` fields one ``/api/show`` answer gives."""
    params = _parameters(data)
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    caps = data.get("capabilities")
    out: dict = {
        "declared_num_ctx": _int(params.get("num_ctx")),
        "trained_ctx": context_length_from_show(data),
        "temperature": _float(params.get("temperature")),
        "family": (details.get("family") or None),
        "budget_message": budget_message_from_show(data),
        "capabilities": tuple(str(c) for c in caps) if isinstance(caps, list) else (),
    }
    # ``thinking: {"values": [false, true], "default": false}`` — the values
    # the model takes. gemma4:31b-cloud declares only on/off, takes no level
    # and, since 2026-09, silently ignores one ("high" thinks not at all),
    # so a model not in the recommendations must be read from here.
    thinking = data.get("thinking")
    if isinstance(thinking, dict):
        from claude_hooks.ollama_account import _thinking_value
        values = thinking.get("values")
        if isinstance(values, list):
            vals = tuple(v for v in map(_thinking_value, values) if v)
            if vals:
                out["thinking_values"] = vals
        default = _thinking_value(thinking.get("default"))
        if default:
            out["thinking_default"] = default
    for key in ("remote_host", "remote_model"):
        if data.get(key):
            out[key] = str(data[key])
    if out.get("remote_host"):
        out["cloud"] = True
    return out


def caps_fields_from_props(data: dict) -> dict:
    """opencoti / llama.cpp ``GET /props``. ``build_info`` is never read:
    it is unreliable, and branching on it is how a client breaks on the
    next build. Only ``features`` decides behaviour."""
    out: dict = {}
    feats = data.get("features")
    if isinstance(feats, list):
        out["features"] = tuple(str(f) for f in feats)
    dgs = data.get("default_generation_settings")
    if isinstance(dgs, dict):
        params = dgs.get("params") if isinstance(dgs.get("params"), dict) else dgs
        t = _float(params.get("temperature"))
        if t is not None:
            out["temperature"] = t
        n_ctx = _int(dgs.get("n_ctx"))
        if n_ctx is not None:
            out["declared_num_ctx"] = n_ctx
    n_ctx = _int(data.get("n_ctx"))
    if n_ctx is not None and "declared_num_ctx" not in out:
        out["declared_num_ctx"] = n_ctx
    slots = _int(data.get("total_slots"))
    if slots is not None:
        out["total_slots"] = slots
    oc = data.get("opencoti")
    kv = oc.get("kv") if isinstance(oc, dict) and isinstance(oc.get("kv"), dict) else {}
    dca = oc.get("dca") if isinstance(oc, dict) and isinstance(oc.get("dca"), dict) else {}
    if isinstance(dca.get("enabled"), bool):
        out["dca_enabled"] = dca["enabled"]
        f = _float(dca.get("yarn_factor"))
        if f is not None:
            out["dca_yarn_factor"] = f
    for src, dst in (("kv_pool_cells", "kv_pool_cells"),
                     ("session_ctx_max", "session_ctx_max"),
                     ("largest_admissible", "largest_admissible")):
        n = _int(kv.get(src))
        if n is not None:
            out[dst] = n
    return out


# ------------------------------------------------------------ transport

def _request(url: str, *, data: Optional[dict] = None,
             timeout: float = PROBE_TIMEOUT_S,
             headers: Optional[dict] = None) -> tuple[int, Any]:
    """``(status, parsed body or None)``. Raises only on a network
    failure, so callers can tell "answered no" from "could not ask"."""
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, method="POST" if data is not None else "GET",
        data=json.dumps(data).encode() if data is not None else None,
        headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = e.code
    try:
        body = json.loads(raw) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    return status, body


Fetch = Callable[..., tuple[int, Any]]


def is_cloud_service(base_url: str) -> bool:
    """Whether ``base_url`` is Ollama's own cloud service (ollama.com, its
    ``/api`` and ``/v1`` used directly, no local server). Decided by the
    host, never by a model's name: there ``gemma4:31b`` is the cloud
    model, while on a local server the same name is the downloadable
    quant and the cloud one is ``gemma4:31b-cloud``."""
    from claude_hooks import ollama_upstream
    return ollama_upstream.is_cloud(base_url)


def probe_xollama(base_url: str, *, fetch: Fetch = _request,
                  headers: Optional[dict] = None) -> Optional[tuple[str, ...]]:
    """xOllama's feature list, ``()`` for a stock Ollama (404), ``None``
    when the server could not be asked."""
    try:
        status, body = fetch(f"{base_url}/api/xollama", headers=headers)
    except (urllib.error.URLError, OSError):
        return None
    if status == 200 and isinstance(body, dict) and body.get("xollama"):
        feats = body.get("features")
        return tuple(str(f) for f in feats) if isinstance(feats, list) else ("",)
    return ()


def probe_show(base_url: str, model: str, *, fetch: Fetch = _request,
               headers: Optional[dict] = None) -> Optional[dict]:
    """The model's ``/api/show`` record; ``{}`` when the server answered
    without one (a 404 for an unknown model, a 410 for a retired one);
    ``None`` when it could not be asked (network failure or a 5xx), which
    must never be cached: the window it would have carried defaults."""
    try:
        status, body = fetch(f"{base_url}/api/show", data={"model": model},
                             headers=headers)
    except (urllib.error.URLError, OSError) as e:
        log.info("provider_caps: /api/show failed for %s at %s: %s",
                 model, base_url, e)
        return None
    if status == 200 and isinstance(body, dict):
        return body
    if 400 <= status < 500:
        return {}
    log.info("provider_caps: /api/show answered %s for %s at %s",
             status, model, base_url)
    return None


def probe_props(base_url: str, *, fetch: Fetch = _request,
                headers: Optional[dict] = None) -> Optional[dict]:
    root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        status, body = fetch(f"{root}/props", headers=headers)
    except (urllib.error.URLError, OSError):
        return None
    return body if status == 200 and isinstance(body, dict) else None


def probe_boot_id(base_url: str, *, fetch: Fetch = _request,
                  headers: Optional[dict] = None) -> Optional[str]:
    root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        status, body = fetch(f"{root}/health", headers=headers)
    except (urllib.error.URLError, OSError):
        return None
    if status == 200 and isinstance(body, dict) and body.get("boot_id"):
        return str(body["boot_id"])
    return None


# ------------------------------------------------------------ the cache

_cache: dict[tuple[str, str], ProviderCaps] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()
    with _lock:
        _loaded.clear()


def cached(base_url: str, model: str) -> Optional[ProviderCaps]:
    with _lock:
        hit = _cache.get((base_url, model))
    if hit is not None and time.time() - hit.probed_at < CAPS_TTL_S:
        return hit
    return None


def _store(caps: ProviderCaps) -> ProviderCaps:
    with _lock:
        _cache[(caps.base_url, caps.model)] = caps
    return caps


def ollama_caps(base_url: str, model: str, *,
                show: Optional[dict] = None,
                fetch: Fetch = _request,
                headers: Optional[dict] = None,
                refresh: bool = False) -> ProviderCaps:
    """Probe an Ollama-protocol server (stock Ollama or xOllama).

    ``show`` lets a caller that already holds this model's ``/api/show``
    answer pass it in instead of paying a second round trip.
    """
    from claude_hooks import ollama_account

    if not refresh:
        hit = cached(base_url, model)
        if hit is not None:
            return hit
    fields: dict = {}
    features = probe_xollama(base_url, fetch=fetch, headers=headers)
    backend = XOLLAMA if features else OLLAMA
    if features:
        fields["features"] = tuple(f for f in features if f)
    if show is None:
        show = probe_show(base_url, model, fetch=fetch, headers=headers)
    if show:
        fields.update(caps_fields_from_show(show))
    entry = ollama_account.catalog_entry(base_url, model, fetch=fetch,
                                         headers=headers)
    if entry is not None:
        fields["cloud"] = bool(entry.get("cloud"))
        for k in ("remote_host", "remote_model"):
            if entry.get(k):
                fields[k] = entry[k]
        if entry.get("family") and not fields.get("family"):
            fields["family"] = entry["family"]
    elif "cloud" not in fields:
        fields["cloud"] = model.endswith(":cloud") or model.endswith("-cloud")
    if is_cloud_service(base_url):
        fields["cloud"] = True
    rec = ollama_account.recommendation_for(
        base_url, model, remote_model=fields.get("remote_model"),
        fetch=fetch, headers=headers)
    if rec is not None:
        for k in ("context_length", "max_output_tokens", "thinking_default",
                  "required_plan"):
            if rec.get(k) is not None:
                fields[k] = rec[k]
        if rec.get("thinking_values"):
            fields["thinking_values"] = tuple(rec["thinking_values"])
    caps = ProviderCaps(base_url=base_url, model=model, backend=backend,
                        **fields)
    # Only a definite answer is worth keeping. ``/api/show`` carries the
    # trained window, so a record built while it could not be asked is
    # asked again next time, whatever else answered: cached, a transient
    # failure pinned an ollama.com model to the 32,768 default.
    if show is not None:
        _store(caps)
    return caps


#: How long a loaded-window answer is trusted. Short: a model reloads.
LOADED_TTL_S = 60.0
_loaded: dict[tuple[str, str], tuple[float, Optional[int]]] = {}


def loaded_context(base_url: str, model: str, *, fetch: Fetch = _request,
                   headers: Optional[dict] = None) -> Optional[int]:
    """The window ``model`` is loaded with right now (``GET /api/ps``
    ``context_length``), or ``None`` when it is not loaded or the server
    did not say. The answer for a model with no Modelfile ``num_ctx``:
    the server's default, not the trained context (plan B2)."""
    key = (base_url, model)
    now = time.time()
    with _lock:
        hit = _loaded.get(key)
    if hit is not None and now - hit[0] < LOADED_TTL_S:
        return hit[1]
    try:
        status, data = fetch(f"{base_url}/api/ps", headers=headers)
    except OSError:
        return None  # not cached: ask again next time
    n: Optional[int] = None
    if status == 200 and isinstance(data, dict):
        for m in data.get("models") or []:
            if isinstance(m, dict) and model in (m.get("name"), m.get("model")):
                n = _int(m.get("context_length"))
                break
    with _lock:
        _loaded[key] = (now, n)
    return n


def openai_compat_caps(base_url: str, model: str, *,
                       fetch: Fetch = _request,
                       headers: Optional[dict] = None,
                       refresh: bool = False) -> ProviderCaps:
    """Probe an OpenAI-compatible llama.cpp-family server (opencoti,
    llama.cpp, llamafile). opencoti is told apart by its features."""
    boot_id = probe_boot_id(base_url, fetch=fetch, headers=headers)
    if not refresh:
        hit = cached(base_url, model)
        if hit is not None and (boot_id is None or hit.boot_id in (None, boot_id)):
            return hit
    props = probe_props(base_url, fetch=fetch, headers=headers)
    fields = caps_fields_from_props(props) if props else {}
    if not (fields.get("session_ctx_max") or fields.get("declared_num_ctx")):
        # No /props window: an Ollama-protocol server (ollama.com, Ollama's
        # own /v1) declares its windows through /api/show and the
        # recommendations at the same root. Without this a /v1 endpoint at
        # ollama.com had no window at all and never compacted.
        root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
        oc = ollama_caps(root, model, fetch=fetch, headers=headers,
                         refresh=refresh)
        if oc.trained_ctx or oc.context_length:
            return _store(replace(oc, base_url=base_url, boot_id=boot_id))
    backend = OPENCOTI if (props and (
        "opencoti" in props or any("polykv" in f or "elastic" in f
                                   for f in fields.get("features", ())))) \
        else LLAMACPP
    caps = ProviderCaps(base_url=base_url, model=model, backend=backend,
                        cloud=False, boot_id=boot_id, **fields)
    if props is not None or boot_id is not None:
        _store(caps)
    return caps


def with_boot_id(caps: ProviderCaps, boot_id: Optional[str]) -> ProviderCaps:
    """A record seen under a new ``boot_id`` is stale: the engine
    restarted, pools were renumbered and its window may differ."""
    if boot_id and caps.boot_id and boot_id != caps.boot_id:
        with _lock:
            _cache.pop((caps.base_url, caps.model), None)
        return replace(caps, boot_id=boot_id, probed_at=0.0)
    return caps
