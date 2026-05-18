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

# Imported here at module top to avoid the mid-file import smell.
# ``coder_defaults`` is a sibling-package leaf with no back-edges
# (it imports nothing from this module), so the cycle risk is nil.
from .engine.coder_defaults import (
    RECOMMENDED_CODER_DEFAULT_ROUTE,
    RECOMMENDED_CODER_MODEL,
    RECOMMENDED_CODER_ROUTES_BY_LANGUAGE,
)
from .engine.state_v2 import CoderLanguageRoute


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
    "planner", "researcher", "tool_executor", "critic", "coder",
    "synthesizer",
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
    # M7: ``xauto`` — adaptive effort tier. Starts at the xmedium
    # topology + caps; the ``escalation`` module mutates
    # runtime_control mid-flight to grow to xhigh or xmax when the
    # critic flags ``needs_more_research`` or the synthesizer's
    # self-rated confidence drops below the threshold. Budget caps
    # match xmax's so a worst-case escalation has a follow-up
    # budget compatible with the final tier reached.
    "xauto": 25,
}


def base_effort(effort: str) -> str:
    """Strip the ``x`` prefix from x-tiers; returns the base tier
    whose caps and budget should be used. ``"xhigh"`` -> ``"high"``;
    ``"high"`` -> ``"high"``.

    M7: ``xauto`` resolves to ``"medium"`` — the starting topology
    when the consultation begins. The escalator rewrites
    ``runtime_control`` to grow toward the xhigh/xmax topologies
    in flight; the resolved base never changes during the run.
    """
    if effort.startswith("x") and effort[1:] in ("low", "medium", "high", "max"):
        return effort[1:]
    if effort == "xauto":
        return "medium"
    return effort


def extras_active(effort: str) -> bool:
    """True when the tier is x-prefixed — i.e. the engine should
    fan out to ``extra_models`` for fan-outable roles. False for
    every base tier; ``extra_models`` is unused at those tiers.

    M7: ``xauto`` is an x-tier by definition — it starts at xmedium
    and can escalate to xhigh/xmax, both of which use extras.
    """
    if effort == "xauto":
        return True
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
    # Task #111: per-language coder routing (only consulted for the
    # ``coder`` role; other roles ignore both fields). Keyed by
    # language id from ``coder_defaults.LANGUAGE_BY_EXTENSION``.
    # Empty dict means "no per-language routing — fall through to
    # ``default_route`` (or ``model`` if that's also unset)".
    routes_by_language: dict[str, CoderLanguageRoute] = \
        field(default_factory=dict)
    # ``None`` means "no global default route — fall through to
    # legacy ``model`` field with no failover (v1 back-compat)".
    default_route: Optional[CoderLanguageRoute] = None


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
    # M10: coder is exploratory — high reasoning helps with
    # nontrivial code generation. The M11b bench will tighten this
    # per-model; default to high until evidence says otherwise.
    "coder":       "high",
    "synthesizer": "high",
}


# M6 / M10: per-role model defaults. Every role except
# tool_executor and coder uses the global DEFAULT_MODEL — that
# preserves v1 behavior for planner/researcher/critic/synthesizer
# (they keep tracking the user-set DEFAULT_MODEL across upgrades).
# tool_executor and coder default to model-specific picks grounded
# in the M11b/M11c skill-eval bench results.
#
# - ``tool_executor`` → ``gemma4:31b-cloud`` per the user's
#   observation + the M11c bench (pending).
# - ``coder`` → ``glm-5.1:cloud`` per the 2026-05-16 M11b run
#   (suite v1.0 rubric winner: pass=100%, avg_quality=4.88,
#   median_tokens=1841, median_wall=4.9 s). The constant lives in
#   ``consultants/engine/coder_defaults.py`` and is sourced from
#   the baselines ledger; imported at module top.
DEFAULT_MODEL_BY_ROLE: dict[str, str] = {
    "tool_executor": "gemma4:31b-cloud",
    "coder": RECOMMENDED_CODER_MODEL,
}


