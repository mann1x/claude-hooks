"""Probe Ollama for a model's max context window via ``/api/show``.

Ollama reports model metadata under ``model_info`` keyed by architecture,
e.g. ``model_info["qwen3.context_length"]`` or
``model_info["llama.context_length"]``. The architecture prefix varies,
so this helper scans for any key ending in ``.context_length``. Result
cached per-process to keep CLI invocations cheap.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("claude_hooks.get_advice.ctx_probe")

DEFAULT_BASE_URL = "http://192.168.178.2:11433"
PROBE_TIMEOUT_S = 10.0

_cache: dict[tuple[str, str], Optional[int]] = {}


def base_url_default() -> str:
    """Same env var the caliber proxy reads. One knob per host."""
    raw = os.environ.get("CALIBER_GROUNDING_UPSTREAM", "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    if raw.endswith("/v1"):
        raw = raw[: -len("/v1")]
    return raw.rstrip("/")


def probe_max_ctx(model: str, base_url: Optional[str] = None,
                  *, force: bool = False) -> Optional[int]:
    """Query ``POST /api/show`` for ``model`` and return its
    ``context_length`` if discoverable. Returns None on failure."""
    if not model:
        return None
    base = base_url or base_url_default()
    key = (base, model)
    if not force and key in _cache:
        return _cache[key]
    url = base.rstrip("/") + "/api/show"
    body = json.dumps({"name": model}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("probe %s: %s", model, e)
        _cache[key] = None
        return None
    ctx = _extract_context_length(data)
    _cache[key] = ctx
    return ctx


def _extract_context_length(data: dict) -> Optional[int]:
    info = data.get("model_info")
    if isinstance(info, dict):
        for k, v in info.items():
            if isinstance(k, str) and k.endswith(".context_length"):
                if isinstance(v, int) and v > 0:
                    return v
                try:
                    n = int(v)
                    if n > 0:
                        return n
                except (TypeError, ValueError):
                    continue
    # Some Ollama variants surface it on the top-level ``parameters`` blob
    # as ``num_ctx N`` (one entry per line). Best-effort parse.
    params = data.get("parameters")
    if isinstance(params, str):
        for line in params.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "num_ctx":
                try:
                    n = int(parts[1])
                    if n > 0:
                        return n
                except ValueError:
                    pass
    return None


def reset_cache() -> None:
    _cache.clear()
