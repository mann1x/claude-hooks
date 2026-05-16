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

# M6 (v2): ``tool_executor`` joins the role registry but is OPTIONAL
# and DISABLED by default. When enabled (per-session or globally via
# cfg.roles.tool_executor.enabled = true), the researcher emits a
# semantic ``tool_plan`` block instead of running tools inline, and
# each plan item fans out to a tool_executor Send lane that runs the
# full agent_loop tool subloop with a tool-call-specialist model
# (default gemma4:31b-cloud). The role is omitted from v1
# parity-check enabled lists (handled by the v1 config-load fallback)
# so existing sessions are unaffected.
ROLES: tuple[str, ...] = (
    "planner", "researcher", "tool_executor", "critic", "synthesizer",
)
# Synthesizer alone is mandatory — every other role is opt-out (or in
# tool_executor's case opt-in via cfg.roles.tool_executor.enabled).
MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})

DEFAULT_TOPOLOGY = "council"
VALID_TOPOLOGIES: tuple[str, ...] = ("council",)  # roundtable / freeform later

DEFAULT_EFFORT = "medium"
EFFORT_BUDGETS: dict[str, int] = {
    "low": 1,
    "medium": 3,
    "high": 5,
    "max": 25,
    # x-prefixed tiers (Phase 9): same follow-up budget as the base
    # tier but the consultation runs each researcher lane against
    # every configured ``roles.researcher.extra_models`` model in
    # parallel. xmax (Phase 10) additionally fans out the critic
    # across ``roles.critic.extra_models`` with a meta-critic
    # combine. Extras are silently ignored at non-x tiers, so a
    # benchmark labeled ``high`` is never accidentally 3× the cost.
    "xmedium": 3,
    "xhigh": 5,
    "xmax": 25,
}


def base_effort(effort: str) -> str:
    """Strip the ``x`` prefix from x-tiers; returns the base tier
    whose caps and budget should be used. ``"xhigh"`` -> ``"high"``;
    ``"high"`` -> ``"high"``."""
    if effort.startswith("x") and effort[1:] in ("low", "medium", "high", "max"):
        return effort[1:]
    return effort


def extras_active(effort: str) -> bool:
    """True when the tier is x-prefixed — i.e. the engine should
    fan out to ``extra_models`` for fan-outable roles. False for
    every base tier; ``extra_models`` is unused at those tiers."""
    return effort.startswith("x") and effort[1:] in (
        "low", "medium", "high", "max"
    )

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
    # Phase 9: additional Ollama tags consulted at x-prefixed effort
    # tiers (xmedium / xhigh / xmax). For fan-outable roles
    # (researcher, critic) each plan-item lane spawns one researcher
    # per [model] + extra_models entry, so the synthesizer / meta-
    # critic sees diverse perspectives. Strictly opt-in via tier;
    # silently ignored at low/medium/high/max so a base-tier
    # consultation always behaves as before.
    #
    # Validation rules applied at load time:
    #   - dedup'd against ``model`` (primary); a duplicate is
    #     dropped with a warning so users can put their primary in
    #     the list without doubling work.
    #   - empty / non-string entries dropped silently.
    # Tools-capability validation against /api/tags is the runner's
    # job (we don't want a config load to require network).
    extra_models: list[str] = field(default_factory=list)


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
    # M6: tool_executor is a tool-call specialist — gemma4 is a
    # non-reasoning model, and disabling think on every tool-call
    # ChatClient request keeps the response shape clean for the
    # agent_loop runner. Override per-config if running with a
    # reasoning-capable specialist.
    "tool_executor": False,
    "critic":      "medium",
    "synthesizer": "high",
}


# M6: per-role model defaults. Every role except tool_executor uses
# the global DEFAULT_MODEL — that preserves v1 behavior for
# planner/researcher/critic/synthesizer (they keep tracking the
# user-set DEFAULT_MODEL across upgrades). tool_executor uniquely
# defaults to ``gemma4:31b-cloud`` because the user's observation +
# the M11c bench will confirm gemma4 leads frontier models on
# tool-calling fluency on this proxy.
DEFAULT_MODEL_BY_ROLE: dict[str, str] = {
    "tool_executor": "gemma4:31b-cloud",
}