# M11c-5 (2026-05-17): tool_executor flipped to enabled-by-default
# after the M11c-2 bench cleared the rubric (gemma4:31b-cloud at
# 87.5% / 5.00) AND task #103 (x-tier proper composition) resolved
# via the M11c-3 engine refactor. See
# ``consultants/engine/tool_executor_defaults.py`` for the bench-
# grounded provenance + the rationale recorded under
# ``project_consultants_v2_103_proper_composition``.
#
# Coder remains disabled-by-default — different decision, gated by
# the operator opting into sandboxed file writes. Opting in is one
# TOML line:
#   [role.coder]          enabled = true
# Disabling tool_executor (if an operator needs the legacy
# researcher-with-inline-tool-subloop topology) is also one line:
#   [role.tool_executor]  enabled = false
DEFAULT_ENABLED_BY_ROLE: dict[str, bool] = {
    "tool_executor": True,
    "coder": False,
}


def _default_role_config(role: str) -> "RoleConfig":
    """Return the per-role boot-time defaults.

    Centralizes the special-casing for tool_executor (different
    primary model + disabled-by-default) so the dataclass factory
    on ``ConsultantsConfig.roles`` stays a one-liner and every
    role-iteration site sees consistent defaults.

    Task #111: the ``coder`` role additionally seeds
    ``routes_by_language`` + ``default_route`` from the v1.0.1-mlang
    bench winners. Other roles leave both fields empty / None so
    the runtime stays a pure-``model`` lookup for them.
    """
    rc = RoleConfig(
        enabled=DEFAULT_ENABLED_BY_ROLE.get(role, True),
        model=DEFAULT_MODEL_BY_ROLE.get(role, DEFAULT_MODEL),
    )
    if role == "coder":
        # Deep-copy the recommended routes — without this every
        # config instance shares the same dict and mutations leak.
        rc.routes_by_language = {
            k: CoderLanguageRoute(primary=v.primary, fallback=v.fallback)
            for k, v in RECOMMENDED_CODER_ROUTES_BY_LANGUAGE.items()
        }
        rc.default_route = CoderLanguageRoute(
            primary=RECOMMENDED_CODER_DEFAULT_ROUTE.primary,
            fallback=RECOMMENDED_CODER_DEFAULT_ROUTE.fallback,
        )
    return rc


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
class StoreTTLConfig:
    """M14: per-namespace TTL for store entries.

    Defaults match the user-locked decisions from
    ``project_consultants_v2_m14_ttl_distillation``:

    - ``research`` (per-session findings) — 30 days. Long enough to
      survive a multi-week project; short enough to keep the index
      from drowning in stale leads.
    - ``tool_results`` (per-session tool outputs) — 24 hours. The
      content is verbatim file contents / grep output; cheap to
      re-fetch, expensive to preserve.
    - ``project`` (per-project distilled memory) — null = never.
      This is the durable bucket distillation writes into.
    - ``user`` (cross-project user-global memory) — null = never.
      Preferences and patterns the user wants kept forever.

    ``refresh_on_read`` (default ``True``) bumps ``expires_at``
    forward on hit so recalled-and-cited findings stay alive — the
    "if it's still useful, keep it" heuristic.
    """
    enabled: bool = False
    research_days: Optional[float] = 30.0
    tool_results_hours: Optional[float] = 24.0
    project_days: Optional[float] = None  # never
    user_days: Optional[float] = None  # never
    refresh_on_read: bool = True

    def ttl_for_namespace(
        self, ns: tuple[str, ...],
    ) -> Optional[float]:
        """Return TTL in seconds for a namespace tuple, or ``None``
        when entries in this namespace should never expire.

        Namespace shape: ``(head, kind)`` where ``head`` is either
        a literal ``"project"`` / ``"user"`` or an opaque ``sid``,
        and ``kind`` is ``"research"`` / ``"tool_results"``.
        """
        if len(ns) != 2:
            return None
        head, kind = ns
        if head == "project":
            return (
                self.project_days * 86400.0
                if self.project_days else None
            )
        if head == "user":
            return (
                self.user_days * 86400.0
                if self.user_days else None
            )
        # Otherwise ``head`` is a sid; kind drives the TTL.
        if kind == "research":
            return (
                self.research_days * 86400.0
                if self.research_days else None
            )
        if kind == "tool_results":
            return (
                self.tool_results_hours * 3600.0
                if self.tool_results_hours else None
            )
        return None


