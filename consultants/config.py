"""Configuration for the /consultants engine.

Two-tier:

- **User-global**: ``~/.claude/consultants-config.toml`` — defaults
  and per-role model assignments. Persists across projects.
- **Per-project**: ``<cwd>/.claude-hooks/consultants.toml`` —
  overrides specific keys for one repo. Optional.

Project-level keys override user-global on a per-key basis (a
project can override just the planner's model and inherit everything
else). Missing files fall back to defaults.

The smart-start daemon flag lives separately in
``config/claude-hooks.json`` under ``hooks.consultants.smart_start.*``
because the daemon owns it — see ``claude_hooks.config``. This module
deliberately stays out of that file so we don't duplicate state.

Stdlib only. We hand-roll a tiny TOML emitter for our fixed schema
because there's no stdlib TOML writer (``tomllib`` is read-only) and
adding ``tomli-w`` would mean pulling another dep into the main test
env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]


# ----------------------- defaults ----------------------------------- #

ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")
MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})

DEFAULT_TOPOLOGY = "council"
VALID_TOPOLOGIES: tuple[str, ...] = ("council",)  # roundtable / freeform later

DEFAULT_EFFORT = "medium"
EFFORT_BUDGETS: dict[str, int] = {
    "low": 1,
    "medium": 3,
    "high": 5,
    "max": 25,
}

DEFAULT_HTTP_PORT = 38095
DEFAULT_MODEL = "kimi-k2.6:cloud"

VALID_SERVICE_MODES: tuple[str, ...] = ("always-on", "smart-start")
DEFAULT_SERVICE_MODE = "always-on"


# ----------------------- types -------------------------------------- #

@dataclass
class RoleConfig:
    enabled: bool = True
    model: str = DEFAULT_MODEL
    ctx_max: Optional[int] = None
    ctx_max_explicit: bool = False
    # Reasoning effort. ``None`` -> fall back to DEFAULT_THINK_BY_ROLE
    # for the role; ``False`` -> explicitly disable reasoning (good
    # for non-reasoning models or when the role doesn't need it);
    # ``"low" | "medium" | "high" | True`` -> pass through to the
    # upstream model. The chat client gracefully degrades to no-think
    # if the model returns 400 on the ``think`` field.
    think: Any = None


# Per-role think defaults. Tuned from the 2026-05-07 trace
# (csl-...-647b smoke = 488s wall, 100% LLM time): the planner
# spends 84s producing a 1481-token plan that's mostly cloud
# reasoning tokens; the critic spends 192s on a smoke prompt for
# 5 lines of decisive output. Lowering reasoning effort on
# decomposition + routing roles cuts wall time without affecting
# the visible answer. Researcher and synthesizer keep ``high``
# because they actually use the reasoning chain.
DEFAULT_THINK_BY_ROLE: dict[str, Any] = {
    "planner":     "medium",
    "researcher":  "high",
    "critic":      "medium",
    "synthesizer": "high",
}


def role_think(cfg: "ConsultantsConfig", role: str) -> Any:
    """Return the effective ``think`` value for a role.

    Priority: explicit role.think (when not None) -> per-role
    default -> ``True`` (Ollama's "reasoning on" sentinel).
    """
    rc = cfg.roles.get(role)
    if rc is not None and rc.think is not None:
        return rc.think
    return DEFAULT_THINK_BY_ROLE.get(role, True)


@dataclass
class ServiceConfig:
    mode: str = DEFAULT_SERVICE_MODE
    http_port: int = DEFAULT_HTTP_PORT


@dataclass
class ConsultantsConfig:
    topology: str = DEFAULT_TOPOLOGY
    effort: str = DEFAULT_EFFORT
    service: ServiceConfig = field(default_factory=ServiceConfig)
    roles: dict[str, RoleConfig] = field(default_factory=lambda: {
        r: RoleConfig() for r in ROLES
    })

    def role(self, name: str) -> RoleConfig:
        if name not in ROLES:
            raise ValueError(
                f"unknown role: {name!r}. Valid: {', '.join(ROLES)}"
            )
        return self.roles[name]

    @property
    def effort_budget(self) -> int:
        return EFFORT_BUDGETS.get(self.effort, EFFORT_BUDGETS[DEFAULT_EFFORT])


# ----------------------- paths -------------------------------------- #

def user_config_path() -> Path:
    return Path.home() / ".claude" / "consultants-config.toml"


def project_config_path(cwd: Path) -> Path:
    return cwd / ".claude-hooks" / "consultants.toml"


# ----------------------- load --------------------------------------- #

def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge_role(base: RoleConfig, override: dict) -> RoleConfig:
    out = RoleConfig(
        enabled=base.enabled,
        model=base.model,
        ctx_max=base.ctx_max,
        ctx_max_explicit=base.ctx_max_explicit,
        think=base.think,
    )
    if "enabled" in override:
        out.enabled = bool(override["enabled"])
    if "model" in override and isinstance(override["model"], str):
        out.model = override["model"]
    if "ctx_max" in override:
        v = override["ctx_max"]
        if isinstance(v, int) and v > 0:
            out.ctx_max = v
        else:
            out.ctx_max = None
    if "ctx_max_explicit" in override:
        out.ctx_max_explicit = bool(override["ctx_max_explicit"])
    # ``think`` accepts: bool, "low" | "medium" | "high", or None to
    # clear and fall back to DEFAULT_THINK_BY_ROLE. Anything else is
    # ignored silently (TOML can't really emit None, so missing is
    # the usual "no override").
    if "think" in override:
        v = override["think"]
        if isinstance(v, bool):
            out.think = v
        elif isinstance(v, str) and v.lower() in ("low", "medium", "high"):
            out.think = v.lower()
        elif v is None:
            out.think = None
    return out


def _merge_layer(base: ConsultantsConfig, raw: dict) -> ConsultantsConfig:
    """Apply a TOML dict on top of ``base``. Fields absent from raw
    keep their base value (per-key override semantics)."""
    if not raw:
        return base

    # top-level
    if isinstance(raw.get("topology"), str) and raw["topology"] in VALID_TOPOLOGIES:
        base.topology = raw["topology"]
    if isinstance(raw.get("effort"), str) and raw["effort"] in EFFORT_BUDGETS:
        base.effort = raw["effort"]

    # service
    svc = raw.get("service") or {}
    if isinstance(svc, dict):
        if isinstance(svc.get("mode"), str) and svc["mode"] in VALID_SERVICE_MODES:
            base.service.mode = svc["mode"]
        if isinstance(svc.get("http_port"), int) and 1 <= svc["http_port"] <= 65535:
            base.service.http_port = svc["http_port"]

    # roles
    roles = raw.get("role") or {}
    if isinstance(roles, dict):
        for r in ROLES:
            r_raw = roles.get(r)
            if isinstance(r_raw, dict):
                base.roles[r] = _merge_role(base.roles[r], r_raw)

    # Synthesizer can never be disabled regardless of what the file says.
    base.roles["synthesizer"].enabled = True

    return base


def load_config(cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Load merged config: defaults < user-global < per-project.
    ``cwd=None`` skips the project layer (useful for daemon contexts
    that don't have a project root)."""
    cfg = ConsultantsConfig()
    cfg = _merge_layer(cfg, _read_toml(user_config_path()))
    if cwd is not None:
        cfg = _merge_layer(cfg, _read_toml(project_config_path(cwd)))
    return cfg


# ----------------------- save --------------------------------------- #
# Hand-rolled TOML emitter for our fixed schema. Sufficient because
# our values are str/int/bool/None — no escaping pitfalls beyond
# strings, which we double-quote and escape conservatively.

_TOML_NEEDS_ESCAPE = {"\\": "\\\\", "\"": "\\\"", "\n": "\\n",
                     "\r": "\\r", "\t": "\\t"}


def _toml_str(value: str) -> str:
    parts: list[str] = []
    for ch in value:
        parts.append(_TOML_NEEDS_ESCAPE.get(ch, ch))
    return '"' + "".join(parts) + '"'


def _render(cfg: ConsultantsConfig) -> str:
    L: list[str] = []
    L.append("# claude-hooks /consultants engine config.")
    L.append("# This file is managed by `claude-consultants config set-*`")
    L.append("# but is also safe to hand-edit.")
    L.append("")
    L.append(f"topology = {_toml_str(cfg.topology)}")
    L.append(f"effort = {_toml_str(cfg.effort)}")
    L.append("")
    L.append("[service]")
    L.append(f"mode = {_toml_str(cfg.service.mode)}")
    L.append(f"http_port = {cfg.service.http_port}")
    L.append("")
    for role in ROLES:
        rc = cfg.roles[role]
        L.append(f"[role.{role}]")
        L.append(f"enabled = {'true' if rc.enabled else 'false'}")
        L.append(f"model = {_toml_str(rc.model)}")
        if rc.ctx_max is not None:
            L.append(f"ctx_max = {int(rc.ctx_max)}")
        L.append(f"ctx_max_explicit = "
                 f"{'true' if rc.ctx_max_explicit else 'false'}")
        L.append("")
    return "\n".join(L)


def save_config(cfg: ConsultantsConfig, *, scope: str = "user",
                cwd: Optional[Path] = None) -> Path:
    """Save the config. ``scope='user'`` writes to ~/.claude/...; any
    other value writes to the per-project file under cwd."""
    if scope == "user":
        path = user_config_path()
    else:
        if cwd is None:
            raise ValueError("project scope requires cwd")
        path = project_config_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _render(cfg)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ----------------------- mutators ----------------------------------- #
# Each mutator loads, modifies, saves to user scope (the typical case).
# ``cwd=...`` shifts to project scope.

def _save_after_change(cfg: ConsultantsConfig, *,
                       scope: str, cwd: Optional[Path]) -> ConsultantsConfig:
    save_config(cfg, scope=scope, cwd=cwd)
    return cfg


def set_role(role: str, *, model: Optional[str] = None,
             ctx_max: Optional[int] = None,
             enabled: Optional[bool] = None,
             scope: str = "user",
             cwd: Optional[Path] = None) -> ConsultantsConfig:
    if role not in ROLES:
        raise ValueError(
            f"unknown role: {role!r}. Valid: {', '.join(ROLES)}"
        )
    if enabled is False and role in MANDATORY_ROLES:
        raise ValueError(
            f"role {role!r} is mandatory and cannot be disabled"
        )
    cfg = load_config(cwd if scope != "user" else None)
    rc = cfg.roles[role]
    if model is not None:
        if not model.strip():
            raise ValueError("model must be non-empty")
        rc.model = model.strip()
    if ctx_max is not None:
        if ctx_max == 0:
            rc.ctx_max = None
            rc.ctx_max_explicit = False
        elif ctx_max < 0:
            raise ValueError("ctx_max must be >= 0 (0 = auto)")
        else:
            rc.ctx_max = ctx_max
            rc.ctx_max_explicit = True
    if enabled is not None:
        rc.enabled = bool(enabled)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_effort(effort: str, *, scope: str = "user",
               cwd: Optional[Path] = None) -> ConsultantsConfig:
    if effort not in EFFORT_BUDGETS:
        raise ValueError(
            f"effort must be one of: {', '.join(sorted(EFFORT_BUDGETS))}"
        )
    cfg = load_config(cwd if scope != "user" else None)
    cfg.effort = effort
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_service_mode(mode: str, *, scope: str = "user",
                     cwd: Optional[Path] = None) -> ConsultantsConfig:
    if mode not in VALID_SERVICE_MODES:
        raise ValueError(
            f"mode must be one of: {', '.join(VALID_SERVICE_MODES)}"
        )
    cfg = load_config(cwd if scope != "user" else None)
    cfg.service.mode = mode
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_topology(topology: str, *, scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    if topology not in VALID_TOPOLOGIES:
        raise ValueError(
            f"topology must be one of: {', '.join(VALID_TOPOLOGIES)}"
        )
    cfg = load_config(cwd if scope != "user" else None)
    cfg.topology = topology
    return _save_after_change(cfg, scope=scope, cwd=cwd)


# ----------------------- helpers ------------------------------------ #

def enabled_roles(cfg: ConsultantsConfig) -> list[str]:
    """Roles flagged enabled, in canonical pipeline order."""
    return [r for r in ROLES if cfg.roles[r].enabled]


def validate_pipeline(cfg: ConsultantsConfig) -> Optional[str]:
    """Return a human-readable error if the enabled-role set can't
    form a valid council pipeline; None if valid.

    Rules:
    - synthesizer must be enabled (enforced by load).
    - At least one of {planner, researcher} must be enabled — without
      either, synthesizer has no input beyond the bare question.
    """
    enabled = enabled_roles(cfg)
    if "synthesizer" not in enabled:
        return "synthesizer is mandatory and must be enabled"
    if not ({"planner", "researcher"} & set(enabled)):
        return ("at least one of planner / researcher must be enabled "
                "so synthesizer has an input beyond the raw question")
    return None
