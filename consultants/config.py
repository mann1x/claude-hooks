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

import json
import logging
import re
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

log = logging.getLogger("consultants.config")


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
    "synthesizer", "adversary",
)
# Synthesizer alone is mandatory — every other role is opt-out (or in
# tool_executor's / coder's / adversary's case opt-in via
# cfg.roles.<role>.enabled). The ``adversary`` role (M3) is a
# post-synthesis refuter that runs once after the synthesizer to attack
# unsupported claims; default-OFF, wired non-re-entrantly so x-tier
# fanout upstream is untouched.
MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})

DEFAULT_TOPOLOGY = "council"
VALID_TOPOLOGIES: tuple[str, ...] = ("council",)  # roundtable / freeform later

DEFAULT_EFFORT = "medium"
# The registry of valid effort tiers (its keys drive every --effort
# choice list and the HTTP validation). The VALUES are display-only:
# they surface as ``effort_budget`` in ``config show`` and nothing
# enforces them. The follow-up cap is the flat ``max_followups``
# (v1.12, default 4) and round / reroute caps come from
# ``council.EFFORT_CAPS`` + ``control.EFFORT_ROUND_CAPS``. Comments
# below that call a value a "follow-up budget" describe the pre-v1.12
# design.
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

# Consultancy review-loop knobs (flat, effort-independent). The cap
# is enforced server-side; ``allow_extra`` is the grant size each user
# approval adds when the cap is reached.
DEFAULT_MAX_FOLLOWUPS = 4
DEFAULT_ALLOW_EXTRA = 1

# Adversary + verify-budget knobs (M1). All conservative / OFF by
# default so a plain consult is byte-identical (M12 parity).
#
# verify_budget sizes the Workflow-driver skeptic panel (M6): how many
# of the answer's riskiest claims get an independent Anthropic refuter,
# and how many verification rounds. minimal=2 claims/1 round,
# bounded=3 (default), generous=5/up-to-cap.
VERIFY_BUDGET_TIERS: tuple[str, ...] = ("minimal", "bounded", "generous")
DEFAULT_VERIFY_BUDGET = "bounded"

# adversary_strictness tunes BOTH the dynamic critic dial (M4) and the
# post-synthesis adversary role (M3): how hard they push to refute.
ADVERSARY_STRICTNESS_LEVELS: tuple[str, ...] = ("soft", "normal", "strict")
DEFAULT_ADVERSARY_STRICTNESS = "normal"

# Adversary checkpoint (M2): an engine-initiated pause before synthesis
# that emits an SSE ``awaiting_adversary`` event and waits up to
# ``adversary_checkpoint_timeout_s`` for the assistant to inject a
# bespoke red-team brief, then auto-proceeds (covers lost SSE / missed
# polls). Default OFF; 10-minute timeout.
DEFAULT_ADVERSARY_CHECKPOINT = False
DEFAULT_ADVERSARY_CHECKPOINT_TIMEOUT_S = 600

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
    # Plan B: ``[role.X.budget]`` (only the keys set) and
    # ``[role.X.models."<model>"]`` (per-model settings, keyed by the
    # model string exactly as the role names it).
    budget: dict[str, Any] = field(default_factory=dict)
    sampling: dict[str, Any] = field(default_factory=dict)
    compaction: dict[str, Any] = field(default_factory=dict)
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


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
# - ``coder`` → ``glm-5.2:cloud`` (succession from the
#   2026-05-16 M11b winner glm-5.1; see MODEL_SUCCESSIONS)
#   (suite v1.0 rubric winner: pass=100%, avg_quality=4.88,
#   median_tokens=1841, median_wall=4.9 s). The constant lives in
#   ``consultants/engine/coder_defaults.py`` and is sourced from
#   the baselines ledger; imported at module top.
DEFAULT_MODEL_BY_ROLE: dict[str, str] = {
    "tool_executor": "gemma4:31b-cloud",
    "coder": RECOMMENDED_CODER_MODEL,
}


# History of tool_executor's default-on bit:
#
# - M11c-1 (2026-05-17, scaffold): False.
# - M11c-5 (2026-05-17, flip-on after bench): True. The M11c-2
#   bench cleared its rubric (gemma4:31b-cloud at 87.5% / 5.00)
#   AND task #103 (x-tier proper composition) resolved via the
#   M11c-3 engine refactor.
# - 2026-05-18 (flip-back-off): False. The M14 first-real-ask
#   tool_executor on/off A/B
#   (``benchmarks/consultants/results/2026-05-18/tool-executor-ab/``)
#   showed the role costing +12 minutes wall time and +43% tokens
#   AND identifying FEWER edge cases on a grep-shaped question.
#   M11c-2 still validates the role for the tool-heavy reasoning
#   questions that bench targeted; this flip-back recognizes that
#   the bench's question shape is NOT what most operator questions
#   look like. Default-off is the right baseline; operators who
#   want the role's specialization can opt in.
#
# When tool_executor is net-positive (turn it on):
#   * Heavy cross-file tool work (5+ files, deep call chains).
#   * Questions where each researcher lane would otherwise
#     saturate context just running grep / read_file.
#   * Sub-question shapes that match the M11c-2 corpus profile
#     (see ``benchmarks/consultants/questions/tool_executor/``).
#
# When tool_executor is net-negative (leave it off):
#   * Grep-and-interpret questions like the M14 walk-me-through
#     prompt. Inline researcher tool-loop is faster + sharper
#     because the model that calls grep is the one that
#     interprets it (no fanback aggregation lossiness).
#   * Sessions where wall time is the binding constraint.
#
# Coder remains disabled-by-default — operator must opt into
# sandboxed file writes. Both opt-in/opt-out is one TOML line:
#   [role.coder]          enabled = true
#   [role.tool_executor]  enabled = true     # opt-in to specialist
DEFAULT_ENABLED_BY_ROLE: dict[str, bool] = {
    "tool_executor": False,
    "coder": False,
    "adversary": False,
}


def _default_role_config(role: str) -> "RoleConfig":
    """Return the per-role boot-time defaults.

    Centralizes the special-casing for tool_executor (different
    primary model + disabled-by-default) so the dataclass factory
    on ``ConsultantsConfig.roles`` stays a one-liner and every
    role-iteration site sees consistent defaults.

    Task #111: the ``coder`` role additionally seeds
    ``routes_by_language`` + ``default_route`` from the coder_med
    v1.0 bench winners (2026-06-04). Other roles leave both fields
    empty / None so the runtime stays a pure-``model`` lookup for them.
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
    #: An address the engine also listens on for the read-only council
    #: dashboard (``0.0.0.0`` or a LAN address, like the proxy
    #: dashboard's ``listen_host``). Only the dashboard's routes answer
    #: there; the control API stays loopback-only. "" = loopback only.
    dashboard_host: str = ""


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
    - ``xauto_retry_infra_lanes`` (default ``True``) — plan D9: an xauto
      run reruns, once and before the critic, the first-round researcher
      lanes that failed on infrastructure (``failure_class = infra``), as
      ``retry-lanes`` would after the run. Other tiers never do.
    """
    review_before_synthesis: bool = False
    interrupt_on_low_confidence: bool = False
    xauto_retry_infra_lanes: bool = True


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

    M14 (2026-05-18) flipped ``enabled`` from False to True as
    part of the default-on flip for the consultants store.

    #215 (2026-05-18) adds ``jitter_pct`` to spread aligned
    cohorts at write time — see :func:`expires_at_seconds`. Default
    10% means a 30-day TTL spreads ±3 days; the M14-default-on flip
    no longer dumps every prior session's expiry onto the same
    tick.
    """
    enabled: bool = True
    research_days: Optional[float] = 30.0
    tool_results_hours: Optional[float] = 24.0
    project_days: Optional[float] = None  # never
    user_days: Optional[float] = None  # never
    refresh_on_read: bool = True
    # #215: spread aligned cohorts at write time so the reaper never
    # sees N sessions all expire on the same tick (e.g., M14
    # default-on flip stamps every existing session's content with
    # the same ``expires_at = now + 30d``). Applied multiplicatively
    # to ttl_seconds: ``ttl * (1 + uniform(-jitter_pct, +jitter_pct))``.
    # Set to 0.0 to disable; 0.1 = ±10% (recommended default).
    jitter_pct: float = 0.1

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
    - ``fallback_models = ["glm-5.2:cloud"]`` — caliber-init
      fallback model; ~64k context window comfortable for prompt
      overflow.
    - ``sweep_interval_seconds = 3600`` — hourly. Cheap on a
      24-hour TTL boundary; safe on a 30-day TTL.
    - ``min_entries_per_distillation = 3`` — cost gate.
      Single-finding sessions just get deleted; no LLM call fires.
    - ``max_session_entries = 50`` — truncate before prompt
      assembly. ~600 chars/entry × 50 ≈ 30 k tokens (gemma's
      32 k ctx ceiling); larger groups overflow to the fallback.

    M14 (2026-05-18) flipped ``enabled`` from False to True as
    part of the default-on flip for the consultants store.

    #215 (2026-05-18) adds two pacing knobs to bound per-sweep
    embedder + cloud-LLM load:

    - ``max_groups_per_sweep = 5`` — caps how many session groups
      one sweep tick distills. Remaining groups roll over to the
      next tick. Bounds worst-case sweep wall time (5 × ~60 s LLM
      + 5 × ~5 s embed = ~5 min) so the embedder always has
      headroom for live consults.
    - ``pace_seconds_between_distillations = 5.0`` — sleep
      between consecutive distillations within a single sweep so
      the embedder gets breathing room. Set to 0.0 to disable.
    """
    enabled: bool = True
    model: str = "gemma4:31b-cloud"
    fallback_models: tuple[str, ...] = ("glm-5.2:cloud",)
    sweep_interval_seconds: float = 3600.0
    min_entries_per_distillation: int = 3
    max_session_entries: int = 50
    # #215: per-sweep distillation cap. Hosts with weaker embedders
    # or shared infra should lower this; hosts with headroom can
    # raise it. The reaper picks the first ``max_groups_per_sweep``
    # research groups it finds and rolls the rest over to the next
    # tick. Set to 0 to mean "no cap" (the pre-#215 behavior).
    max_groups_per_sweep: int = 5
    # #215: inter-group sleep. With 5 groups × 5 s pacing = +25 s
    # added wall time per sweep. Set to 0.0 to disable.
    pace_seconds_between_distillations: float = 5.0


