"""Sampling diversity for every role (plan Milestone B4).

The same model often runs as several council instances in one run (fan-out
lanes, critic and meta-critic on one tag, a synthesizer that shares the
researcher's model) and must not answer alike. The mechanism is the fork's
(``spawn-sampling.ts``): each call-site instance gets its own seed and a
small temperature jitter around the model's own temperature.

- ``seed``: ``"random"`` (default) draws from ``[0, 2**31)`` per instance;
  same-model instances in one run never share one. An integer ``s`` gives
  the i-th same-model instance ``s + i``. ``"off"`` sends none.
- ``temperature``: the base is an explicit setting, else the model
  template, else the Modelfile / ``/props`` value; with none known it
  stays unset (the provider's default, which we cannot jitter honestly).
- ``temperature_range_pct`` (default 2, clamped to 10):
  ``T = base × (1 + u × pct/100)``, ``u ∈ [-1, 1]``, 3 decimals, ``≥ 0``.

An instance is one role turn or one lane: its tool-loop iterations reuse
the instance's values, so a lane is internally consistent while lanes
differ. Layers: ``[sampling]`` → ``[role.X.sampling]`` →
``[role.X.models."<m>"]``, seeded as ``runtime_control["sampling"]``.
"""
from __future__ import annotations

import random
import threading
from collections import OrderedDict
from typing import Any, Optional

DEFAULT_SEED = "random"
DEFAULT_RANGE_PCT = 2.0
MAX_RANGE_PCT = 10.0
SEED_SPACE = 2 ** 31

KEYS = ("seed", "temperature", "temperature_range_pct")

#: Runs whose allocations are remembered at once. A run is one council;
#: older ones are forgotten (their seeds no longer need to be unique).
_MAX_RUNS = 64

_lock = threading.Lock()
_runs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _run_book(run: str) -> dict[str, Any]:
    book = _runs.get(run)
    if book is None:
        book = {"seeds": {}, "count": {}}
        _runs[run] = book
        while len(_runs) > _MAX_RUNS:
            _runs.popitem(last=False)
    else:
        _runs.move_to_end(run)
    return book


def settings_for(layers: Optional[dict], role: str, model: str) -> dict:
    """Merged ``seed`` / ``temperature`` / ``temperature_range_pct`` for
    ``(role, model)``, defaults filled in."""
    out: dict = {"seed": DEFAULT_SEED, "temperature_range_pct": DEFAULT_RANGE_PCT}
    for key in ("*", role, f"{role}@{model}"):
        for k, v in ((layers or {}).get(key) or {}).items():
            if k in KEYS and v is not None:
                out[k] = v
    return out


def base_temperature(model: str, explicit: Any = None,
                     chat_client: Any = None) -> Optional[float]:
    """Explicit → model template → the backend's declared temperature."""
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    from claude_hooks.model_sampling import sampling_for
    t = sampling_for(model).get("temperature")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return float(t)
    probe = getattr(chat_client, "caps", None)
    if callable(probe):
        try:
            caps = probe(model)
        except Exception:  # pragma: no cover — probing never fails a call
            caps = None
        t = getattr(caps, "temperature", None)
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            return float(t)
    return None


def jitter(base: float, range_pct: float, rng: random.Random) -> float:
    pct = min(MAX_RANGE_PCT, max(0.0, float(range_pct)))
    if pct == 0:
        return round(base, 3)
    u = rng.uniform(-1.0, 1.0)
    return max(0.0, round(base * (1 + u * pct / 100), 3))


def allocate(run: Optional[str], role: str, model: str, *,
             layers: Optional[dict] = None, chat_client: Any = None,
             rng: Optional[random.Random] = None) -> dict:
    """The sampler options for one new instance of ``role`` on ``model``:
    ``{"seed": ..., "temperature": ...}``, either key absent when off or
    unknown. ``run`` scopes seed uniqueness; ``None`` disables B4 (the
    record a pre-B4 checkpoint resumes with), returning ``{}``."""
    if not run:
        return {}
    rng = rng or random.SystemRandom()
    cfg = settings_for(layers, role, model)
    out: dict = {}
    seed = cfg.get("seed")
    with _lock:
        book = _run_book(run)
        i = book["count"].get(model, 0)
        book["count"][model] = i + 1
        used = book["seeds"].setdefault(model, set())
        if isinstance(seed, int) and not isinstance(seed, bool):
            s = (seed + i) % SEED_SPACE
            while s in used:
                s = (s + 1) % SEED_SPACE
            out["seed"] = s
        elif seed == "random":
            s = rng.randrange(SEED_SPACE)
            while s in used:
                s = rng.randrange(SEED_SPACE)
            out["seed"] = s
        if "seed" in out:
            used.add(out["seed"])
    explicit = cfg.get("temperature")
    pct = cfg.get("temperature_range_pct", DEFAULT_RANGE_PCT)
    if explicit is None and not pct:
        # Nothing to jitter and nothing set: leave the temperature to the
        # template / provider exactly as before B4 (parity).
        return out
    base = base_temperature(model, explicit, chat_client)
    if base is not None:
        out["temperature"] = jitter(base, pct, rng)
    return out


def forget(run: str) -> None:
    with _lock:
        _runs.pop(run, None)


__all__ = ["allocate", "settings_for", "base_temperature", "jitter", "forget",
           "KEYS", "DEFAULT_SEED", "DEFAULT_RANGE_PCT", "MAX_RANGE_PCT"]