# M6: tool_executor is the one role that ships disabled-by-default.
# Every other role's RoleConfig starts ``enabled=True``; the runner
# strips disabled roles from the compiled graph topology. Opting in
# is one TOML line: ``[role.tool_executor] enabled = true``.
DEFAULT_ENABLED_BY_ROLE: dict[str, bool] = {
    "tool_executor": False,
}


def _default_role_config(role: str) -> "RoleConfig":
    """Return the per-role boot-time defaults.

    Centralizes the special-casing for tool_executor (different
    primary model + disabled-by-default) so the dataclass factory
    on ``ConsultantsConfig.roles`` stays a one-liner and every
    role-iteration site sees consistent defaults.
    """
    return RoleConfig(
        enabled=DEFAULT_ENABLED_BY_ROLE.get(role, True),
        model=DEFAULT_MODEL_BY_ROLE.get(role, DEFAULT_MODEL),
    )


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


VALID_CHECKPOINTER_BACKENDS: tuple[str, ...] = ("sqlite", "postgres")
DEFAULT_CHECKPOINTER_BACKEND = "sqlite"


@dataclass
class CheckpointerConfig:
    """v2 checkpointer settings.

    Default is per-session SQLite under
    ``<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db`` — zero
    new dependency. Postgres is opt-in via the [postgres] extra
    (``pip install -e 'consultants[postgres]'``); set ``backend =
    "postgres"`` AND a connection ``url`` to enable.
    """
    backend: str = DEFAULT_CHECKPOINTER_BACKEND
    url: Optional[str] = None
    postgres_pool_min: int = 1
    postgres_pool_max: int = 10
    postgres_pool_timeout_s: float = 30.0


@dataclass
class RuntimeConfig:
    """Per-session runtime knobs orthogonal to topology / effort.

    These are **boot-time defaults**; the v2 ``RuntimeControl`` channel
    is the live mutable surface that ``graph.update_state`` rewrites
    via the HTTP control endpoints. Whatever the user puts here seeds
    the channel on session start.

    Fields:

    - ``review_before_synthesis`` (default ``False``) — when ``True``,
      compile the graph with ``interrupt_before=["synthesizer"]`` so
      execution pauses just before the synthesizer composes the final
      answer. The HTTP ``GET /state`` endpoint exposes the partial
      research so the human can preview + inject before approving.
    - ``interrupt_on_low_confidence`` (default ``False``) — opt-in
      dynamic interrupt when the synthesizer's self-rating dips
      below ``confidence_target``. Off by default because the same
      signal normally drives xauto escalation, not a human pause.
    """
    review_before_synthesis: bool = False
    interrupt_on_low_confidence: bool = False