@dataclass
class StoreDistillationConfig:
    """M14: Caliber-style distillation of expiring research entries.

    When the daemon's :class:`~consultants.engine.store_reaper.\
StoreReaperThread` finds expiring research rows, it groups them by
    ``sid`` and calls a distiller LLM to write ONE summary entry
    into the durable ``("project", project_id)`` namespace before
    deleting the originals. Episodic short-term → semantic long-
    term, mirroring human memory consolidation.

    The LLM is invoked with the rubric in
    :mod:`consultants.engine.distillation`: retain file:line
    citations, decisions, gotchas, and open questions; drop process
    narration, retries, and prose padding.

    **Critical invariant**: the daemon only deletes originals after
    a successful summary write. If every model in
    ``[model] + fallback_models`` fails, the originals stay in
    place and the next sweep tick retries.

    Defaults (user-locked 2026-05-17):

    - ``model = "gemma4:31b-cloud"`` — the M11c-2 tool_executor
      winner; already trusted in the council pipeline.
    - ``fallback_models = ["glm-5.1:cloud"]`` — caliber-init
      fallback model; ~64k context window comfortable for prompt
      overflow.
    - ``sweep_interval_seconds = 3600`` — hourly. Cheap on a
      24-hour TTL boundary; safe on a 30-day TTL.
    - ``min_entries_per_distillation = 3`` — cost gate.
      Single-finding sessions just get deleted; no LLM call fires.
    - ``max_session_entries = 50`` — truncate before prompt
      assembly. ~600 chars/entry × 50 ≈ 30 k tokens (gemma's
      32 k ctx ceiling); larger groups overflow to the fallback.
    """
    enabled: bool = False
    model: str = "gemma4:31b-cloud"
    fallback_models: tuple[str, ...] = ("glm-5.1:cloud",)
    sweep_interval_seconds: float = 3600.0
    min_entries_per_distillation: int = 3
    max_session_entries: int = 50


@dataclass
class StoreConfig:
    """M8: long-term memory BaseStore settings.

    A LangGraph :class:`BaseStore` lets researcher lanes recall what
    other lanes already discovered (within a session) and lets the
    follow-up runner semantically replay the parent's research
    (across sessions). The store is **opt-in** and **effort-gated**:

    - ``enabled = false`` (default) — no store is wired; recall + record
      helpers in :mod:`consultants.engine.store` are no-ops. Zero cost.
    - ``backend = "memory"`` — LangGraph's bundled InMemoryStore (per-
      process, no durability). Useful for users who want intra-session
      recall without a database.
    - ``backend = "pgvector"`` — wraps :class:`PgvectorProvider`. Shares
      the Postgres instance the recall hook pipeline already uses.
      Cross-session persistence + KG-style relations.
    - ``backend = "sqlite_vec"`` — file-backed alternative for hosts
      that don't run Postgres.

    The ``enable_at_efforts`` gate keeps the zero-cost path for the
    light tiers and turns the store on for the deep ones, where the
    multi-lane x-tier diversity benefits the most from cross-lane
    recall. Pass ``effort=None`` to the factory to bypass the gate
    entirely (used by the follow-up runner).
    """
    enabled: bool = False
    backend: str = "memory"  # "memory" | "pgvector" | "sqlite_vec"
    # Effort tiers at which the store is wired into the graph. Lower
    # tiers stay zero-cost. ``xauto`` is explicitly included because
    # it inherits the xmedium baseline and can escalate; we want the
    # store available the whole climb, not just at xhigh+.
    enable_at_efforts: tuple[str, ...] = (
        "high", "max",
        "xmedium", "xhigh", "xmax", "xauto",
    )
    # Default search budget when researcher_node calls recall_research.
    # Kept conservative — recall is a "did anyone else find this?"
    # check, not the primary evidence source.
    recall_limit: int = 5
    # Backend-specific endpoints (only the relevant one is consulted).
    pgvector_dsn: Optional[str] = None
    pgvector_table: Optional[str] = None
    sqlite_vec_path: Optional[str] = None
    # M14: per-namespace TTL + distillation-on-expiry. Both default
    # to ``enabled = False`` — the M14 wiring is fully opt-in so
    # existing M8 deployments stay zero-cost.
    ttl: StoreTTLConfig = field(default_factory=StoreTTLConfig)
    distillation: StoreDistillationConfig = field(
        default_factory=StoreDistillationConfig,
    )