@dataclass
class StoreConfig:
    """M8: long-term memory BaseStore settings.

    A LangGraph :class:`BaseStore` lets researcher lanes recall what
    other lanes already discovered (within a session) and lets the
    follow-up runner semantically replay the parent's research
    (across sessions). The store is **effort-gated**:

    - ``enabled = true`` (M14 default) — store is wired into the
      graph at the configured effort tiers.
      ``backend = "sqlite_vec"`` (default) is the lowest-friction
      persistence option: a single file at
      ``~/.claude/consultants-store.db`` with no daemon dependency.
      Cross-session persistence + the M14 TTL / distillation chain
      fires (see ``[store.ttl]`` and ``[store.distillation]``).
    - ``backend = "pgvector"`` — shares the Postgres instance the
      recall hook pipeline already uses. Higher throughput, KG-
      style relations available. Configure on hosts that already
      run claude-hooks against pgvector.
    - ``backend = "memory"`` — LangGraph's bundled InMemoryStore
      (per-process, no durability). The M14 reaper short-circuits
      on this backend because there's nothing to sweep
      cross-session.
    - ``enabled = false`` — explicit opt-out; recall + record
      helpers in :mod:`consultants.engine.store` become no-ops.
      Zero cost.

    The ``enable_at_efforts`` gate keeps the zero-cost path for the
    light tiers and turns the store on for the deep ones, where the
    multi-lane x-tier diversity benefits the most from cross-lane
    recall. Pass ``effort=None`` to the factory to bypass the gate
    entirely (used by the follow-up runner).

    M14 (2026-05-18) flipped ``enabled`` from False to True and
    ``backend`` from "memory" to "sqlite_vec" so the TTL +
    distillation chain is wired by default. Hosts that don't want
    any persistent state set ``enabled = false``.
    """
    enabled: bool = True
    backend: str = "sqlite_vec"  # "memory" | "pgvector" | "sqlite_vec"
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
    # M14 default: a dedicated file under the user's claude config
    # dir so the consultants store stays decoupled from the main
    # recall pipeline's ``claude-hooks-memory.db``. The path is
    # tilde-expanded by ``claude_hooks.utils.expand_user_path``
    # downstream — leaving the literal ``~`` here keeps configs
    # portable across hosts.
    sqlite_vec_path: Optional[str] = "~/.claude/consultants-store.db"
    # M14 follow-up (2026-05-18): the provider needs an embedder to
    # turn ``content`` into vectors at ``store`` / ``recall_hybrid``
    # time. Without explicit config it defaults to ``NullEmbedder``,
    # which always raises — so every store call would fail.
    # ``embedder`` and ``embedder_options`` mirror the shape used by
    # the recall hook pipeline's ``providers.pgvector`` /
    # ``providers.sqlite_vec`` blocks in ``claude-hooks.json``, so
    # operators can copy/paste their existing embedder config
    # straight across into ``[store]`` without re-learning the
    # field names.
    embedder: Optional[str] = None  # "ollama" | "llamafile" | ...
    embedder_options: dict = field(default_factory=dict)
    # M14 default-on (2026-05-18): TTL + distillation default to
    # ``enabled = True`` (in the sub-config dataclasses) so the
    # consultants store self-curates. Hosts that don't want this
    # set ``[store.ttl].enabled = false`` and
    # ``[store.distillation].enabled = false``, or
    # ``[store].enabled = false`` to disable the store outright.
    ttl: StoreTTLConfig = field(default_factory=StoreTTLConfig)
    distillation: StoreDistillationConfig = field(
        default_factory=StoreDistillationConfig,
    )


@dataclass
class ShellConfig:
    """``[tools.shell]``: the sandboxed ``run_command`` (plan E5).

    Off unless asked for (``--shell`` on ask / follow-up) or
    ``default = "always"``. Only ``mode = "read"`` and ``net = "none"``
    exist so far: ``write`` gives each lane a private git worktree (git
    roots only), egress is plan E6.
    """
    #: ``off`` | ``always`` — the user's "enable always" knob.
    default: str = "off"
    mode: str = "read"
    net: str = "none"
    #: ``auto`` | ``bwrap``.
    backend: str = "auto"
    #: Toolchain dirs shown read-only (their ``bin`` goes first on PATH).
    ro_binds: list = field(default_factory=list)
    timeout_s: int = 120
    max_timeout_s: int = 600
    memory_mib: int = 4096
    pids: int = 256
    cpu_pct: int = 400
    lane_disk_gib: float = 2.0
    free_floor_gib: float = 20.0
    free_floor_pct: float = 10.0
    #: Days a held patch (write mode) waits for apply / discard before it
    #: moves to the session's ``expired/`` (kept, never deleted silently).
    held_ttl_days: float = 7.0


def _shell_choice(*choices):
    def check(v):
        if v not in choices:
            raise ValueError(f"must be one of {', '.join(choices)}")
        return v
    return check


def _shell_net(v):
    if v != "none":
        raise ValueError("only 'none' exists so far (egress is plan E6)")
    return v


