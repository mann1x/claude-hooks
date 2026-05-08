"""Persistent configuration for the ``/get-advice`` skill.

Settings live in ``~/.claude/get-advice-config.json`` (user-global).
Schema:

    {
      "model": "deepseek-v4-pro:cloud",
      "ctx_max": 65536,
      "ctx_max_explicit": true,
      "effort": "medium",
      "reset_threshold": 0.85,
      "tools": ["read_file", "grep", "glob", "list_files",
                "survey_project", "recall_memory"]
    }

All keys optional — defaults applied below when missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_MODEL = "qwen3.5:cloud"
DEFAULT_EFFORT = "medium"
DEFAULT_RESET_THRESHOLD = 0.85

KNOWN_TOOLS: tuple[str, ...] = (
    "read_file",
    "grep",
    "glob",
    "list_files",
    "survey_project",
    "recall_memory",
)

EFFORT_BUDGETS = {
    "low": 1,
    "medium": 3,
    "high": 5,
    "max": 25,
}


def config_path() -> Path:
    return Path.home() / ".claude" / "get-advice-config.json"


@dataclass
class AdvisorConfig:
    model: str = DEFAULT_MODEL
    ctx_max: Optional[int] = None
    ctx_max_explicit: bool = False
    effort: str = DEFAULT_EFFORT
    reset_threshold: float = DEFAULT_RESET_THRESHOLD
    # Empty list/None = no tools (pure chat). ["all"] is normalized to
    # the full KNOWN_TOOLS list at load time.
    tools: list[str] = field(default_factory=lambda: list(KNOWN_TOOLS))

    def to_dict(self) -> dict:
        d: dict = {
            "model": self.model,
            "effort": self.effort,
            "reset_threshold": self.reset_threshold,
            "tools": list(self.tools),
            "ctx_max_explicit": self.ctx_max_explicit,
        }
        if self.ctx_max is not None:
            d["ctx_max"] = self.ctx_max
        return d

    @property
    def effort_budget(self) -> int:
        return EFFORT_BUDGETS.get(self.effort, EFFORT_BUDGETS[DEFAULT_EFFORT])


def _normalize_tools(raw) -> list[str]:
    """Map a tools value (list/str/None) to a canonical list. Unknown
    names are dropped silently here; ``set_tools()`` validates upstream.
    Special tokens: ``all`` expands to KNOWN_TOOLS, ``none`` collapses
    to []."""
    if raw is None:
        return list(KNOWN_TOOLS)
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, Iterable):
        items = [str(t).strip() for t in raw if str(t).strip()]
    else:
        return list(KNOWN_TOOLS)
    if not items:
        return []
    lowered = [t.lower() for t in items]
    if "none" in lowered:
        return []
    if "all" in lowered:
        return list(KNOWN_TOOLS)
    return [t for t in items if t in KNOWN_TOOLS]


def load_config(path: Optional[Path] = None) -> AdvisorConfig:
    """Load config from disk, applying defaults for any missing keys.
    Missing file => all defaults."""
    p = path or config_path()
    cfg = AdvisorConfig()
    if not p.exists():
        return cfg
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    if isinstance(raw.get("model"), str) and raw["model"]:
        cfg.model = raw["model"]
    cm = raw.get("ctx_max")
    if isinstance(cm, int) and cm > 0:
        cfg.ctx_max = cm
    cfg.ctx_max_explicit = bool(raw.get("ctx_max_explicit", False))
    if isinstance(raw.get("effort"), str) and raw["effort"] in EFFORT_BUDGETS:
        cfg.effort = raw["effort"]
    rt = raw.get("reset_threshold")
    if isinstance(rt, (int, float)) and 0 < float(rt) <= 1:
        cfg.reset_threshold = float(rt)
    if "tools" in raw:
        cfg.tools = _normalize_tools(raw["tools"])
    return cfg


def save_config(cfg: AdvisorConfig, path: Optional[Path] = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), indent=2, sort_keys=True) + "\n")


def set_model(name: str, ctx_max: Optional[int] = None,
              path: Optional[Path] = None) -> AdvisorConfig:
    if not name or not name.strip():
        raise ValueError("model name must be non-empty")
    cfg = load_config(path)
    cfg.model = name.strip()
    if ctx_max is not None:
        if ctx_max <= 0:
            raise ValueError("ctx_max must be a positive integer")
        cfg.ctx_max = ctx_max
        cfg.ctx_max_explicit = True
    else:
        # Switching model invalidates a prior auto-detected ctx; only
        # keep an explicit value because the user pinned it on a prior
        # call to /get-advice--model NAME CTX.
        if not cfg.ctx_max_explicit:
            cfg.ctx_max = None
    save_config(cfg, path)
    return cfg


def set_effort(effort: str, path: Optional[Path] = None) -> AdvisorConfig:
    if effort not in EFFORT_BUDGETS:
        valid = ", ".join(sorted(EFFORT_BUDGETS))
        raise ValueError(f"effort must be one of: {valid}")
    cfg = load_config(path)
    cfg.effort = effort
    save_config(cfg, path)
    return cfg


def set_tools(spec: str, path: Optional[Path] = None) -> AdvisorConfig:
    """Set the tool list from a CSV string. ``all`` and ``none`` are
    accepted as special tokens. Unknown names raise ValueError."""
    if not spec or not spec.strip():
        raise ValueError("tool spec must be non-empty")
    items = [t.strip() for t in spec.split(",") if t.strip()]
    lowered = [t.lower() for t in items]
    cfg = load_config(path)
    if "none" in lowered:
        cfg.tools = []
        save_config(cfg, path)
        return cfg
    if "all" in lowered:
        cfg.tools = list(KNOWN_TOOLS)
        save_config(cfg, path)
        return cfg
    unknown = [t for t in items if t not in KNOWN_TOOLS]
    if unknown:
        valid = ", ".join(KNOWN_TOOLS)
        raise ValueError(
            f"unknown tool(s): {', '.join(unknown)}. Valid: {valid}, "
            f"or 'all'/'none'."
        )
    # De-dup but preserve user-supplied order so reports are stable.
    seen = set()
    cfg.tools = [t for t in items if not (t in seen or seen.add(t))]
    save_config(cfg, path)
    return cfg
