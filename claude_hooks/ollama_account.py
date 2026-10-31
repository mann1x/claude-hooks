"""The Ollama account and its cloud-model metadata, read from the server.

Three reads, each standing alone (one failing never empties the others):

- ``POST <server>/api/me``: who is signed in and on which plan. The local
  server signs the call with its own key, so no credentials are needed
  here. Only the plan and the signed-in flag are kept — the answer also
  carries the account's name and e-mail, which nothing here needs.
- ``GET <server>/api/experimental/model-recommendations``: per model the
  window the service grants (``context_length``; ``0`` on a local entry
  means "the model decides"), the most output it accepts
  (``max_output_tokens``), the plan it needs and the thinking values it
  accepts (``false``/``true`` become ``off``/``on``).
- ``GET <server>/api/tags``: the catalog. ``remote_host`` (or
  ``remote_model``) is what makes a model a cloud model; the ``:cloud``
  name rule is only the fallback when the catalog cannot be read.

Cached per server with a TTL; a read that failed is not cached.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.error
from typing import Any, Optional

log = logging.getLogger("claude_hooks.ollama_account")

TAGS_TTL_S = 600.0
RECOMMENDATIONS_TTL_S = 3600.0
WHOAMI_TTL_S = 3600.0

_lock = threading.Lock()
_tags: dict[str, tuple[float, list[dict]]] = {}
_recs: dict[str, tuple[float, list[dict]]] = {}
_whoami: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    with _lock:
        _tags.clear()
        _recs.clear()
        _whoami.clear()


def _fetch_default():
    from claude_hooks.provider_caps import _request
    return _request


def _thinking_value(value: Any) -> Optional[str]:
    if value is False:
        return "off"
    if value is True:
        return "on"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def looks_like_cloud_name(model: str) -> bool:
    return bool(re.search(r"(?::cloud|-cloud)$", (model or "").strip()))


def _stem(model: str) -> str:
    return re.sub(r"(?::cloud|-cloud)$", "", (model or "").strip())


# ------------------------------------------------------------ parsers

def parse_whoami(status: int, body: Any) -> dict:
    """``{reachable, signed_in, plan?, signin_url?}``. Go's exported
    names (``Plan``) and lowercase ones are both accepted. 401 means
    signed out; any other non-200 is "could not check", not a verdict."""
    body = body if isinstance(body, dict) else {}
    if status == 401:
        out = {"reachable": True, "signed_in": False}
        if isinstance(body.get("signin_url"), str) and body["signin_url"]:
            out["signin_url"] = body["signin_url"]
        return out
    if status != 200:
        return {"reachable": False, "signed_in": False}
    name = body.get("Name") or body.get("name")
    if not name:
        return {"reachable": True, "signed_in": False}
    plan = body.get("Plan") or body.get("plan") or "free"
    return {"reachable": True, "signed_in": True, "plan": str(plan).lower()}


def parse_recommendations(body: Any) -> list[dict]:
    raw = body.get("recommendations") if isinstance(body, dict) else None
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get("model"):
            continue
        thinking = entry.get("thinking") if isinstance(entry.get("thinking"), dict) else {}
        values = [v for v in (_thinking_value(x) for x in thinking.get("values") or [])
                  if v is not None]
        rec: dict = {"model": str(entry["model"])}
        for key in ("context_length", "max_output_tokens", "vram_bytes"):
            n = _positive(entry.get(key))
            if n is not None:
                rec[key] = n
        if isinstance(entry.get("required_plan"), str) and entry["required_plan"]:
            rec["required_plan"] = entry["required_plan"].lower()
        if values:
            rec["thinking_values"] = values
        default = _thinking_value(thinking.get("default"))
        if default is not None:
            rec["thinking_default"] = default
        if isinstance(entry.get("description"), str):
            rec["description"] = entry["description"]
        out.append(rec)
    return out


def parse_catalog(body: Any) -> list[dict]:
    raw = body.get("models") if isinstance(body, dict) else None
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        item = {
            "name": str(name),
            "cloud": bool(entry.get("remote_host") or entry.get("remote_model")),
        }
        for key in ("remote_host", "remote_model"):
            if entry.get(key):
                item[key] = str(entry[key])
        if details.get("family"):
            item["family"] = str(details["family"])
        caps = entry.get("capabilities")
        if isinstance(caps, list):
            item["capabilities"] = [str(c) for c in caps]
        out.append(item)
    return out


def match_recommendation(recs: list[dict], name: str,
                         remote_model: Optional[str] = None) -> Optional[dict]:
    """``remote_model`` before the tag's own name — a tag re-templated
    FROM a cloud model keeps its own name — then a stem match without the
    ``:cloud`` / ``-cloud`` suffix."""
    candidates = [c for c in (remote_model, name) if c]
    for c in candidates:
        for r in recs:
            if r["model"] == c:
                return r
    for c in candidates:
        stem = _stem(c)
        for r in recs:
            if _stem(r["model"]) == stem:
                return r
    return None


# ------------------------------------------------------------ reads

def _cached(table: dict, key: str, ttl: float):
    with _lock:
        hit = table.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _put(table: dict, key: str, value) -> None:
    with _lock:
        table[key] = (time.monotonic(), value)


def catalog(base_url: str, *, fetch=None, headers: Optional[dict] = None
            ) -> Optional[list[dict]]:
    hit = _cached(_tags, base_url, TAGS_TTL_S)
    if hit is not None:
        return hit
    fetch = fetch or _fetch_default()
    try:
        status, body = fetch(f"{base_url}/api/tags", headers=headers)
    except (urllib.error.URLError, OSError) as e:
        log.debug("ollama_account: /api/tags failed at %s: %s", base_url, e)
        return None
    if status != 200:
        return None
    parsed = parse_catalog(body)
    _put(_tags, base_url, parsed)
    return parsed


def catalog_entry(base_url: str, model: str, *, fetch=None,
                  headers: Optional[dict] = None) -> Optional[dict]:
    """The catalog row for ``model``. When the catalog was read but the
    model is not in it, a cloud-looking name is reported as an unpulled
    cloud model: the server fetches its manifest on first use."""
    cat = catalog(base_url, fetch=fetch, headers=headers)
    if cat is None:
        return None
    for item in cat:
        if item["name"] == model:
            return item
    if looks_like_cloud_name(model):
        return {"name": model, "cloud": True, "pulled": False}
    return {"name": model, "cloud": False, "pulled": False}


def recommendations(base_url: str, *, fetch=None,
                    headers: Optional[dict] = None) -> Optional[list[dict]]:
    hit = _cached(_recs, base_url, RECOMMENDATIONS_TTL_S)
    if hit is not None:
        return hit
    fetch = fetch or _fetch_default()
    try:
        status, body = fetch(f"{base_url}/api/experimental/model-recommendations",
                             headers=headers)
    except (urllib.error.URLError, OSError) as e:
        log.debug("ollama_account: recommendations failed at %s: %s",
                  base_url, e)
        return None
    if status != 200:
        # An older server without the endpoint: a definite "none".
        _put(_recs, base_url, [])
        return []
    parsed = parse_recommendations(body)
    _put(_recs, base_url, parsed)
    return parsed


def recommendation_for(base_url: str, model: str, *,
                       remote_model: Optional[str] = None, fetch=None,
                       headers: Optional[dict] = None) -> Optional[dict]:
    recs = recommendations(base_url, fetch=fetch, headers=headers)
    if not recs:
        return None
    return match_recommendation(recs, model, remote_model)


def whoami(base_url: str, *, fetch=None, headers: Optional[dict] = None
           ) -> dict:
    """The account through the local server (no key needed here)."""
    hit = _cached(_whoami, base_url, WHOAMI_TTL_S)
    if hit is not None:
        return hit
    fetch = fetch or _fetch_default()
    try:
        status, body = fetch(f"{base_url}/api/me", data={}, headers=headers)
    except (urllib.error.URLError, OSError) as e:
        log.debug("ollama_account: /api/me failed at %s: %s", base_url, e)
        return {"reachable": False, "signed_in": False}
    parsed = parse_whoami(status, body)
    if parsed["reachable"]:
        _put(_whoami, base_url, parsed)
    return parsed


def status(base_url: str, *, models: Optional[list[str]] = None,
           fetch=None, headers: Optional[dict] = None) -> dict:
    """Everything ``claude-consultants account`` shows for one server:
    the plan, and for each model asked about (default: every cloud
    model in the catalog plus the cloud recommendations) its window, max
    output, thinking values and whether the plan covers it."""
    acct = whoami(base_url, fetch=fetch, headers=headers)
    cat = catalog(base_url, fetch=fetch, headers=headers) or []
    recs = recommendations(base_url, fetch=fetch, headers=headers) or []
    names = list(models or [])
    if not names:
        names = [m["name"] for m in cat if m.get("cloud")]
        names += [r["model"] for r in recs
                  if "vram_bytes" not in r and r["model"] not in names]
    rows = []
    for name in names:
        entry = next((m for m in cat if m["name"] == name), None)
        rec = match_recommendation(recs, name,
                                   (entry or {}).get("remote_model"))
        row: dict = {"model": name,
                     "cloud": bool(entry["cloud"]) if entry
                     else looks_like_cloud_name(name),
                     "pulled": entry is not None}
        if rec:
            for k in ("context_length", "max_output_tokens",
                      "thinking_values", "thinking_default", "required_plan"):
                if k in rec:
                    row[k] = rec[k]
        row["plan_ok"] = plan_covers(acct.get("plan"), row.get("required_plan"))
        rows.append(row)
    return {"base_url": base_url, "account": acct, "models": rows}


_PLAN_RANK = {"free": 0, "pro": 1, "max": 2}


def plan_covers(plan: Optional[str], required: Optional[str]) -> Optional[bool]:
    """``None`` when either side is unknown."""
    if not plan or not required:
        return None
    if plan not in _PLAN_RANK or required not in _PLAN_RANK:
        return None
    return _PLAN_RANK[plan] >= _PLAN_RANK[required]