def _shell_paths(v):
    if not isinstance(v, list) or not all(
            isinstance(x, str) and (x.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", x))
            for x in v):
        raise ValueError("must be a list of absolute paths")
    return list(v)


def _shell_num(lo, hi, kind=int):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        if kind is int and int(v) != v:
            raise ValueError("must be an integer")
        if not lo <= v <= hi:
            raise ValueError(f"must be in [{lo}, {hi}]")
        return kind(v)
    return check


#: ``[tools.shell]`` keys and how each is checked.
SHELL_KEYS = {
    "default": _shell_choice("off", "always"),
    "mode": _shell_choice("read", "write"),
    "net": _shell_net,
    "backend": _shell_choice("auto", "launcher", "bwrap"),
    "ro_binds": _shell_paths,
    "timeout_s": _shell_num(1, 3600),
    "max_timeout_s": _shell_num(1, 3600),
    "memory_mib": _shell_num(64, 1 << 20),
    "pids": _shell_num(8, 1 << 16),
    "cpu_pct": _shell_num(10, 100 * 1024),
    "lane_disk_gib": _shell_num(0.01, 1 << 14, float),
    "free_floor_gib": _shell_num(0, 1 << 14, float),
    "free_floor_pct": _shell_num(0, 99, float),
    "held_ttl_days": _shell_num(0.01, 3650, float),
}


def apply_shell(sc: ShellConfig, values: dict, *, strict: bool) -> None:
    """Set ``[tools.shell]`` keys on ``sc``. ``strict`` raises on a bad
    key or value (the setter); otherwise it is logged and skipped (a
    hand-edited file must not take the council down)."""
    for k, v in values.items():
        try:
            if k not in SHELL_KEYS:
                raise ValueError(f"unknown key (valid: {', '.join(SHELL_KEYS)})")
            setattr(sc, k, SHELL_KEYS[k](v))
        except ValueError as e:
            if strict:
                raise ValueError(f"[tools.shell] {k}: {e}") from None
            log.warning("[tools.shell] %s = %r ignored: %s", k, v, e)
    if sc.timeout_s > sc.max_timeout_s:
        if strict:
            raise ValueError("[tools.shell] timeout_s must be <= max_timeout_s")
        sc.timeout_s = sc.max_timeout_s


@dataclass
class ToolsConfig:
    """M-A: the council's tool surface and its permission ladder.

    See ``docs/PLAN-council-tool-surface.md``. Every tool the council
    can reach is composed here and dispatched through one gate, so a
    provider added later inherits approval and denial without its own
    plumbing.

    ``git`` defaults **off** deliberately, mirroring how ``store``
    landed: scaffold disabled, validated live, flipped on in a later
    change. The tools themselves are read-only and carry no new risk
    surface, but turning them on adds five schemas to every prompt on
    every lane, which is a default-behaviour change and therefore an
    M12 parity concern. One config command flips it.

    ``permissions`` maps a tool name to a rung:
    ``auto`` / ``ask_assistant`` / ``ask_human`` / ``deny``. An invalid
    value is refused at dispatch rather than coerced — a typo must
    never silently produce an ungated tool.
    """

    #: Master switch for the registry. False keeps the pre-M-A path.
    enabled: bool = True
    #: M-C git history provider (git_history / log / blame / diff / show).
    git: bool = False
    #: M-B: give every role the same tool access, not just the
    #: researcher.
    #:
    #: Flip history:
    #: - 2026-08-01, landed False. The cost argument was that a
    #:   single-shot role costs one LLM call and a tooled one costs one
    #:   per iteration, with critic fanning out per lane at the
    #:   x-tiers — multiplier roles x lanes x iterations.
    #: - 2026-08-01, flipped True after both bench tiers.
    #:   Tier 1 (critic driven directly against planted-false research,
    #:   n=72): 100% recall vs 0% untooled, 100% precision, zero silent
    #:   corrections. Tier 2 (full council, n=3 per arm, paired
    #:   concurrent): -30% prompt tokens, -13% completion, ranges
    #:   NON-OVERLAPPING — the off arm's cheapest trial cost more than
    #:   the on arm's most expensive.
    #:
    #: The cost argument was simply wrong, and the reason is worth
    #: keeping: a tooled *planner* grounds its plan in the code, and
    #: the researcher then converges in ~2 fewer iterations. A tool
    #: loop resends its history each iteration, so the iterations
    #: removed are the most expensive ones. The knob pays for itself
    #: through the planner, not the critic.
    #: See benchmarks/consultants/results/2026-08-01/.
    all_roles: bool = True
    #: Fallback rung for a tool no provider or override names.
    default_level: str = "auto"
    #: Per-tool overrides, ``[tools.permissions]``.
    permissions: dict = field(default_factory=dict)
    #: Seconds a parked ``ask_human`` call waits before it is DENIED.
    #: Only ``ask_human`` can stall (``ask_assistant`` auto-approves per
    #: the ladder), so this is the spend-approval deadline. Timeout
    #: denies on purpose: absence of an approver never authorizes spend.
    approval_timeout_s: float = 600.0
    #: ``[tools.shell]`` — the sandboxed shell, off by default (plan E5).
    shell: ShellConfig = field(default_factory=ShellConfig)


#: Floor for :attr:`ToolsConfig.approval_timeout_s`. A value below this
#: is un-answerable in practice rather than strict: the request has to
#: reach the assistant through a poll, be relayed to a person, and get a
#: decision back, and Claude Code's own turn latency eats most of a
#: minute before anyone has read the tool name. A deadline nobody can
#: meet is ``deny`` with extra steps — and worse than ``deny``, because
#: it also spends the wall-clock. Against a council that runs 30–60
#: minutes, three minutes is not a meaningful delay.
MIN_APPROVAL_TIMEOUT_S = 180.0


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


def _budget_int(v: Any, lo: int) -> Any:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ValueError(f"must be an integer >= {lo}")
    return v


def _budget_share(v: Any, lo: float, hi: float, *, lo_open: bool) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("must be a number")
    v = float(v)
    if (v <= lo if lo_open else v < lo) or v > hi:
        raise ValueError(f"must be in {'(' if lo_open else '['}{lo}, {hi}]")
    return v


def _budget_num_ctx(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower() == "model":
        return "model"
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return _budget_int(v, 1)


def _budget_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("on", "true", "yes", "1"):
        return True
    if isinstance(v, str) and v.lower() in ("off", "false", "no", "0"):
        return False
    raise ValueError("must be true or false")


#: Budget knobs (plan B1/B2) and their validators. Defaults live in
#: ``consultants.engine.budget.Knobs``; a config holds only what was set.
BUDGET_KEYS: dict[str, Any] = {
    "output_ceiling": lambda v: _budget_int(v, 1),
    "output_window_share": lambda v: _budget_share(v, 0.0, 1.0, lo_open=True),
    "reserve_min": lambda v: _budget_int(v, 0),
    "reserve_share": lambda v: _budget_share(v, 0.0, 0.9, lo_open=False),
    "num_ctx": _budget_num_ctx,
    "announce": _budget_bool,
    "trigger_ratio": lambda v: _budget_share(v, 0.3, 0.98, lo_open=False),
    "output_room_min": lambda v: _budget_int(v, 0),
}

#: ``think`` values (plan B3): off, on (the model's own budget), a level,
#: or a token count. ``auto`` / ``on`` mean on; ``xhigh`` means ``max``.
THINK_LEVELS = ("minimal", "low", "medium", "high", "max")


def check_think(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        if v <= 0:
            raise ValueError("a token count must be > 0 (use false for off)")
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("auto", "on", "true"):
            return True
        if t in ("off", "false", "none"):
            return False
        if t == "xhigh":
            return "max"
        if t in THINK_LEVELS:
            return t
        if t.isdigit():
            return check_think(int(t))
    raise ValueError("think must be true/false/auto, a level "
                     f"({'/'.join(THINK_LEVELS)}, xhigh) or a token count")


def _check_seed(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower() in ("random", "off"):
        return v.strip().lower()
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    raise ValueError("seed must be 'random', 'off' or an integer >= 0")


def _check_temperature(v: Any) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError("temperature must be a number >= 0")
    return float(v)


def _check_range_pct(v: Any) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError("temperature_range_pct must be a number >= 0")
    if v > 10:
        log.warning("consultants config: temperature_range_pct %s clamped "
                    "to 10 (the fork's margin)", v)
    return min(10.0, float(v))


#: Sampling diversity (plan B4). Defaults: seed "random",
#: temperature_range_pct 2 (consultants.engine.sampling_diversity).
SAMPLING_KEYS: dict[str, Any] = {
    "seed": _check_seed,
    "temperature": _check_temperature,
    "temperature_range_pct": _check_range_pct,
}

def _choice(*allowed: str):
    def check(v: Any) -> str:
        if isinstance(v, str) and v.strip().lower() in allowed:
            return v.strip().lower()
        raise ValueError(f"must be one of {', '.join(allowed)}")
    return check


#: Compaction (plan C). Defaults live in
#: ``consultants.engine.compaction.Settings``: flow "reduced" (replay
#: writer → retrospective → reviewer), placement "system", review true,
#: target_share 0.25, writer_attempts 3, ledger true, tool results
#: middle-truncated past 32000 chars (file contents 50000), stale reads
#: rewritten at 0.4 of the window, reasoning_replay "none".
COMPACTION_KEYS: dict[str, Any] = {
    "flow": _choice("reduced", "full", "basic"),
    "placement": _choice("system", "user"),
    "review": _budget_bool,
    "target_share": lambda v: _budget_share(v, 0.05, 0.6, lo_open=False),
    "writer_attempts": lambda v: min(5, _budget_int(v, 1)),
    "ledger": _budget_bool,
    "tool_result_max_chars": lambda v: _budget_int(v, 1_000),
    "file_result_max_chars": lambda v: _budget_int(v, 1_000),
    "stale_read_share": lambda v: _budget_share(v, 0.0, 1.0, lo_open=False),
    "reasoning_replay": _choice("none", "all", "inline"),
}

#: Keys a ``[role.X.models."<model>"]`` table takes.
MODEL_OVERRIDE_KEYS: dict[str, Any] = {**BUDGET_KEYS, **SAMPLING_KEYS,
                                       **COMPACTION_KEYS,
                                       "think": check_think}


def validate_settings(raw: Any, keys: dict[str, Any], *,
                      where: str) -> dict[str, Any]:
    """The valid ``key = value`` pairs of a settings table. An unknown
    key or a bad value is logged and dropped, never fatal at load."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        check = keys.get(k)
        if check is None:
            log.warning("consultants config: %s: unknown key %r ignored",
                        where, k)
            continue
        try:
            out[k] = check(v)
        except ValueError as e:
            log.warning("consultants config: %s.%s = %r ignored: %s",
                        where, k, v, e)
    return out


@dataclass
class ProtocolConfig:
    """How roles hand results to each other (plan Milestone G).

    ``typed``: planner, researcher, critic, meta-critic and adversary
    hand in a typed record through a result tool, rendered back to the
    prose each role writes today; the synthesizer stays prose. Off by
    default until the paired prose-vs-typed bench (G6) decides."""
    typed: bool = False


GUARD_PROFILES = ("standard", "strict")
#: Roles the strict profile changes (plan §0.7). The critic, meta-critic,
#: adversary, refuter and synthesizer are never nudged under either.
GUARD_ROLES = ("planner", "researcher", "tool_executor", "coder")


@dataclass
class GuardsConfig:
    """The nudge profile (plan §0.7, D3). ``strict`` is for councils on
    small models: the lanes (researcher, tool_executor, coder) must open
    with a tool call and a report after none is re-asked once; the
    planner is re-asked once when its plan does not parse; thinking-only
    re-asks go from 2 to 3; the wrap-up warning comes at least 2 turns
    before the cap. ``roles`` overrides the profile per role. The loop
    guards are in no profile: always on."""
    profile: str = "standard"
    roles: dict = field(default_factory=dict)

    def for_role(self, role: str) -> str:
        return self.roles.get(role) or self.profile

    def is_default(self) -> bool:
        return self.profile == "standard" and all(
            v == "standard" for v in self.roles.values())


@dataclass
class EscalationConfig:
    """Per-lane model escalation (plan D, after D1/D2): a first-round
    researcher lane that a guard ended (struggle, loop guard, mistake
    limit, reasoning loop) or that failed on its task reruns its plan item
    once on ``expert_model``, given a brief of the failed attempt instead
    of its transcript; at most ``max_per_council`` lanes. The harness
    decides. ``expert_model`` empty (the default): off, and the graph is
    unchanged. A tag on the engine's server, or ``@endpoint/model``."""
    expert_model: str = ""
    max_per_council: int = 2


ESCALATION_MAX = 10


def check_guard_profile(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v not in GUARD_PROFILES:
        raise ValueError(f"profile must be one of {', '.join(GUARD_PROFILES)}")
    return v


#: Where a decision model is asked (plan H). ``review``: an accept /
#: followup / research recommendation on the council's answer;
#: ``confidence``: a scored confidence beside the critic's and
#: synthesizer's self-rating; ``struggle``: a stuck-vs-progressing reading
#: of a lane's recent calls; ``citations``: whether quoted code supports
#: the claim citing it. A seam in ``shadow`` is asked and recorded (a
#: ``decision`` event) but never acted on — for collecting live scores
#: before trusting them (plan H6).
DECISION_SEAMS = ("review", "confidence", "struggle", "citations")


@dataclass
class DecisionConfig:
    """A decision model (TypeSafe's Jev, or a ``/v1/systemone`` server
    such as xOllama with tev1 / nimble). Off by default; no host defaults.

    ``url`` empty means TypeSafe (``https://api.typesafe.ai/v1``), which
    needs a key: ``key_file`` names a file holding it (the secret itself
    never enters this config or the run's checkpoint), else the
    ``TYPESAFE_API_KEY`` environment variable."""
    enabled: bool = False
    url: Optional[str] = None
    model: str = "jev-latest"
    key_file: Optional[str] = None
    floor: float = 0.6
    high_stakes_floor: float = 0.85
    timeout_s: float = 15.0
    seams: list = field(default_factory=lambda: list(DECISION_SEAMS))
    shadow: list = field(default_factory=list)


def check_decision_seams(value: Any) -> list:
    items = value if isinstance(value, (list, tuple)) else [
        v.strip() for v in str(value).split(",") if v.strip()]
    bad = [v for v in items if v not in DECISION_SEAMS]
    if bad:
        raise ValueError(f"unknown seam(s) {bad}; seams: "
                         f"{', '.join(DECISION_SEAMS)}")
    return list(dict.fromkeys(items))


#: Backends an ``[endpoints.<name>]`` entry may name. ``opencoti`` and
#: ``llamacpp`` speak OpenAI ``/v1/chat/completions``; ``xollama`` and
#: ``ollama`` speak Ollama's native ``/api/chat``.
ENDPOINT_BACKENDS = ("opencoti", "llamacpp", "xollama", "ollama")
_ENDPOINT_NAME_RX = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class EndpointConfig:
    """A named chat server a role can use as ``@name`` or
    ``@name/<model>``. Nothing here is a host default: endpoints exist
    only where a user configured them."""
    url: str
    backend: str = "opencoti"
    #: The served model when the reference names none; else the first
    #: id the server lists (OpenAI-compatible backends).
    model: Optional[str] = None
    api_key: Optional[str] = None
    #: PolyKV window mode (plan B2). ``dynamic``: no fixed context; each
    #: agent's request is its own allocation, up to the engine's
    #: ``session_ctx_max`` (the model's window, more with DCA), and the
    #: council budgets against that. ``assured`` (a booked window per
    #: agent session) needs Milestone F's session manager.
    window: str = "dynamic"


#: Endpoint window modes accepted today (plan B2 / F).
ENDPOINT_WINDOW_MODES = ("dynamic",)
ENDPOINT_WINDOW_MODES_LATER = ("assured",)


def check_endpoint_window(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m in ENDPOINT_WINDOW_MODES:
        return m
    if m in ENDPOINT_WINDOW_MODES_LATER:
        raise ValueError(
            "window = 'assured' books a window per agent session, which "
            "needs the PolyKV session manager (plan Milestone F); until it "
            "lands, use 'dynamic' (no fixed context, budgeted against the "
            "engine's session_ctx_max)")
    raise ValueError(f"window must be one of {ENDPOINT_WINDOW_MODES}")


def valid_endpoint_name(name: str) -> bool:
    return bool(_ENDPOINT_NAME_RX.match(name or ""))


@dataclass
class ConsultantsConfig:
    topology: str = DEFAULT_TOPOLOGY
    effort: str = DEFAULT_EFFORT
    # Consultancy review loop (mirrors /get-advice's discuss-until-
    # satisfied flow). ``max_followups`` caps how many followups Claude
    # may auto-issue to the council within one consultancy before it
    # must stop and ask the user; ``allow_extra`` is how many additional
    # followups each user approval grants (the per-call ``--allow-extra``
    # flag overrides this). Both are flat (effort-independent) and the
    # cap is enforced server-side.
    max_followups: int = DEFAULT_MAX_FOLLOWUPS
    allow_extra: int = DEFAULT_ALLOW_EXTRA
    # Adversary / verify-budget (M1). All conservative so a plain
    # consult is unchanged (M12 parity). ``adversary_checkpoint`` gates
    # the engine pause (M2); the ``adversary`` ROLE is gated separately
    # by ``roles["adversary"].enabled`` (M3).
    verify_budget: str = DEFAULT_VERIFY_BUDGET
    adversary_strictness: str = DEFAULT_ADVERSARY_STRICTNESS
    adversary_checkpoint: bool = DEFAULT_ADVERSARY_CHECKPOINT
    adversary_checkpoint_timeout_s: int = DEFAULT_ADVERSARY_CHECKPOINT_TIMEOUT_S
    service: ServiceConfig = field(default_factory=ServiceConfig)
    checkpointer: CheckpointerConfig = field(default_factory=CheckpointerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    coder_limits: CoderLimitsConfig = field(
        default_factory=CoderLimitsConfig,
    )
    roles: dict[str, RoleConfig] = field(default_factory=lambda: {
        r: _default_role_config(r) for r in ROLES
    })
    endpoints: dict[str, EndpointConfig] = field(default_factory=dict)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    guards: GuardsConfig = field(default_factory=GuardsConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    # Plan B: ``[budget]`` / ``[sampling]`` — only the keys a layer set.
    budget: dict[str, Any] = field(default_factory=dict)
    sampling: dict[str, Any] = field(default_factory=dict)
    compaction: dict[str, Any] = field(default_factory=dict)

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
        budget=dict(base.budget),
        sampling=dict(base.sampling),
        compaction=dict(base.compaction),
        model_overrides={m: dict(v) for m, v in base.model_overrides.items()},
    )
    if "budget" in override:
        out.budget.update(validate_settings(
            override["budget"], BUDGET_KEYS, where="role budget"))
    if "sampling" in override:
        out.sampling.update(validate_settings(
            override["sampling"], SAMPLING_KEYS, where="role sampling"))
    if "compaction" in override:
        out.compaction.update(validate_settings(
            override["compaction"], COMPACTION_KEYS, where="role compaction"))
    if isinstance(override.get("models"), dict):
        for m, table in override["models"].items():
            if not isinstance(m, str) or not m.strip():
                continue
            vals = validate_settings(table, MODEL_OVERRIDE_KEYS,
                                     where=f"role models.{m}")
            if vals:
                out.model_overrides.setdefault(m.strip(), {}).update(vals)
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
        if v is None:
            out.think = None
        else:
            try:
                out.think = check_think(v)
            except ValueError as e:
                log.warning("consultants config: role think = %r ignored: %s",
                            v, e)
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
    # Consultancy review-loop knobs. ``max_followups`` accepts >= 0
    # (0 = the very first followup needs approval); ``allow_extra``
    # accepts >= 1 (granting 0 extra rounds would make approval inert).
    if "max_followups" in raw:
        v = raw["max_followups"]
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            base.max_followups = v
    if "allow_extra" in raw:
        v = raw["allow_extra"]
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            base.allow_extra = v

    # Adversary / verify-budget knobs (M1). Unknown values are ignored
    # so a typo never silently flips behavior to an invalid state.
    if isinstance(raw.get("verify_budget"), str) and \
            raw["verify_budget"] in VERIFY_BUDGET_TIERS:
        base.verify_budget = raw["verify_budget"]
    if isinstance(raw.get("adversary_strictness"), str) and \
            raw["adversary_strictness"] in ADVERSARY_STRICTNESS_LEVELS:
        base.adversary_strictness = raw["adversary_strictness"]
    if isinstance(raw.get("adversary_checkpoint"), bool):
        base.adversary_checkpoint = raw["adversary_checkpoint"]
    if "adversary_checkpoint_timeout_s" in raw:
        v = raw["adversary_checkpoint_timeout_s"]
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            base.adversary_checkpoint_timeout_s = v

    # service
    svc = raw.get("service") or {}
    if isinstance(svc, dict):
        if isinstance(svc.get("mode"), str) and svc["mode"] in VALID_SERVICE_MODES:
            base.service.mode = svc["mode"]
        if isinstance(svc.get("http_port"), int) and 1 <= svc["http_port"] <= 65535:
            base.service.http_port = svc["http_port"]
        if isinstance(svc.get("dashboard_host"), str):
            base.service.dashboard_host = svc["dashboard_host"].strip()

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
        if "xauto_retry_infra_lanes" in rt:
            base.runtime.xauto_retry_infra_lanes = \
                bool(rt["xauto_retry_infra_lanes"])

    # store (M8)
    tl = raw.get("tools") or {}
    if isinstance(tl, dict):
        if "enabled" in tl:
            base.tools.enabled = bool(tl["enabled"])
        if "git" in tl:
            base.tools.git = bool(tl["git"])
        if "all_roles" in tl:
            base.tools.all_roles = bool(tl["all_roles"])
        if "default_level" in tl and isinstance(tl["default_level"], str):
            base.tools.default_level = (
                tl["default_level"].strip() or base.tools.default_level)
        if "approval_timeout_s" in tl:
            raw_to = tl["approval_timeout_s"]
            if (isinstance(raw_to, (int, float))
                    and not isinstance(raw_to, bool) and raw_to > 0):
                # A hand-edited TOML below the floor is raised to it and
                # logged rather than rejected — refusing the whole config
                # over one short deadline would take the council down for
                # a value we can safely correct. The setter still errors,
                # because there the operator is right there to be told.
                if float(raw_to) < MIN_APPROVAL_TIMEOUT_S:
                    log.warning(
                        "[tools] approval_timeout_s=%gs is below the %gs "
                        "floor and is un-answerable in practice; using "
                        "%gs. Use permission level 'deny' if you mean "
                        "deny.",
                        float(raw_to), MIN_APPROVAL_TIMEOUT_S,
                        MIN_APPROVAL_TIMEOUT_S,
                    )
                    raw_to = MIN_APPROVAL_TIMEOUT_S
                base.tools.approval_timeout_s = float(raw_to)
        perms = tl.get("permissions")
        if isinstance(perms, dict):
            # Values are NOT validated here on purpose. The gate refuses
            # an unknown rung at dispatch with a message naming the tool
            # and the source; silently dropping a bad value at load time
            # would leave the operator believing a restriction is in
            # force when it is not.
            base.tools.permissions = {
                str(k): v for k, v in perms.items()
            }
        sh = tl.get("shell")
        if isinstance(sh, dict):
            apply_shell(base.tools.shell, sh, strict=False)

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
        # M14 follow-up: embedder + embedder_options. Required when
        # backend = "pgvector" or "sqlite_vec" — without these the
        # provider falls back to NullEmbedder and store calls fail.
        if "embedder" in st and isinstance(st["embedder"], str):
            base.store.embedder = st["embedder"].strip() or None
        if "embedder_options" in st and isinstance(
                st["embedder_options"], dict):
            # Shallow copy + str-key normalisation; TOML keys are
            # already strings so this is a defensive no-op.
            base.store.embedder_options = dict(st["embedder_options"])

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
            # #215: TTL jitter at write time.
            if "jitter_pct" in ttl_raw:
                try:
                    j = float(ttl_raw["jitter_pct"])
                    # Clamp to [0.0, 0.5] — 50% jitter is the max
                    # sensible value; anything above that risks
                    # rows expiring far earlier/later than the
                    # nominal TTL.
                    if 0.0 <= j <= 0.5:
                        base.store.ttl.jitter_pct = j
                except (TypeError, ValueError):
                    pass

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
            # #215: per-sweep distillation cap.
            if "max_groups_per_sweep" in dist_raw:
                try:
                    n = int(dist_raw["max_groups_per_sweep"])
                    if n >= 0:
                        base.store.distillation.max_groups_per_sweep = n
                except (TypeError, ValueError):
                    pass
            # #215: inter-group pacing sleep.
            if "pace_seconds_between_distillations" in dist_raw:
                try:
                    p = float(
                        dist_raw["pace_seconds_between_distillations"]
                    )
                    if p >= 0.0:
                        base.store.distillation \
                            .pace_seconds_between_distillations = p
                except (TypeError, ValueError):
                    pass

    # coder_limits (M10)
    cl = raw.get("coder_limits") or {}
    if isinstance(cl, dict):
        for k in ("max_file_bytes", "max_total_bytes", "max_files"):
            v = cl.get(k)
            if isinstance(v, int) and v > 0:
                setattr(base.coder_limits, k, v)

    # budget (plan B1/B2)
    if "budget" in raw:
        base.budget.update(validate_settings(raw["budget"], BUDGET_KEYS,
                                             where="budget"))
    if "sampling" in raw:
        base.sampling.update(validate_settings(raw["sampling"], SAMPLING_KEYS,
                                               where="sampling"))
    if "compaction" in raw:
        base.compaction.update(validate_settings(
            raw["compaction"], COMPACTION_KEYS, where="compaction"))

    # protocol (plan G)
    proto = raw.get("protocol") or {}
    if isinstance(proto, dict) and isinstance(proto.get("typed"), bool):
        base.protocol.typed = proto["typed"]

    # nudge profile (plan §0.7)
    gd = raw.get("guards") or {}
    if isinstance(gd, dict) and gd:
        try:
            if "profile" in gd:
                base.guards.profile = check_guard_profile(gd["profile"])
        except ValueError as e:
            log.warning("consultants config: guards.profile ignored: %s", e)
        for role, v in (gd.get("roles") or {}).items():
            try:
                if role not in GUARD_ROLES:
                    raise ValueError(f"role must be one of {', '.join(GUARD_ROLES)}")
                base.guards.roles[role] = check_guard_profile(v)
            except ValueError as e:
                log.warning("consultants config: guards.roles.%s ignored: %s",
                            role, e)

    # per-lane model escalation (plan D)
    es = raw.get("escalation") or {}
    if isinstance(es, dict) and es:
        if isinstance(es.get("expert_model"), str):
            base.escalation.expert_model = es["expert_model"].strip()
        mx = es.get("max_per_council")
        if isinstance(mx, int) and not isinstance(mx, bool) \
                and 0 <= mx <= ESCALATION_MAX:
            base.escalation.max_per_council = mx
        elif mx is not None:
            log.warning("consultants config: escalation.max_per_council "
                        "ignored: must be an integer in [0, %d]",
                        ESCALATION_MAX)

    # decision model (plan H)
    dec = raw.get("decision") or {}
    if isinstance(dec, dict) and dec:
        d = base.decision
        if isinstance(dec.get("enabled"), bool):
            d.enabled = dec["enabled"]
        for k in ("url", "key_file"):
            if k in dec:
                v = dec[k]
                setattr(d, k, (str(v).strip().rstrip("/") or None)
                        if v is not None else None)
        if isinstance(dec.get("model"), str) and dec["model"].strip():
            d.model = dec["model"].strip()
        for k, lo, hi in (("floor", 0.0, 1.0), ("high_stakes_floor", 0.0, 1.0),
                          ("timeout_s", 0.5, 300.0)):
            if k in dec:
                try:
                    v = float(dec[k])
                    if not lo <= v <= hi:
                        raise ValueError(f"must be in [{lo}, {hi}]")
                    setattr(d, k, v)
                except (TypeError, ValueError) as e:
                    log.warning("consultants config: decision.%s = %r "
                                "ignored: %s", k, dec[k], e)
        if "seams" in dec:
            try:
                d.seams = check_decision_seams(dec["seams"])
            except ValueError as e:
                log.warning("consultants config: decision.seams: %s", e)
        if "shadow" in dec:
            try:
                d.shadow = check_decision_seams(dec["shadow"])
            except ValueError as e:
                log.warning("consultants config: decision.shadow: %s", e)
        if "api_key" in dec:
            log.warning("consultants config: decision.api_key is not read; "
                        "put the key in a file and set decision.key_file")

    # endpoints (plan A1): ``[endpoints.<name>] url, backend, model, api_key``
    eps = raw.get("endpoints") or {}
    if isinstance(eps, dict):
        for name, ep in eps.items():
            if not isinstance(ep, dict) or not valid_endpoint_name(name):
                continue
            url = ep.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            backend = str(ep.get("backend") or "opencoti").lower()
            if backend not in ENDPOINT_BACKENDS:
                log.warning("consultants config: endpoint %r has unknown "
                            "backend %r; skipped", name, backend)
                continue
            window = "dynamic"
            if "window" in ep:
                try:
                    window = check_endpoint_window(str(ep["window"]))
                except ValueError as e:
                    log.warning("consultants config: endpoint %r: %s; "
                                "using 'dynamic'", name, e)
            base.endpoints[name] = EndpointConfig(
                url=url.strip().rstrip("/"), backend=backend,
                model=(ep.get("model") or None),
                api_key=(ep.get("api_key") or None), window=window)

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


def _override_flag(raw: dict) -> bool:
    """Per-project ``override_user_global`` directive: when a per-project
    file sets it false, the file is ignored (engine + every ``config``
    command fall back to user-global). Absent / non-bool → True, so a
    legacy per-project file (written before this directive existed) keeps
    today's "per-project is merged over user-global" behavior, and a
    brand-new per-project file is active by default.

    This is a per-project-FILE directive, NOT a ``ConsultantsConfig``
    field: it must be read from the raw TOML *before* the merge decision,
    and it never appears in the user-global file."""
    flag = raw.get("override_user_global")
    return flag if isinstance(flag, bool) else True


def project_override_active(cwd: Path) -> bool:
    """True iff a per-project config file exists at ``cwd`` AND its
    ``override_user_global`` directive is on (the default). False when no
    per-project file exists. Single source of truth shared by
    ``load_config``'s merge gate and the CLI's active-scope resolver."""
    raw = _read_toml(project_config_path(cwd))
    return bool(raw) and _override_flag(raw)


def load_config(cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Load merged config: defaults < user-global < per-project.
    ``cwd=None`` skips the project layer (useful for daemon contexts
    that don't have a project root). The per-project layer is merged
    only when the project file's ``override_user_global`` directive is
    on (absent → on); when off, the project file is ignored entirely and
    the result is pure user-global."""
    cfg = ConsultantsConfig()
    cfg = _merge_layer(cfg, _read_toml(user_config_path()))
    if cwd is not None:
        proj_raw = _read_toml(project_config_path(cwd))
        if proj_raw and _override_flag(proj_raw):
            cfg = _merge_layer(cfg, proj_raw)
    return cfg


def _load_for_edit(scope: str, cwd: Optional[Path]) -> ConsultantsConfig:
    """Base config for an in-place mutation, selected by *write* scope.

    For ``scope == "project"`` the per-project layer is merged
    **unconditionally** — bypassing ``load_config``'s
    ``override_user_global`` gate — so editing a *dormant* (flag-off)
    project file preserves the file's own content instead of
    re-snapshotting pure user-global over it. Without this, a
    ``set-* --project`` against a flag-off file would silently revert
    every other project override to the user-global default, breaking the
    "flipping OFF preserves content so flipping back ON restores it"
    invariant. For any other scope the project layer is skipped
    (user-global only). Mirrors :func:`set_override_user_global`'s
    unconditional merge."""
    cfg = ConsultantsConfig()
    cfg = _merge_layer(cfg, _read_toml(user_config_path()))
    if scope == "project" and cwd is not None:
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


def _toml_value(value: Any) -> str:
    """A scalar settings value (bool / int / float / str) as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _toml_str(str(value))


def _render(cfg: ConsultantsConfig, *,
            override_flag: Optional[bool] = None) -> str:
    L: list[str] = []
    L.append("# claude-hooks /consultants engine config.")
    L.append("# This file is managed by `claude-consultants config set-*`")
    L.append("# but is also safe to hand-edit.")
    L.append("")
    L.append(f"topology = {_toml_str(cfg.topology)}")
    L.append(f"effort = {_toml_str(cfg.effort)}")
    L.append("# Consultancy review loop: max_followups caps auto-issued "
             "followups before Claude must ask the user; allow_extra is "
             "the grant size per approval.")
    L.append(f"max_followups = {cfg.max_followups}")
    L.append(f"allow_extra = {cfg.allow_extra}")
    L.append("")
    L.append("# Adversary / verify budget (M1+). verify_budget sizes the "
             "Workflow skeptic panel (minimal|bounded|generous); "
             "adversary_strictness tunes the critic dial + adversary role "
             "(soft|normal|strict); adversary_checkpoint enables the "
             "engine pause-for-red-team before synthesis.")
    L.append(f"verify_budget = {_toml_str(cfg.verify_budget)}")
    L.append(f"adversary_strictness = {_toml_str(cfg.adversary_strictness)}")
    L.append(f"adversary_checkpoint = {str(cfg.adversary_checkpoint).lower()}")
    L.append(
        f"adversary_checkpoint_timeout_s = {cfg.adversary_checkpoint_timeout_s}"
    )
    # Per-project-file directive (emitted only for project-scope writes;
    # ``override_flag is None`` for user-global → byte-identical to pre-fix).
    if override_flag is not None:
        L.append("# override_user_global (per-project files only): true => "
                 "this file is the active config — merged over user-global "
                 "and read+written by every `config` command; false => the "
                 "file is ignored everywhere (fall back to user-global).")
        L.append(f"override_user_global = {str(override_flag).lower()}")
    L.append("")
    L.append("[service]")
    L.append(f"mode = {_toml_str(cfg.service.mode)}")
    L.append(f"http_port = {cfg.service.http_port}")
    if cfg.service.dashboard_host:
        L.append(f"dashboard_host = {_toml_str(cfg.service.dashboard_host)}")
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
    L.append("# xauto_retry_infra_lanes: xauto reruns lanes that failed on "
             "infrastructure, once, before the critic")
    L.append("xauto_retry_infra_lanes = "
             f"{'true' if cfg.runtime.xauto_retry_infra_lanes else 'false'}")
    L.append("")
    L.append("[tools]")
    L.append("# The council's tool surface (M-A). Every tool is dispatched")
    L.append("# through one permission gate; see docs/PLAN-council-tool-surface.md")
    L.append("# enabled = false restores the pre-M-A fixed builtin surface.")
    L.append(f"enabled = {'true' if cfg.tools.enabled else 'false'}")
    L.append("# git: read-only history tools — git_history (\"when did this")
    L.append("#   regress?\" via git log -L), git_log / blame / diff / show.")
    L.append(f"git = {'true' if cfg.tools.git else 'false'}")
    L.append("# all_roles: give planner / critic / meta_critic /")
    L.append("#   synthesizer / adversary the same tools the researcher")
    L.append("#   has. On by default since 2026-08-01: measured -30%")
    L.append("#   prompt / -13% completion tokens at effort=high, and")
    L.append("#   100% vs 0% detection of false research claims.")
    L.append(f"all_roles = {'true' if cfg.tools.all_roles else 'false'}")
    L.append("# default_level: auto | ask_assistant | ask_human | deny")
    L.append(f"default_level = {_toml_str(cfg.tools.default_level)}")
    L.append("# approval_timeout_s: how long a parked ask_human tool")
    L.append("#   call waits before it is DENIED. ask_assistant never")
    L.append("#   parks (it auto-approves), so this is the spend gate.")
    L.append(f"#   Minimum {MIN_APPROVAL_TIMEOUT_S:g}s — a shorter deadline is")
    L.append("#   un-answerable once poll latency and a human decision")
    L.append("#   are in the loop. Use level 'deny' if you mean deny.")
    L.append(f"approval_timeout_s = {cfg.tools.approval_timeout_s:g}")
    if cfg.tools.permissions:
        L.append("")
        L.append("[tools.permissions]")
        for k in sorted(cfg.tools.permissions):
            L.append(f"{_toml_str(k)} = {_toml_str(str(cfg.tools.permissions[k]))}")
    else:
        L.append("")
        L.append("# [tools.permissions]")
        L.append('# "git_diff" = "auto"')
        L.append('# "some_tool" = "ask_assistant"')
    L.extend(_render_shell(cfg.tools.shell))
    L.append("")
    L.append("[store]")
    L.append("# Long-term memory BaseStore for cross-lane / cross-session recall.")
    L.append("# M14 default: enabled = true, backend = sqlite_vec ->")
    L.append("#   ~/.claude/consultants-store.db with TTL + distillation on.")
    L.append("# Set enabled = false for zero-cost (no store wired).")
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
    # M14 follow-up: embedder config. Without these the pgvector /
    # sqlite_vec backend falls back to NullEmbedder and every store
    # call raises. ``install.py`` borrows the values from the main
    # recall pipeline's providers block; operators can hand-edit
    # here to override.
    if cfg.store.embedder:
        L.append(f"embedder = {_toml_str(cfg.store.embedder)}")
    else:
        L.append('# embedder = "llamafile"   # or "ollama" — borrow '
                 'from providers.<name>.embedder')
    if cfg.store.embedder_options:
        L.append("[store.embedder_options]")
        for k in sorted(cfg.store.embedder_options.keys()):
            v = cfg.store.embedder_options[k]
            if isinstance(v, bool):
                L.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                L.append(f"{k} = {v}")
            elif isinstance(v, str):
                L.append(f"{k} = {_toml_str(v)}")
            else:
                # Lists/dicts: emit as JSON-ish literal and trust
                # the TOML parser to accept it. Rare in practice.
                L.append(f"{k} = {json.dumps(v)}")
        L.append("")
    else:
        L.append("# [store.embedder_options]")
        L.append('# url = "http://127.0.0.1:38092/embedding"')
        L.append('# model = "qwen3-embedding:0.6b"')
        L.append("# timeout = 30.0")
        L.append("# num_ctx = 16384")
        L.append("# daemon_ensure = true")
    L.append("")
    # M14: nested [store.ttl] block — per-namespace TTL.
    L.append("[store.ttl]")
    L.append("# Per-namespace TTL on store entries. M14 default = true "
             "so research findings age out at research_days (30d) and "
             "tool_results at tool_results_hours (24h); project / user "
             "namespaces stay forever (null = never).")
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
    L.append("# jitter_pct (#215): spread aligned cohorts at write "
             "time so the reaper doesn't see N sessions all expire "
             "on the same tick. ttl * (1 + uniform(-jitter, +jitter)). "
             "0.0 = disabled; 0.1 = ±10% (recommended default).")
    L.append(f"jitter_pct = {cfg.store.ttl.jitter_pct}")
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
    L.append("# max_groups_per_sweep (#215): cap distillations per "
             "sweep tick. Bounds per-hour embedder + cloud LLM load. "
             "5 means ≤5 × (~60 s LLM + ~5 s embed) ≈ 5 min wall per "
             "tick worst case. 0 = uncapped (pre-#215 behavior).")
    L.append(
        f"max_groups_per_sweep = "
        f"{cfg.store.distillation.max_groups_per_sweep}"
    )
    L.append("# pace_seconds_between_distillations (#215): sleep "
             "between consecutive distillations within a single "
             "sweep. Gives the embedder breathing room. 0.0 = no "
             "extra delay.")
    L.append(
        f"pace_seconds_between_distillations = "
        f"{cfg.store.distillation.pace_seconds_between_distillations}"
    )
    L.append("")
    L.append("[coder_limits]")
    L.append("# Sandbox caps for the coder role (M10). Only consulted "
             "when [role.coder] enabled = true.")
    L.append(f"max_file_bytes = {cfg.coder_limits.max_file_bytes}")
    L.append(f"max_total_bytes = {cfg.coder_limits.max_total_bytes}")
    L.append(f"max_files = {cfg.coder_limits.max_files}")
    L.append("")
    L.append("[budget]")
    L.append("# Output cap and window (plan B). Unset keys use the shipped "
             "defaults: output_ceiling = 96000, output_window_share = 0.75, "
             "reserve_min = 1024, reserve_share = 0.12, num_ctx = \"model\", "
             "announce = true. Per role: [role.X.budget]; per (role, model): "
             "[role.X.models.\"<model>\"].")
    for k in BUDGET_KEYS:
        if k in cfg.budget:
            L.append(f"{k} = {_toml_value(cfg.budget[k])}")
    L.append("")
    L.append("[sampling]")
    L.append("# Sampling diversity (plan B4). Unset keys use the defaults: "
             "seed = \"random\" (per instance, never shared by same-model "
             "instances in a run; an integer s gives s + i; \"off\" sends "
             "none), temperature = the model's own, temperature_range_pct "
             "= 2 (max 10). Per role: [role.X.sampling].")
    for k in SAMPLING_KEYS:
        if k in cfg.sampling:
            L.append(f"{k} = {_toml_value(cfg.sampling[k])}")
    L.append("")
    L.append("[compaction]")
    L.append("# Compaction (plan C). Unset keys use the defaults: flow = "
             "\"reduced\" (replay writer, retrospective, one reviewer; "
             "\"full\" adds the halfway marker, two half-critics and a "
             "synthesizer; \"basic\" makes no model call), placement = "
             "\"system\", review = true, target_share = 0.25, "
             "writer_attempts = 3, ledger = true, tool_result_max_chars = "
             "32000, file_result_max_chars = 50000, stale_read_share = 0.4, "
             "reasoning_replay = \"none\". Every step runs on the role's "
             "own model. Per role: [role.X.compaction].")
    for k in COMPACTION_KEYS:
        if k in cfg.compaction:
            L.append(f"{k} = {_toml_value(cfg.compaction[k])}")
    L.append("")
    L.append("[protocol]")
    L.append("# typed: roles hand typed records to each other (plan G); the "
             "synthesizer stays prose. Off until benched.")
    L.append(f"typed = {'true' if cfg.protocol.typed else 'false'}")
    L.append("")
    es = cfg.escalation
    if es.expert_model or es.max_per_council != EscalationConfig().max_per_council:
        L.append("[escalation]")
        L.append("# Per-lane model escalation: a lane a guard ended, or one "
                 "that failed on its task, reruns once on expert_model with "
                 "a brief of the failed attempt. Empty expert_model = off.")
        L.append(f"expert_model = {_toml_value(es.expert_model)}")
        L.append(f"max_per_council = {es.max_per_council}")
        L.append("")
    g = cfg.guards
    if not g.is_default():
        L.append("[guards]")
        L.append("# The nudge profile (plan §0.7): \"strict\" for small "
                 "models — lanes must open with a tool call, the planner is "
                 "re-asked when its plan does not parse, 3 thinking-only "
                 "re-asks, the cap warning 2+ turns early. Loop guards are "
                 "always on, in no profile.")
        L.append(f"profile = {_toml_value(g.profile)}")
        if g.roles:
            L.append("")
            L.append("[guards.roles]")
            for role in sorted(g.roles):
                L.append(f"{role} = {_toml_value(g.roles[role])}")
        L.append("")
    d = cfg.decision
    if d != DecisionConfig():
        L.append("[decision]")
        L.append("# A decision model (plan H): TypeSafe's Jev when url is "
                 "unset (key from key_file or TYPESAFE_API_KEY), or a "
                 "/v1/systemone server such as xOllama. seams: "
                 + ", ".join(DECISION_SEAMS) + ".")
        L.append(f"enabled = {'true' if d.enabled else 'false'}")
        if d.url:
            L.append(f"url = {_toml_str(d.url)}")
        L.append(f"model = {_toml_str(d.model)}")
        if d.key_file:
            L.append(f"key_file = {_toml_str(d.key_file)}")
        L.append(f"floor = {d.floor!r}")
        L.append(f"high_stakes_floor = {d.high_stakes_floor!r}")
        L.append(f"timeout_s = {d.timeout_s!r}")
        L.append("seams = [" + ", ".join(_toml_str(x) for x in d.seams) + "]")
        if d.shadow:
            L.append("# asked and recorded, never acted on")
            L.append("shadow = [" + ", ".join(_toml_str(x) for x in d.shadow)
                     + "]")
        L.append("")
    for name, ep in sorted(cfg.endpoints.items()):
        L.append(f"[endpoints.{name}]")
        L.append(f"url = {_toml_str(ep.url)}")
        L.append(f"backend = {_toml_str(ep.backend)}")
        L.append(f"window = {_toml_str(ep.window)}  # dynamic: no fixed "
                 "context, each agent up to the engine's session_ctx_max")
        if ep.model:
            L.append(f"model = {_toml_str(ep.model)}")
        if ep.api_key:
            L.append(f"api_key = {_toml_str(ep.api_key)}")
        L.append("")
    for role in ROLES:
        rc = cfg.roles[role]
        L.append(f"[role.{role}]")
        L.append(f"enabled = {'true' if rc.enabled else 'false'}")
        L.append(f"model = {_toml_str(rc.model)}")
        if rc.think is not None:
            L.append(f"think = {_toml_value(rc.think)}")
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
        if rc.budget:
            L.append(f"[role.{role}.budget]")
            for k, v in rc.budget.items():
                L.append(f"{k} = {_toml_value(v)}")
            L.append("")
        if rc.sampling:
            L.append(f"[role.{role}.sampling]")
            for k, v in rc.sampling.items():
                L.append(f"{k} = {_toml_value(v)}")
            L.append("")
        if rc.compaction:
            L.append(f"[role.{role}.compaction]")
            for k, v in rc.compaction.items():
                L.append(f"{k} = {_toml_value(v)}")
            L.append("")
        for m in sorted(rc.model_overrides):
            vals = rc.model_overrides[m]
            if not vals:
                continue
            L.append(f"[role.{role}.models.{_toml_str(m)}]")
            for k, v in vals.items():
                L.append(f"{k} = {_toml_value(v)}")
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
                cwd: Optional[Path] = None,
                override_flag: Optional[bool] = None) -> Path:
    """Save the config. ``scope='user'`` writes to ~/.claude/...; any
    other value writes to the per-project file under cwd.

    ``override_user_global`` is a per-project-FILE directive, never
    written to user-global. For a project-scope write we emit it so the
    file is self-describing: an explicit ``override_flag`` (set by
    ``set_override_user_global``) wins; otherwise we PRESERVE the existing
    file's flag (default True for a brand-new file) so an ordinary
    ``set-*`` never silently flips the active-scope directive."""
    if scope == "user":
        path = user_config_path()
        emit_flag: Optional[bool] = None  # never pollute user-global
    else:
        if cwd is None:
            raise ValueError("project scope requires cwd")
        path = project_config_path(cwd)
        emit_flag = (
            override_flag if override_flag is not None
            else _override_flag(_read_toml(path))  # preserve; {} → True
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _render(cfg, override_flag=emit_flag)
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
    cfg = _load_for_edit(scope, cwd)
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


def set_endpoint(name: str, *, url: Optional[str] = None,
                 backend: Optional[str] = None,
                 model: Optional[str] = None,
                 api_key: Optional[str] = None,
                 window: Optional[str] = None,
                 scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Add or update ``[endpoints.<name>]``. ``url`` is required for a
    new endpoint; on an update unset arguments keep their value and an
    empty string clears ``model`` / ``api_key``."""
    if not valid_endpoint_name(name):
        raise ValueError(f"endpoint name {name!r} must match [A-Za-z0-9_.-]+")
    cfg = _load_for_edit(scope, cwd)
    ep = cfg.endpoints.get(name)
    if ep is None and not (url and url.strip()):
        raise ValueError(f"endpoint {name!r} does not exist; --url is required")
    if backend is not None and backend.lower() not in ENDPOINT_BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(ENDPOINT_BACKENDS)}")
    if url is not None and not url.strip().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    ep = ep or EndpointConfig(url=url.strip())
    if url:
        ep.url = url.strip().rstrip("/")
    if backend:
        ep.backend = backend.lower()
    if model is not None:
        ep.model = model.strip() or None
    if api_key is not None:
        ep.api_key = api_key.strip() or None
    if window is not None:
        ep.window = check_endpoint_window(window)
    cfg.endpoints[name] = ep
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_protocol(*, typed: Optional[bool] = None, scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    cfg = _load_for_edit(scope, cwd)
    if typed is not None:
        cfg.protocol.typed = bool(typed)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_guards(*, profile: Optional[str] = None, role: Optional[str] = None,
               scope: str = "user",
               cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set ``[guards] profile``, or one role's override with ``role``
    (``profile = "standard"`` on a role removes the override when the
    run-wide profile is standard). Bad values raise ``ValueError``."""
    if profile is None:
        raise ValueError("--profile is required (standard or strict)")
    checked = check_guard_profile(profile)
    if role is not None and role not in GUARD_ROLES:
        raise ValueError(f"role must be one of {', '.join(GUARD_ROLES)}")
    cfg = _load_for_edit(scope, cwd)
    if role is None:
        cfg.guards.profile = checked
    elif checked == cfg.guards.profile:
        cfg.guards.roles.pop(role, None)
    else:
        cfg.guards.roles[role] = checked
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_escalation(*, model: Optional[str] = None,
                   max_per_council: Optional[int] = None,
                   scope: str = "user",
                   cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set ``[escalation]``; ``model=""`` turns it off. Bad values raise
    ``ValueError``; nothing is written then."""
    if max_per_council is not None and not 0 <= int(max_per_council) <= ESCALATION_MAX:
        raise ValueError(f"max must be in [0, {ESCALATION_MAX}]")
    cfg = _load_for_edit(scope, cwd)
    if model is not None:
        cfg.escalation.expert_model = model.strip()
    if max_per_council is not None:
        cfg.escalation.max_per_council = int(max_per_council)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_xauto_retry(*, enabled: bool, scope: str = "user",
                    cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set ``[runtime] xauto_retry_infra_lanes`` (plan D9)."""
    cfg = _load_for_edit(scope, cwd)
    cfg.runtime.xauto_retry_infra_lanes = bool(enabled)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_decision(*, enabled: Optional[bool] = None,
                 url: Optional[str] = None, model: Optional[str] = None,
                 key_file: Optional[str] = None,
                 floor: Optional[float] = None,
                 high_stakes_floor: Optional[float] = None,
                 timeout_s: Optional[float] = None,
                 seams: Optional[Any] = None,
                 shadow: Optional[Any] = None, reset: bool = False,
                 scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set ``[decision]`` (plan H). Unset arguments keep their value; an
    empty ``url`` / ``key_file`` clears it (TypeSafe / the env key).
    Bad values raise ``ValueError``; nothing is written then."""
    if url and not url.strip().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    for name, v, lo, hi in (("floor", floor, 0.0, 1.0),
                            ("high_stakes_floor", high_stakes_floor, 0.0, 1.0),
                            ("timeout_s", timeout_s, 0.5, 300.0)):
        if v is not None and not lo <= float(v) <= hi:
            raise ValueError(f"{name} must be in [{lo}, {hi}]")
    checked_seams = check_decision_seams(seams) if seams is not None else None
    checked_shadow = (check_decision_seams(shadow) if shadow is not None
                      else None)
    if key_file:
        kp = Path(key_file).expanduser()
        if not kp.is_file():
            raise ValueError(f"key_file {key_file!r} is not a file")
    cfg = _load_for_edit(scope, cwd)
    d = DecisionConfig() if reset else cfg.decision
    if enabled is not None:
        d.enabled = bool(enabled)
    if url is not None:
        d.url = url.strip().rstrip("/") or None
    if model is not None and model.strip():
        d.model = model.strip()
    if key_file is not None:
        d.key_file = key_file.strip() or None
    if floor is not None:
        d.floor = float(floor)
    if high_stakes_floor is not None:
        d.high_stakes_floor = float(high_stakes_floor)
    if timeout_s is not None:
        d.timeout_s = float(timeout_s)
    if checked_seams is not None:
        d.seams = checked_seams
    if checked_shadow is not None:
        d.shadow = checked_shadow
    cfg.decision = d
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_budget(values: Optional[dict] = None, *,
               clear: Optional[list[str]] = None,
               role: Optional[str] = None, model: Optional[str] = None,
               scope: str = "user",
               cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set or clear budget knobs (plan B) in ``[budget]``,
    ``[role.<role>.budget]`` or ``[role.<role>.models."<model>"]``.
    Bad values raise ``ValueError``; nothing is written then."""
    if model and not role:
        raise ValueError("--model needs --role: per-model settings belong "
                         "to a role")
    keys = MODEL_OVERRIDE_KEYS if model else BUDGET_KEYS
    checked: dict[str, Any] = {}
    for k, v in (values or {}).items():
        if k not in keys:
            raise ValueError(f"unknown budget key {k!r} "
                             f"(keys: {', '.join(keys)})")
        checked[k] = keys[k](v)
    for k in clear or []:
        if k not in keys:
            raise ValueError(f"unknown budget key {k!r}")
    cfg = _load_for_edit(scope, cwd)
    if role:
        rc = cfg.role(role)
        if model:
            target = rc.model_overrides.setdefault(model, {})
        else:
            target = rc.budget
    else:
        target = cfg.budget
    target.update(checked)
    for k in clear or []:
        target.pop(k, None)
    if role and model and not target:
        cfg.roles[role].model_overrides.pop(model, None)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_sampling(values: Optional[dict] = None, *,
                 clear: Optional[list[str]] = None,
                 role: Optional[str] = None, model: Optional[str] = None,
                 scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set or clear sampling-diversity keys (plan B4) in ``[sampling]``,
    ``[role.<role>.sampling]`` or ``[role.<role>.models."<model>"]``."""
    if model and not role:
        raise ValueError("--model needs --role: per-model settings belong "
                         "to a role")
    checked = {}
    for k, v in (values or {}).items():
        if k not in SAMPLING_KEYS:
            raise ValueError(f"unknown sampling key {k!r} "
                             f"(keys: {', '.join(SAMPLING_KEYS)})")
        checked[k] = SAMPLING_KEYS[k](v)
    for k in clear or []:
        if k not in SAMPLING_KEYS:
            raise ValueError(f"unknown sampling key {k!r}")
    cfg = _load_for_edit(scope, cwd)
    if role:
        rc = cfg.role(role)
        target = (rc.model_overrides.setdefault(model, {}) if model
                  else rc.sampling)
    else:
        target = cfg.sampling
    target.update(checked)
    for k in clear or []:
        target.pop(k, None)
    if role and model and not target:
        cfg.roles[role].model_overrides.pop(model, None)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_compaction(values: Optional[dict] = None, *,
                   clear: Optional[list[str]] = None,
                   role: Optional[str] = None, model: Optional[str] = None,
                   scope: str = "user",
                   cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set or clear compaction keys (plan C) in ``[compaction]``,
    ``[role.<role>.compaction]`` or ``[role.<role>.models."<model>"]``.
    Bad values raise ``ValueError``; nothing is written then."""
    if model and not role:
        raise ValueError("--model needs --role: per-model settings belong "
                         "to a role")
    checked = {}
    for k, v in (values or {}).items():
        if k not in COMPACTION_KEYS:
            raise ValueError(f"unknown compaction key {k!r} "
                             f"(keys: {', '.join(COMPACTION_KEYS)})")
        checked[k] = COMPACTION_KEYS[k](v)
    for k in clear or []:
        if k not in COMPACTION_KEYS:
            raise ValueError(f"unknown compaction key {k!r}")
    cfg = _load_for_edit(scope, cwd)
    if role:
        rc = cfg.role(role)
        target = (rc.model_overrides.setdefault(model, {}) if model
                  else rc.compaction)
    else:
        target = cfg.compaction
    target.update(checked)
    for k in clear or []:
        target.pop(k, None)
    if role and model and not target:
        cfg.roles[role].model_overrides.pop(model, None)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_think(role: str, value: Any = None, *, model: Optional[str] = None,
              clear: bool = False, scope: str = "user",
              cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set or clear a role's ``think`` (plan B3), or its per-model one
    (``[role.<role>.models."<model>"] think``)."""
    checked = None if clear else check_think(value)
    cfg = _load_for_edit(scope, cwd)
    rc = cfg.role(role)
    if model:
        table = rc.model_overrides.setdefault(model, {})
        if clear:
            table.pop("think", None)
            if not table:
                rc.model_overrides.pop(model, None)
        else:
            table["think"] = checked
    else:
        rc.think = checked
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def remove_endpoint(name: str, *, scope: str = "user",
                    cwd: Optional[Path] = None) -> ConsultantsConfig:
    cfg = _load_for_edit(scope, cwd)
    if name not in cfg.endpoints:
        raise ValueError(f"endpoint {name!r} is not configured")
    del cfg.endpoints[name]
    return _save_after_change(cfg, scope=scope, cwd=cwd)


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
    cfg = _load_for_edit(scope, cwd)
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
    cfg = _load_for_edit(scope, cwd)
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
    cfg = _load_for_edit(scope, cwd)
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
    cfg = _load_for_edit(scope, cwd)
    cfg.effort = effort
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_service_mode(mode: str, *, scope: str = "user",
                     cwd: Optional[Path] = None) -> ConsultantsConfig:
    if mode not in VALID_SERVICE_MODES:
        raise ValueError(
            f"mode must be one of: {', '.join(VALID_SERVICE_MODES)}"
        )
    cfg = _load_for_edit(scope, cwd)
    cfg.service.mode = mode
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_dashboard_host(host: str, *, scope: str = "user",
                       cwd: Optional[Path] = None) -> ConsultantsConfig:
    """``""`` / ``off`` = loopback only; else an address to bind the
    read-only dashboard listener on (``0.0.0.0`` or this host's LAN IP)."""
    import ipaddress
    host = (host or "").strip()
    if host.lower() in ("off", "none", "loopback"):
        host = ""
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"{host!r} is not an IP address (use 0.0.0.0, "
                             "a LAN address, or off)") from None
    cfg = _load_for_edit(scope, cwd)
    cfg.service.dashboard_host = host
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_topology(topology: str, *, scope: str = "user",
                 cwd: Optional[Path] = None) -> ConsultantsConfig:
    if topology not in VALID_TOPOLOGIES:
        raise ValueError(
            f"topology must be one of: {', '.join(VALID_TOPOLOGIES)}"
        )
    cfg = _load_for_edit(scope, cwd)
    cfg.topology = topology
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_max_followups(value: int, *, scope: str = "user",
                      cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set the consultancy followup cap (>= 0). 0 means the first
    followup already needs user approval."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("max_followups must be an integer >= 0")
    cfg = _load_for_edit(scope, cwd)
    cfg.max_followups = value
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_allow_extra(value: int, *, scope: str = "user",
                    cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set the per-approval grant size (>= 1) — how many extra
    followups each user approval adds to the cap for that consultancy."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("allow_extra must be an integer >= 1")
    cfg = _load_for_edit(scope, cwd)
    cfg.allow_extra = value
    return _save_after_change(cfg, scope=scope, cwd=cwd)


# --------------------------------------------------------------------- #
# Adversary / verify-budget mutators (M1)
# --------------------------------------------------------------------- #

def set_verify_budget(tier: str, *, scope: str = "user",
                      cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set the Workflow skeptic-panel budget tier
    (``minimal`` | ``bounded`` | ``generous``)."""
    if tier not in VERIFY_BUDGET_TIERS:
        raise ValueError(
            f"verify_budget must be one of {', '.join(VERIFY_BUDGET_TIERS)}"
        )
    cfg = _load_for_edit(scope, cwd)
    cfg.verify_budget = tier
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_adversary_strictness(level: str, *, scope: str = "user",
                             cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set the adversary/critic-dial strictness
    (``soft`` | ``normal`` | ``strict``)."""
    if level not in ADVERSARY_STRICTNESS_LEVELS:
        raise ValueError(
            "adversary_strictness must be one of "
            f"{', '.join(ADVERSARY_STRICTNESS_LEVELS)}"
        )
    cfg = _load_for_edit(scope, cwd)
    cfg.adversary_strictness = level
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_adversary_checkpoint(enabled: bool, *,
                             timeout_s: Optional[int] = None,
                             scope: str = "user",
                             cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Enable/disable the engine adversary checkpoint (pause-for-red-team
    before synthesis). Optionally set the auto-resume timeout (>= 1 s)."""
    if not isinstance(enabled, bool):
        raise ValueError("adversary_checkpoint must be a bool")
    if timeout_s is not None and (
        not isinstance(timeout_s, int) or isinstance(timeout_s, bool)
        or timeout_s < 1
    ):
        raise ValueError("timeout_s must be an integer >= 1")
    cfg = _load_for_edit(scope, cwd)
    cfg.adversary_checkpoint = enabled
    if timeout_s is not None:
        cfg.adversary_checkpoint_timeout_s = timeout_s
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_override_user_global(enabled: bool, *,
                             cwd: Path) -> ConsultantsConfig:
    """Flip the per-project ``override_user_global`` directive — the only
    explicit writer of the flag. Always project-scoped: the flag lives
    solely in ``<cwd>/.claude-hooks/consultants.toml``.

    on  -> the per-project file is the active config (merged over
           user-global; read + written by every ``config`` command and
           the engine). off -> the file is ignored everywhere and both
           the CLI and the engine fall back to user-global.

    Preserves the project file's own content by merging user-global THEN
    the project raw dict *unconditionally* (bypassing ``load_config``'s
    gate, which for a currently-off file would drop its content and
    re-snapshot user-global). Creates the file (a full snapshot seeded
    from the current effective config) if it does not exist yet."""
    if not isinstance(enabled, bool):
        raise ValueError("override_user_global must be a bool")
    # Unconditional project merge (preserve a dormant file's content) —
    # the same base every project-scoped mutator now uses.
    cfg = _load_for_edit("project", cwd)
    save_config(cfg, scope="project", cwd=cwd, override_flag=enabled)
    return cfg


# --------------------------------------------------------------------- #
# Store / TTL / distillation mutators (#220)
# --------------------------------------------------------------------- #
# Until #220, the [store] / [store.ttl] / [store.distillation] knobs
# were TOML-only — users had to hand-edit ~/.claude/consultants-config.toml
# (or the per-project override) to flip them. These mutators surface
# every knob with the same "pass-None-to-leave-unchanged" contract that
# ``set_role`` uses, so the CLI (``config set-store``,
# ``config set-store-ttl``, ``config set-store-distillation``) and the
# installer can drive them programmatically.

VALID_STORE_BACKENDS: tuple[str, ...] = ("memory", "pgvector", "sqlite_vec")

#: The permission ladder, mirrored from
#: ``claude_hooks.tool_registry.policy.LEVELS``. Duplicated rather than
#: imported so ``consultants.config`` stays importable without
#: ``claude_hooks`` on the path — the same reason ``interrupt_policy``
#: avoids its LangGraph import. ``test_tool_registry`` pins the two
#: lists together so they cannot drift.
VALID_PERMISSION_LEVELS: tuple[str, ...] = (
    "auto", "ask_assistant", "ask_human", "deny",
)


def _render_shell(sc: ShellConfig) -> list[str]:
    """``[tools.shell]``, only the keys that differ from the default
    (all commented out when none do), so an untouched config renders the
    shell as a documented example."""
    default = ShellConfig()
    changed = [k for k in SHELL_KEYS if getattr(sc, k) != getattr(default, k)]
    L = ["", "# The sandboxed shell (run_command), off by default. Turn it on",
         "# per run with --shell, or always with default = \"always\".",
         "# mode: read (project read-only + private /scratch); net: none."]
    if not changed:
        L += ["# [tools.shell]", '# default = "off"', '# ro_binds = []']
        return L
    L.append("[tools.shell]")
    for k in changed:
        v = getattr(sc, k)
        if isinstance(v, str):
            L.append(f"{k} = {_toml_str(v)}")
        elif isinstance(v, list):
            L.append(f"{k} = [" + ", ".join(_toml_str(x) for x in v) + "]")
        else:
            L.append(f"{k} = {v:g}" if isinstance(v, float) else f"{k} = {v}")
    return L


def set_shell(*, values: dict, reset: bool = False, scope: str = "user",
              cwd: Optional[Path] = None) -> ConsultantsConfig:
    """Set ``[tools.shell]`` keys (``reset`` first restores the
    defaults) and persist. Values are validated; a bad one raises."""
    cfg = _load_for_edit(scope, cwd)
    if reset:
        cfg.tools.shell = ShellConfig()
    apply_shell(cfg.tools.shell, values, strict=True)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_tools(
    *,
    enabled: Optional[bool] = None,
    git: Optional[bool] = None,
    all_roles: Optional[bool] = None,
    default_level: Optional[str] = None,
    approval_timeout_s: Optional[float] = None,
    set_permission: Optional[tuple] = None,
    clear_permission: Optional[str] = None,
    clear_all_permissions: bool = False,
    scope: str = "user",
    cwd: Optional[Path] = None,
) -> ConsultantsConfig:
    """Mutate the ``[tools]`` block and persist.

    Same "pass None to leave unchanged" contract as ``set_role`` and
    ``set_store``. ``set_permission`` takes a ``(tool, level)`` pair.

    Levels ARE validated here, unlike at load time: a value typed at the
    CLI can be rejected immediately with the valid list, whereas a value
    already sitting in a file is better refused loudly at dispatch than
    silently dropped at load.
    """
    cfg = load_config(cwd=cwd)
    if enabled is not None:
        cfg.tools.enabled = bool(enabled)
    if git is not None:
        cfg.tools.git = bool(git)
    if all_roles is not None:
        cfg.tools.all_roles = bool(all_roles)
    if approval_timeout_s is not None:
        if float(approval_timeout_s) <= 0:
            raise ValueError(
                "approval_timeout_s must be > 0 — a zero or negative "
                "deadline denies every parked call before an approver "
                "can see it, which is 'deny' with extra steps"
            )
        if float(approval_timeout_s) < MIN_APPROVAL_TIMEOUT_S:
            raise ValueError(
                f"approval_timeout_s must be >= {MIN_APPROVAL_TIMEOUT_S:g} "
                "— a shorter deadline is un-answerable in practice: the "
                "request has to be polled, relayed to a person and "
                "decided, and Claude Code's turn latency alone eats most "
                "of a minute. Against a 30-60 minute council, three "
                "minutes costs nothing. Use 'deny' if you mean deny."
            )
        cfg.tools.approval_timeout_s = float(approval_timeout_s)
    if default_level is not None:
        lvl = default_level.strip()
        if lvl not in VALID_PERMISSION_LEVELS:
            raise ValueError(
                f"invalid permission level {lvl!r}. Valid: "
                f"{', '.join(VALID_PERMISSION_LEVELS)}")
        cfg.tools.default_level = lvl
    if set_permission is not None:
        tool, lvl = set_permission
        tool = str(tool).strip()
        lvl = str(lvl).strip()
        if not tool:
            raise ValueError("tool name must be non-empty")
        if lvl not in VALID_PERMISSION_LEVELS:
            raise ValueError(
                f"invalid permission level {lvl!r}. Valid: "
                f"{', '.join(VALID_PERMISSION_LEVELS)}")
        cfg.tools.permissions[tool] = lvl
    if clear_permission:
        cfg.tools.permissions.pop(str(clear_permission).strip(), None)
    if clear_all_permissions:
        cfg.tools.permissions = {}
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def _normalize_ttl_days(val: Optional[float]) -> Optional[float]:
    """0 or negative -> None (never expire); positive -> float."""
    if val is None:
        return None  # sentinel for "leave unchanged" handled upstream
    if val <= 0:
        return None
    return float(val)


def set_store(
    *,
    enabled: Optional[bool] = None,
    backend: Optional[str] = None,
    recall_limit: Optional[int] = None,
    sqlite_vec_path: Optional[str] = None,
    pgvector_dsn: Optional[str] = None,
    pgvector_table: Optional[str] = None,
    embedder: Optional[str] = None,
    add_enable_at_effort: Optional[str] = None,
    remove_enable_at_effort: Optional[str] = None,
    clear_enable_at_efforts: bool = False,
    scope: str = "user",
    cwd: Optional[Path] = None,
) -> ConsultantsConfig:
    """Mutate the top-level ``[store]`` block and persist.

    Each kwarg is "None = leave unchanged". Empty strings on path /
    DSN / table / embedder mean "clear the override". Effort tier
    add/remove are idempotent (no-op when the tier is already
    present / absent). The full list of toggleable tiers is the
    same as :data:`EFFORT_BUDGETS`.
    """
    cfg = _load_for_edit(scope, cwd)
    s = cfg.store
    if enabled is not None:
        s.enabled = bool(enabled)
    if backend is not None:
        b = backend.strip().lower()
        if b not in VALID_STORE_BACKENDS:
            raise ValueError(
                "backend must be one of: "
                + ", ".join(VALID_STORE_BACKENDS)
            )
        s.backend = b
    if recall_limit is not None:
        if recall_limit < 1:
            raise ValueError("recall_limit must be >= 1")
        s.recall_limit = int(recall_limit)
    if sqlite_vec_path is not None:
        v = sqlite_vec_path.strip()
        s.sqlite_vec_path = v or None
    if pgvector_dsn is not None:
        v = pgvector_dsn.strip()
        s.pgvector_dsn = v or None
    if pgvector_table is not None:
        v = pgvector_table.strip()
        s.pgvector_table = v or None
    if embedder is not None:
        v = embedder.strip()
        s.embedder = v or None
    # ``enable_at_efforts`` round-trips as a tuple (dataclass type) so
    # any in-place mutation must coerce to a list first; we keep it a
    # list on the live config since the renderer accepts either and
    # other set_store calls in the same process can append cheaply.
    if clear_enable_at_efforts:
        s.enable_at_efforts = []
    if add_enable_at_effort is not None:
        tier = add_enable_at_effort.strip()
        if tier not in EFFORT_BUDGETS:
            raise ValueError(
                "effort tier must be one of: "
                + ", ".join(sorted(EFFORT_BUDGETS))
            )
        existing = list(s.enable_at_efforts)
        if tier not in existing:
            existing.append(tier)
        s.enable_at_efforts = existing
    if remove_enable_at_effort is not None:
        tier = remove_enable_at_effort.strip()
        s.enable_at_efforts = [
            t for t in s.enable_at_efforts if t != tier
        ]
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_store_ttl(
    *,
    enabled: Optional[bool] = None,
    research_days: Optional[float] = None,
    tool_results_hours: Optional[float] = None,
    project_days: Optional[float] = None,
    user_days: Optional[float] = None,
    refresh_on_read: Optional[bool] = None,
    jitter_pct: Optional[float] = None,
    scope: str = "user",
    cwd: Optional[Path] = None,
) -> ConsultantsConfig:
    """Mutate the ``[store.ttl]`` block.

    Negative or zero day/hour values map to ``None`` (never expire).
    ``jitter_pct`` is clamped to ``[0.0, 1.0]`` — 0.0 disables the
    cohort-spread mechanic from #215.
    """
    cfg = _load_for_edit(scope, cwd)
    t = cfg.store.ttl
    if enabled is not None:
        t.enabled = bool(enabled)
    if research_days is not None:
        t.research_days = _normalize_ttl_days(research_days)
    if tool_results_hours is not None:
        t.tool_results_hours = _normalize_ttl_days(tool_results_hours)
    if project_days is not None:
        t.project_days = _normalize_ttl_days(project_days)
    if user_days is not None:
        t.user_days = _normalize_ttl_days(user_days)
    if refresh_on_read is not None:
        t.refresh_on_read = bool(refresh_on_read)
    if jitter_pct is not None:
        if jitter_pct < 0.0 or jitter_pct > 1.0:
            raise ValueError("jitter_pct must be in [0.0, 1.0]")
        t.jitter_pct = float(jitter_pct)
    return _save_after_change(cfg, scope=scope, cwd=cwd)


def set_store_distillation(
    *,
    enabled: Optional[bool] = None,
    model: Optional[str] = None,
    add_fallback_model: Optional[str] = None,
    remove_fallback_model: Optional[str] = None,
    clear_fallback_models: bool = False,
    sweep_interval_seconds: Optional[float] = None,
    min_entries_per_distillation: Optional[int] = None,
    max_session_entries: Optional[int] = None,
    max_groups_per_sweep: Optional[int] = None,
    pace_seconds_between_distillations: Optional[float] = None,
    scope: str = "user",
    cwd: Optional[Path] = None,
) -> ConsultantsConfig:
    """Mutate the ``[store.distillation]`` block.

    The fallback chain is a tuple internally; ``add_fallback_model``
    appends idempotently (no-op when already present or equal to
    the primary), ``remove_fallback_model`` drops by tag,
    ``clear_fallback_models`` empties the chain. Numeric caps are
    range-checked.
    """
    cfg = _load_for_edit(scope, cwd)
    d = cfg.store.distillation
    if enabled is not None:
        d.enabled = bool(enabled)
    if model is not None:
        m = model.strip()
        if not m:
            raise ValueError("model must be non-empty")
        d.model = m
    if clear_fallback_models:
        d.fallback_models = tuple()
    if add_fallback_model is not None:
        tag = add_fallback_model.strip()
        if not tag:
            raise ValueError("add_fallback_model must be non-empty")
        existing = list(d.fallback_models)
        if tag not in existing and tag != d.model:
            existing.append(tag)
        d.fallback_models = tuple(existing)
    if remove_fallback_model is not None:
        tag = remove_fallback_model.strip()
        d.fallback_models = tuple(
            m for m in d.fallback_models if m != tag
        )
    if sweep_interval_seconds is not None:
        if sweep_interval_seconds < 30.0:
            raise ValueError("sweep_interval_seconds must be >= 30")
        d.sweep_interval_seconds = float(sweep_interval_seconds)
    if min_entries_per_distillation is not None:
        if min_entries_per_distillation < 1:
            raise ValueError("min_entries_per_distillation must be >= 1")
        d.min_entries_per_distillation = int(min_entries_per_distillation)
    if max_session_entries is not None:
        if max_session_entries < 1:
            raise ValueError("max_session_entries must be >= 1")
        d.max_session_entries = int(max_session_entries)
    if max_groups_per_sweep is not None:
        if max_groups_per_sweep < 0:
            raise ValueError(
                "max_groups_per_sweep must be >= 0 (0 = uncapped)"
            )
        d.max_groups_per_sweep = int(max_groups_per_sweep)
    if pace_seconds_between_distillations is not None:
        if pace_seconds_between_distillations < 0.0:
            raise ValueError(
                "pace_seconds_between_distillations must be >= 0"
            )
        d.pace_seconds_between_distillations = float(
            pace_seconds_between_distillations
        )
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