@dataclass
class CoderLimitsConfig:
    """M10: per-session sandbox caps for the coder role.

    The coder writes files inside an isolated directory at
    ``<cwd>/.claude-hooks/consultants/<sid>/coder-out/``. These caps
    bound how much a runaway model can write before the guard
    rejects further calls — important because a single LLM that
    misreads the task ("write the whole stdlib") could otherwise
    fill the disk before a human notices.

    Defaults match the plan:
    - 50 KB per individual file
    - 1 MB total bytes per session
    - 16 distinct files per session

    All caps apply per-lane: each coder Send lane keeps its own
    audit buffer, so the 1 MB total is per-coder-call. Cross-lane
    bookkeeping is the synthesizer's job (it reads coder_artifacts
    and can warn the user if a single lane saturated).
    """
    max_file_bytes: int = 50 * 1024
    max_total_bytes: int = 1024 * 1024
    max_files: int = 16


@dataclass
class ConsultantsConfig:
    topology: str = DEFAULT_TOPOLOGY
    effort: str = DEFAULT_EFFORT
    service: ServiceConfig = field(default_factory=ServiceConfig)
    checkpointer: CheckpointerConfig = field(default_factory=CheckpointerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    coder_limits: CoderLimitsConfig = field(
        default_factory=CoderLimitsConfig,
    )
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


def _coerce_route(raw: Any) -> Optional[CoderLanguageRoute]:
    """Parse one ``CoderLanguageRoute`` from a TOML sub-table dict.

    Accepts ``{primary, fallback?}``. Returns ``None`` when the
    primary is missing / empty (a route without a primary is
    meaningless; the caller decides whether to fall through to the
    legacy ``model`` field).
    """
    if not isinstance(raw, dict):
        return None
    primary = raw.get("primary")
    if not isinstance(primary, str) or not primary.strip():
        return None
    fallback = raw.get("fallback") or ""
    if not isinstance(fallback, str):
        fallback = ""
    return CoderLanguageRoute(
        primary=primary.strip(),
        fallback=fallback.strip(),
    )


def _merge_role(base: RoleConfig, override: dict) -> RoleConfig:
    out = RoleConfig(
        enabled=base.enabled,
        model=base.model,
        ctx_max=base.ctx_max,
        ctx_max_explicit=base.ctx_max_explicit,
        think=base.think,
        extra_models=list(base.extra_models),
        routes_by_language=dict(base.routes_by_language),
        default_route=base.default_route,
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
    # Task #111: per-language coder routing. Only the ``coder`` role
    # ever populates these fields, but the merge logic is uniform —
    # other roles simply never have TOML sections for them.
    if "default_route" in override:
        # Empty dict / None / malformed → clear (operator chose
        # "no global default"). Valid sub-table → parse + replace.
        out.default_route = _coerce_route(override["default_route"])
    if "routes" in override and isinstance(override["routes"], dict):
        # Empty dict: explicit clear. Non-empty dict: REPLACE — TOML
        # users who want to *add* one entry while keeping the rest
        # should re-emit all entries (the CLI handles that). The
        # alternative (merge-in) would surprise an operator who set
        # ``routes = {}`` expecting to start fresh.
        new_routes: dict[str, CoderLanguageRoute] = {}
        for lang, route_raw in override["routes"].items():
            if not isinstance(lang, str) or not lang.strip():
                continue
            parsed = _coerce_route(route_raw)
            if parsed is not None:
                new_routes[lang.strip()] = parsed
        out.routes_by_language = new_routes
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

    # store (M8)
    st = raw.get("store") or {}
    if isinstance(st, dict):
        if "enabled" in st:
            base.store.enabled = bool(st["enabled"])
        if "backend" in st and isinstance(st["backend"], str):
            base.store.backend = st["backend"].strip() or base.store.backend
        if "enable_at_efforts" in st and isinstance(
                st["enable_at_efforts"], (list, tuple)):
            base.store.enable_at_efforts = tuple(
                str(x) for x in st["enable_at_efforts"]
            )
        if "recall_limit" in st:
            try:
                base.store.recall_limit = max(1, int(st["recall_limit"]))
            except (TypeError, ValueError):
                pass
        if "pgvector_dsn" in st and isinstance(st["pgvector_dsn"], str):
            base.store.pgvector_dsn = st["pgvector_dsn"].strip() or None
        if "pgvector_table" in st and isinstance(st["pgvector_table"], str):
            base.store.pgvector_table = st["pgvector_table"].strip() or None
        if "sqlite_vec_path" in st and isinstance(st["sqlite_vec_path"], str):
            base.store.sqlite_vec_path = st["sqlite_vec_path"].strip() or None

        # M14: nested [store.ttl] block — opt-in per-namespace TTL.
        ttl_raw = st.get("ttl") or {}
        if isinstance(ttl_raw, dict):
            if "enabled" in ttl_raw:
                base.store.ttl.enabled = bool(ttl_raw["enabled"])
            for fld, key in (
                ("research_days", "research_days"),
                ("tool_results_hours", "tool_results_hours"),
                ("project_days", "project_days"),
                ("user_days", "user_days"),
            ):
                if key in ttl_raw:
                    v = ttl_raw[key]
                    if v is None:
                        setattr(base.store.ttl, fld, None)
                    else:
                        try:
                            num = float(v)
                        except (TypeError, ValueError):
                            continue
                        setattr(
                            base.store.ttl, fld,
                            num if num > 0 else None,
                        )
            if "refresh_on_read" in ttl_raw:
                base.store.ttl.refresh_on_read = bool(
                    ttl_raw["refresh_on_read"]
                )

        # M14: nested [store.distillation] block.
        dist_raw = st.get("distillation") or {}
        if isinstance(dist_raw, dict):
            if "enabled" in dist_raw:
                base.store.distillation.enabled = bool(
                    dist_raw["enabled"]
                )
            if "model" in dist_raw and isinstance(
                    dist_raw["model"], str):
                m = dist_raw["model"].strip()
                if m:
                    base.store.distillation.model = m
            if "fallback_models" in dist_raw and isinstance(
                    dist_raw["fallback_models"], (list, tuple)):
                base.store.distillation.fallback_models = tuple(
                    str(x).strip()
                    for x in dist_raw["fallback_models"]
                    if str(x).strip()
                )
            if "sweep_interval_seconds" in dist_raw:
                try:
                    secs = float(
                        dist_raw["sweep_interval_seconds"]
                    )
                    if secs > 0:
                        base.store.distillation.sweep_interval_seconds = secs
                except (TypeError, ValueError):
                    pass
            if "min_entries_per_distillation" in dist_raw:
                try:
                    n = int(
                        dist_raw["min_entries_per_distillation"]
                    )
                    if n >= 1:
                        base.store.distillation.min_entries_per_distillation = n
                except (TypeError, ValueError):
                    pass
            if "max_session_entries" in dist_raw:
                try:
                    m = int(dist_raw["max_session_entries"])
                    if m >= 1:
                        base.store.distillation.max_session_entries = m
                except (TypeError, ValueError):
                    pass

    # coder_limits (M10)
    cl = raw.get("coder_limits") or {}
    if isinstance(cl, dict):
        for k in ("max_file_bytes", "max_total_bytes", "max_files"):
            v = cl.get(k)
            if isinstance(v, int) and v > 0:
                setattr(base.coder_limits, k, v)

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
    L.append("[store]")
    L.append("# Long-term memory BaseStore for cross-lane / cross-session recall.")
    L.append("# enabled = false  -> no store wired (zero cost, default).")
    L.append("# backend choices: memory | pgvector | sqlite_vec")
    L.append(f"enabled = {'true' if cfg.store.enabled else 'false'}")
    L.append(f"backend = {_toml_str(cfg.store.backend)}")
    L.append(
        "enable_at_efforts = ["
        + ", ".join(_toml_str(t) for t in cfg.store.enable_at_efforts)
        + "]"
    )
    L.append(f"recall_limit = {cfg.store.recall_limit}")
    if cfg.store.pgvector_dsn:
        L.append(f"pgvector_dsn = {_toml_str(cfg.store.pgvector_dsn)}")
    else:
        L.append('# pgvector_dsn = "postgresql://user:pass@host:5432/db"')
    if cfg.store.pgvector_table:
        L.append(f"pgvector_table = {_toml_str(cfg.store.pgvector_table)}")
    if cfg.store.sqlite_vec_path:
        L.append(f"sqlite_vec_path = {_toml_str(cfg.store.sqlite_vec_path)}")
    else:
        L.append('# sqlite_vec_path = "~/.claude/consultants-store.db"')
    L.append("")
    # M14: nested [store.ttl] block — per-namespace TTL.
    L.append("[store.ttl]")
    L.append("# Per-namespace TTL on store entries. enabled = false "
             "(default) keeps the M8 'live forever' behavior; flip on "
             "to age out stale findings before they dilute recall.")
    L.append(f"enabled = {'true' if cfg.store.ttl.enabled else 'false'}")
    if cfg.store.ttl.research_days is None:
        L.append("# research_days: null = never expire")
        L.append("research_days = 0  # treat 0 / negative as 'never'")
    else:
        L.append(f"research_days = {cfg.store.ttl.research_days}")
    if cfg.store.ttl.tool_results_hours is None:
        L.append("tool_results_hours = 0  # treat 0 / negative as 'never'")
    else:
        L.append(f"tool_results_hours = {cfg.store.ttl.tool_results_hours}")
    if cfg.store.ttl.project_days is None:
        L.append('# project_days = 365   # never by default '
                 '(cross-session memory)')
        L.append("project_days = 0  # 0 / negative = never")
    else:
        L.append(f"project_days = {cfg.store.ttl.project_days}")
    if cfg.store.ttl.user_days is None:
        L.append('# user_days = 365      # never by default '
                 '(cross-project memory)')
        L.append("user_days = 0  # 0 / negative = never")
    else:
        L.append(f"user_days = {cfg.store.ttl.user_days}")
    L.append("# refresh_on_read: bump expires_at forward on every "
             "successful recall hit (the 'if it's still useful, "
             "keep it' heuristic).")
    L.append(
        f"refresh_on_read = "
        f"{'true' if cfg.store.ttl.refresh_on_read else 'false'}"
    )
    L.append("")
    # M14: nested [store.distillation] block.
    L.append("[store.distillation]")
    L.append("# Caliber-style summary written into the durable "
             "('project', pid) namespace before expiring research "
             "entries get deleted. Episodic short-term → semantic "
             "long-term.")
    L.append(
        f"enabled = "
        f"{'true' if cfg.store.distillation.enabled else 'false'}"
    )
    L.append(f"model = {_toml_str(cfg.store.distillation.model)}")
    if cfg.store.distillation.fallback_models:
        inner = ", ".join(
            _toml_str(m)
            for m in cfg.store.distillation.fallback_models
        )
        L.append(f"fallback_models = [{inner}]")
    else:
        L.append("fallback_models = []")
    L.append(
        f"sweep_interval_seconds = "
        f"{cfg.store.distillation.sweep_interval_seconds}"
    )
    L.append(
        f"min_entries_per_distillation = "
        f"{cfg.store.distillation.min_entries_per_distillation}"
    )
    L.append(
        f"max_session_entries = "
        f"{cfg.store.distillation.max_session_entries}"
    )
    L.append("")
    L.append("[coder_limits]")
    L.append("# Sandbox caps for the coder role (M10). Only consulted "
             "when [role.coder] enabled = true.")
    L.append(f"max_file_bytes = {cfg.coder_limits.max_file_bytes}")
    L.append(f"max_total_bytes = {cfg.coder_limits.max_total_bytes}")
    L.append(f"max_files = {cfg.coder_limits.max_files}")
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
        # Task #111: emit coder per-language routing as nested
        # sub-tables AFTER the plain role block — TOML grammar
        # requires sub-tables to follow their parent. Other roles
        # leave both fields empty / None so this block is a no-op
        # for them.
        if rc.default_route is not None:
            L.append(f"[role.{role}.default_route]")
            L.append(f"primary = {_toml_str(rc.default_route.primary)}")
            if rc.default_route.fallback:
                L.append(
                    f"fallback = {_toml_str(rc.default_route.fallback)}"
                )
            else:
                L.append('# fallback = ""  # empty / unset: '
                         "no failover on this route")
            L.append("")
        if rc.routes_by_language:
            for lang in sorted(rc.routes_by_language):
                route = rc.routes_by_language[lang]
                L.append(f"[role.{role}.routes.{lang}]")
                L.append(f"primary = {_toml_str(route.primary)}")
                if route.fallback:
                    L.append(f"fallback = {_toml_str(route.fallback)}")
                else:
                    L.append('# fallback = ""  # empty / unset: '
                             "no failover on this route")
                L.append("")
    return "\n".join(L)


# Task #111: single-source resolver for "given a language id, which
# (primary, fallback) pair should the coder lane use?" Re-exported
# for the graph + tests. Pure function — no side effects, no I/O.
def coder_resolve_route(cfg: ConsultantsConfig,
                        language: Optional[str]) -> CoderLanguageRoute:
    """Walk ``cfg.roles['coder']`` to find the route for ``language``.

    Resolution order:
    1. ``language`` ∈ ``routes_by_language`` → that entry.
    2. ``default_route`` set → that entry.
    3. legacy: synthesize a route from ``cfg.roles['coder'].model``
       with no fallback (v1 back-compat for callers that haven't
       touched the new fields).

    Always returns a ``CoderLanguageRoute``; the lane code decides
    whether to fail fast on an empty fallback.
    """
    rc = cfg.roles.get("coder")
    if rc is None:
        return CoderLanguageRoute(primary=DEFAULT_MODEL)
    if language and language in rc.routes_by_language:
        return rc.routes_by_language[language]
    if rc.default_route is not None:
        return rc.default_route
    return CoderLanguageRoute(primary=rc.model or DEFAULT_MODEL)


def coder_unique_models(cfg: ConsultantsConfig) -> list[str]:
    """De-duped list of every model name the coder role might ever
    invoke (primary + fallback across every per-language entry plus
    the global default plus the legacy ``model`` field). Used by the
    runner to materialise one chat client per distinct model up
    front. Order is stable: legacy model first, then default-route
    primary/fallback, then per-language entries sorted by language
    id (primary, fallback). Empty fallbacks are skipped.
    """
    rc = cfg.roles.get("coder")
    seen: set[str] = set()
    out: list[str] = []

    def _add(m: str) -> None:
        s = (m or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    if rc is None:
        _add(DEFAULT_MODEL)
        return out
    _add(rc.model)
    if rc.default_route is not None:
        _add(rc.default_route.primary)
        _add(rc.default_route.fallback)
    for lang in sorted(rc.routes_by_language):
        route = rc.routes_by_language[lang]
        _add(route.primary)
        _add(route.fallback)
    return out


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


# ---------- Task #111: coder per-language route mutators ----------- #

def _validate_lang_id(lang: str) -> str:
    """Normalise + sanity-check a language id.

    The CLI's ``config coder set <lang>`` lets the operator pass any
    lower-case identifier (so future languages don't require a code
    change); we just enforce the id is a non-empty lower-case slug
    matching ``[a-z][a-z0-9_-]*``. Validation against
    ``LANGUAGE_BY_EXTENSION.values()`` happens at the CLI layer with
    an opt-out flag for future-proofing.
    """
    s = (lang or "").strip().lower()
    if not s:
        raise ValueError("language id must be non-empty")
    import re
    if not re.match(r"^[a-z][a-z0-9_+-]*$", s):
        raise ValueError(
            f"language id {lang!r} must be a lower-case slug "
            "([a-z][a-z0-9_+-]*)"
        )
    return s


def set_coder_route(language: str, *, primary: Optional[str] = None,
                    fallback: Optional[str] = None,
                    scope: str = "user",
                    cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Upsert one per-language coder route entry.

    On a NEW entry (``language`` not yet in ``routes_by_language``)
    ``primary`` is required. On an UPDATE either flag alone works
    — the unset side keeps its current value. Pass ``fallback=""``
    explicitly to clear the failover model on an existing entry.

    Persists to user-global by default; ``scope`` + ``cwd`` switch
    to per-project (matches ``set_role``).
    """
    lang = _validate_lang_id(language)
    cfg = load_config(cwd if scope != "user" else None)
    rc = cfg.roles["coder"]
    existing = rc.routes_by_language.get(lang)
    if existing is None:
        if not primary or not primary.strip():
            raise ValueError(
                f"language {lang!r} has no existing route; "
                "--primary is required to create one"
            )
        new_primary = primary.strip()
        new_fallback = (fallback or "").strip()
    else:
        new_primary = (
            primary.strip() if primary and primary.strip()
            else existing.primary
        )
        # fallback semantics: None ⇒ keep current; "" ⇒ explicit clear;
        # non-empty ⇒ replace.
        if fallback is None:
            new_fallback = existing.fallback
        else:
            new_fallback = fallback.strip()
    rc.routes_by_language[lang] = CoderLanguageRoute(
        primary=new_primary, fallback=new_fallback,
    )
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def unset_coder_route(language: str, *, scope: str = "user",
                      cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Remove one per-language coder route entry. Idempotent — a
    missing entry is a silent no-op (no error). After removal the
    language falls through to the global default route at routing
    time.
    """
    lang = _validate_lang_id(language)
    cfg = load_config(cwd if scope != "user" else None)
    rc = cfg.roles["coder"]
    rc.routes_by_language.pop(lang, None)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_coder_default_route(*, primary: Optional[str] = None,
                            fallback: Optional[str] = None,
                            scope: str = "user",
                            cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set or update the global default coder route.

    When no default exists yet, ``primary`` is required. Updates
    follow the same semantics as ``set_coder_route``: ``None`` for
    a field keeps the current value; ``""`` for ``fallback``
    explicitly clears it.
    """
    cfg = load_config(cwd if scope != "user" else None)
    rc = cfg.roles["coder"]
    existing = rc.default_route
    if existing is None:
        if not primary or not primary.strip():
            raise ValueError(
                "no default_route exists; --primary is required to "
                "create one"
            )
        new_primary = primary.strip()
        new_fallback = (fallback or "").strip()
    else:
        new_primary = (
            primary.strip() if primary and primary.strip()
            else existing.primary
        )
        if fallback is None:
            new_fallback = existing.fallback
        else:
            new_fallback = fallback.strip()
    rc.default_route = CoderLanguageRoute(
        primary=new_primary, fallback=new_fallback,
    )
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