@dataclass
class ConsultantsConfig:
    topology: str = DEFAULT_TOPOLOGY
    effort: str = DEFAULT_EFFORT
    service: ServiceConfig = field(default_factory=ServiceConfig)
    checkpointer: CheckpointerConfig = field(default_factory=CheckpointerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    roles: dict[str, RoleConfig] = field(default_factory=lambda: {
        r: _default_role_config(r) for r in ROLES
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
        extra_models=list(base.extra_models),
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
    if "extra_models" in override:
        raw_extras = override["extra_models"]
        if isinstance(raw_extras, list):
            out.extra_models = _sanitize_extras(raw_extras, primary=out.model)
    return out


def _sanitize_extras(values: list, *, primary: str) -> list[str]:
    """Drop empties / non-strings, dedup, and strip the primary
    model so set+extras can be written verbatim by users without
    double-running the primary."""
    seen: set[str] = set()
    out: list[str] = []
    primary_norm = (primary or "").strip()
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s:
            continue
        if s == primary_norm:
            # Drop silently; primary is always-on regardless of
            # extras.
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
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

    # checkpointer (v2)
    cp = raw.get("checkpointer") or {}
    if isinstance(cp, dict):
        if isinstance(cp.get("backend"), str) \
                and cp["backend"] in VALID_CHECKPOINTER_BACKENDS:
            base.checkpointer.backend = cp["backend"]
        if isinstance(cp.get("url"), str) and cp["url"].strip():
            base.checkpointer.url = cp["url"].strip()
        if isinstance(cp.get("postgres_pool_min"), int) \
                and cp["postgres_pool_min"] >= 1:
            base.checkpointer.postgres_pool_min = cp["postgres_pool_min"]
        if isinstance(cp.get("postgres_pool_max"), int) \
                and cp["postgres_pool_max"] >= 1:
            base.checkpointer.postgres_pool_max = cp["postgres_pool_max"]
        if isinstance(cp.get("postgres_pool_timeout_s"), (int, float)) \
                and cp["postgres_pool_timeout_s"] > 0:
            base.checkpointer.postgres_pool_timeout_s = \
                float(cp["postgres_pool_timeout_s"])

    # runtime (M5)
    rt = raw.get("runtime") or {}
    if isinstance(rt, dict):
        if "review_before_synthesis" in rt:
            base.runtime.review_before_synthesis = \
                bool(rt["review_before_synthesis"])
        if "interrupt_on_low_confidence" in rt:
            base.runtime.interrupt_on_low_confidence = \
                bool(rt["interrupt_on_low_confidence"])

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
    L.append("[checkpointer]")
    L.append(f"backend = {_toml_str(cfg.checkpointer.backend)}")
    if cfg.checkpointer.url:
        L.append(f"url = {_toml_str(cfg.checkpointer.url)}")
    else:
        # Emit an empty-key line as a hint for hand-edit; commented
        # out so non-postgres configs round-trip cleanly.
        L.append("# url = \"postgresql://user:pass@host:5432/db\"  # required when backend = \"postgres\"")
    L.append(f"postgres_pool_min = {cfg.checkpointer.postgres_pool_min}")
    L.append(f"postgres_pool_max = {cfg.checkpointer.postgres_pool_max}")
    L.append(f"postgres_pool_timeout_s = {cfg.checkpointer.postgres_pool_timeout_s}")
    L.append("")
    L.append("[runtime]")
    L.append("# review_before_synthesis: pause before the final answer to "
             "approve / inject")
    L.append("review_before_synthesis = "
             f"{'true' if cfg.runtime.review_before_synthesis else 'false'}")
    L.append("# interrupt_on_low_confidence: opt-in HITL when synthesizer "
             "self-rates below confidence_target")
    L.append("interrupt_on_low_confidence = "
             f"{'true' if cfg.runtime.interrupt_on_low_confidence else 'false'}")
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
        if rc.extra_models:
            inner = ", ".join(_toml_str(m) for m in rc.extra_models)
            L.append(f"extra_models = [{inner}]")
        else:
            # Always emit the key even when empty so hand-editors
            # know it exists; cheaper than docs-spelunking.
            L.append("extra_models = []")
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
             add_extra_model: Optional[str] = None,
             remove_extra_model: Optional[str] = None,
             clear_extras: bool = False,
             scope: str = "user",
             cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Mutate one role's config and persist.

    Phase 9 added the multi-model knobs:

    - ``add_extra_model``: append an Ollama tag to ``extra_models``.
      No-op + idempotent if the tag is already present or matches the
      primary; raises if empty.
    - ``remove_extra_model``: drop an entry; idempotent if absent.
    - ``clear_extras``: empty the list. Useful for reverting an
      x-tier benchmark prep.

    Researcher and critic are the only fan-outable roles. The extras
    knobs are accepted on planner / synthesizer too:

    - ``planner.extra_models`` is currently unused at runtime
      (planner doesn't fan out and doesn't have a fallback path).
    - ``synthesizer.extra_models`` is repurposed (2026-05-07) as a
      serial **failure-fallback** chain — when the primary
      synthesizer model exhausts its retry budget on a cloud flap,
      the engine walks this list in order before declaring the
      consultation failed. Active at every effort tier (not gated
      by x-prefix). NOT a fan-out.
    """
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
        # Re-sanitize extras against the new primary so a swap
        # doesn't leave the primary in extras.
        rc.extra_models = _sanitize_extras(rc.extra_models, primary=rc.model)
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
    if clear_extras:
        rc.extra_models = []
    if add_extra_model is not None:
        tag = add_extra_model.strip()
        if not tag:
            raise ValueError("add_extra_model must be non-empty")
        rc.extra_models = _sanitize_extras(
            rc.extra_models + [tag], primary=rc.model,
        )
    if remove_extra_model is not None:
        tag = remove_extra_model.strip()
        rc.extra_models = [m for m in rc.extra_models if m != tag]
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
