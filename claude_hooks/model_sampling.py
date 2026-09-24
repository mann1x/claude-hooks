"""Sampling templates matched to model names, sent with every request.

A cloud model runs on the provider's default sampler unless the request
says otherwise, and that default is not always one the model does well
at: glm-5.3 at the cloud default (1.0) answered poorly, at 0.2 broke
outright, and at 0.7 worked well (measured by hand in Cerebriline's
prompt-template work, 2026-09). The setting belongs in the request, not
in an Ollama Modelfile overlay — an overlay is host state that can be
re-pulled, recreated or absent on another host, and nothing tells the
caller when it is.

Two layers, one table:

- **Shipped** — ``config/model-sampling.json``, tracked in the repo and
  updated by releases. Nobody edits it locally.
- **User** — ``model_sampling.templates`` in ``config/claude-hooks.json``,
  the file install and reinstall never overwrite::

    "model_sampling": {
      "templates": {
        "glm-5.3*":             {"top_p": 0.95},          // adds to shipped 0.7
        "minimax-m3*":          {"temperature": 0.6},     // a new template
        "gemma4*":              null                      // turn one off
      }
    }

- A pattern the user names **merges field by field** with ours, so an
  entry that only sets ``top_p`` keeps the shipped temperature.
- ``null`` on a field drops it (the provider default is used); ``null``
  on a pattern disables that template.
- The pattern with the most literal characters wins
  (``glm-5.3-flash*`` beats ``glm-5.3*`` beats ``*``).
- ``CLAUDE_HOOKS_MODEL_SAMPLING`` (a JSON templates object) merges over
  both for one process, the same way — a benchmark arm, not a setting.
- Only fields a template sets are sent. An unset field is not a zero:
  it is the provider's default, the honest setting until a value has
  been measured. ``{}`` means "send nothing".
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Ollama sampler fields a template may set. Anything else is dropped
#: with a warning — a typo must not become a silent no-op.
FIELDS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "typical_p",
    "repeat_penalty", "repeat_last_n", "presence_penalty",
    "frequency_penalty", "seed", "mirostat", "mirostat_tau", "mirostat_eta",
})


SHIPPED_FILE = Path(__file__).resolve().parent.parent / "config" / "model-sampling.json"


def merged_raw(shipped: dict, user: dict) -> dict:
    """User templates over shipped ones: a pattern in both merges field
    by field (user wins, ``None`` kept so it can drop a field later); a
    user ``None`` pattern replaces the shipped one outright."""
    out = {p: (dict(o) if isinstance(o, dict) else o) for p, o in shipped.items()}
    for pattern, opts in user.items():
        if isinstance(opts, dict) and isinstance(out.get(pattern), dict):
            out[pattern].update(opts)
        else:
            out[pattern] = opts
    return out


def templates(cfg: Optional[dict] = None,
              shipped: Optional[dict] = None) -> dict[str, dict]:
    """The effective ``pattern -> options`` table: the shipped file with
    the user's config merged over it, nulls, notes (``_``-keys) and
    unknown fields removed."""
    live = cfg is None
    if live:
        cfg = _cached_json("user")
    if shipped is None:
        shipped = (_cached_json("shipped") or {}).get("templates") or {}
    user = ((cfg or {}).get("model_sampling") or {}).get("templates") or {}
    raw = merged_raw(shipped, user)
    if live:
        raw = merged_raw(raw, _env_templates())
    out: dict[str, dict] = {}
    for pattern, opts in raw.items():
        if opts is None:
            continue  # disabled by the user
        if not isinstance(opts, dict):
            log.warning("model_sampling: template %r is not an object", pattern)
            continue
        opts = {k: v for k, v in opts.items() if not k.startswith("_")}
        unknown = set(opts) - FIELDS
        if unknown:
            log.warning("model_sampling: template %r ignores unknown field(s) %s",
                        pattern, sorted(unknown))
        out[pattern] = {k: v for k, v in opts.items()
                        if k in FIELDS and v is not None}
    return out


#: A JSON templates object merged over the user's file, same rules: one
#: process (a benchmark arm) can run other sampling without editing the
#: config every hook on the host reads.
ENV_VAR = "CLAUDE_HOOKS_MODEL_SAMPLING"


def _env_templates() -> dict:
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("model_sampling: %s is not JSON: %s", ENV_VAR, e)
        return {}
    if not isinstance(data, dict):
        log.warning("model_sampling: %s must be a JSON object", ENV_VAR)
        return {}
    return data


_CACHE: dict = {}


def _cached_json(which: str) -> dict:
    """The user config (merged over DEFAULT_CONFIG) or the shipped
    templates file, re-read only when the file's mtime changes: this runs
    on every request, and an edit must take effect without a restart."""
    from claude_hooks.config import default_config_path, load_config
    path = default_config_path() if which == "user" else SHIPPED_FILE
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    hit = _CACHE.get(which)
    if hit is None or hit[0] != stamp:
        if which == "user":
            data = load_config(path)
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                if stamp is not None:
                    log.warning("model_sampling: cannot read %s: %s", path, e)
                data = {}
        _CACHE[which] = (stamp, data)
    return _CACHE[which][1]


def _specificity(pattern: str) -> int:
    return sum(ch not in "*?[]" for ch in pattern)


def match(model: str, table: dict[str, dict]) -> Optional[str]:
    """The most specific pattern in ``table`` that matches ``model``."""
    hits = [p for p in table if fnmatch.fnmatchcase(model, p)]
    return max(hits, key=_specificity) if hits else None


def resolve(model: str, cfg: Optional[dict] = None
            ) -> tuple[dict, Optional[str]]:
    """``(options, pattern)`` for ``model``; ``({}, None)`` when no
    template matches."""
    table = templates(cfg)
    pattern = match(model, table)
    return (dict(table[pattern]), pattern) if pattern else ({}, None)


def sampling_for(model: str, cfg: Optional[dict] = None) -> dict:
    """The options to send for ``model`` (a copy, safe to extend)."""
    return resolve(model, cfg)[0]


def apply(payload: dict, cfg: Optional[dict] = None) -> dict:
    """Merge the model's template into ``payload["options"]``. A value
    the caller already put in ``options`` wins: an explicit per-call
    setting is more specific than any template."""
    opts = sampling_for(payload.get("model", ""), cfg)
    if opts:
        merged = dict(opts)
        merged.update(payload.get("options") or {})
        payload["options"] = merged
    return payload


def parse_options(pairs: list[str]) -> dict:
    """``["repeat_penalty=1.1", "repeat_last_n=2048"]`` -> options. Values
    are JSON (``null`` drops a field); a field outside :data:`FIELDS` is
    an error here, since a CLI typo would otherwise measure nothing."""
    out: dict = {}
    for pair in pairs:
        field, sep, raw = pair.partition("=")
        field = field.strip()
        if not sep or field not in FIELDS:
            raise ValueError(f"not a sampler option: {pair!r} "
                             f"(fields: {', '.join(sorted(FIELDS))})")
        try:
            out[field] = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"{field}: {raw!r} is not a number or null")
    return out


def label(model: str, options: dict) -> str:
    """A name that records the sampling a result was produced under, so
    results from different settings never share a key:
    ``glm-5.3-flash:cloud@temperature=0.7``."""
    if not options:
        return model
    return model + "@" + ",".join(f"{k}={options[k]}" for k in sorted(options))


def model_of(label_or_model: str) -> str:
    """Inverse of :func:`label`."""
    return label_or_model.split("@", 1)[0]
