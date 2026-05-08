"""LangGraph wiring for the council pipeline.

This module is the ONLY place ``langgraph`` is imported, and the
import happens inside ``build_council_graph()`` — not at module top
— so the module itself loads cleanly in the main ``claude-hooks``
conda env (which intentionally does not have LangGraph). Calling
``build_council_graph()`` requires the dedicated
``claude-hooks-consultants`` env where the LangChain stack is
installed.

Topology:

    START -> [planner] -> [researcher] -> [critic] -> ?
                                              |
                                              +-- ready ------> [synthesizer] -> END
                                              |
                                              +-- needs_more --> [researcher]   (loop)

Roles disabled in config are simply not added as nodes; the edges
close over the gap. Synthesizer is always present (mandatory).

The graph operates on a plain ``dict`` state (LangGraph's
``StateGraph(dict)``) — same dict shape returned by
``council.initial_state()``. Each node returns a partial-state
dict that LangGraph merges into the running state via the default
dict-merge reducer.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Optional, TypedDict

from consultants.engine import council
from consultants.config import ROLES


# ----------------------- state schema --------------------------- #
# LangGraph's StateGraph treats every declared key as a channel; only
# channels are preserved across node hops. ``StateGraph(dict)`` with
# no schema works for trivial graphs but silently strips keys the
# graph hasn't seen as node return values — including the initial
# state — which is how the v1 deploy ate ``question`` between
# ``invoke()`` and the first ``planner_node`` call. Declare the full
# state shape as a TypedDict so every key the council uses is a
# preserved channel.

class CouncilState(TypedDict, total=False):
    question: str
    cwd: str
    models: dict
    topology: str
    effort: str
    plan: str
    # Numbered items extracted from the planner's output. Drives
    # researcher fan-out via Send when len >= FANOUT_MIN_ITEMS.
    plan_items: list[str]
    # Reducers: ``research`` and ``turns`` use list concat so
    # parallel sub-researchers fanned out via Send merge cleanly;
    # ``research_rounds_used`` / ``critic_reroutes_used`` /
    # ``total_*_tokens`` use int sum so per-lane counts add up.
    research: Annotated[list, operator.add]
    critique: Optional[str]
    critic_decision: Optional[str]
    final_answer: str
    turns: Annotated[list, operator.add]
    research_rounds_used: Annotated[int, operator.add]
    critic_reroutes_used: Annotated[int, operator.add]
    total_prompt_tokens: Annotated[int, operator.add]
    total_completion_tokens: Annotated[int, operator.add]
    retries_by_role: dict
    error: Optional[str]
    _role_failed: Optional[str]
    # Send-injected, per-lane fields. Set on a researcher invocation
    # spawned by the fan-out conditional edge after planner; absent
    # on the single-researcher path used by critic re-routes.
    plan_item: Optional[str]
    lane_idx: Optional[int]

log = logging.getLogger("consultants.engine.graph")


# ----------------------- builder config --------------------------- #
# Knobs are bundled into a dataclass so the caller (server / cli)
# constructs it once and graph.py doesn't grow a 12-arg signature.

@dataclass
class GraphDeps:
    """All non-state inputs the council nodes need.

    ``chat_clients`` is keyed by role so each role can use a
    different Ollama model with its own ChatClient instance — this
    matters because ChatClient holds per-call usage state and we
    want clean per-role accounting.

    ``think_by_role`` carries the per-role reasoning level
    (bool / "low" / "medium" / "high"). Defaults to ``True`` for
    every role when absent. The chat client gracefully degrades
    on models that don't accept ``think`` (proactive ``/api/show``
    probe + reactive 400 fallback) so a non-reasoning model in any
    role works without code changes.
    """
    chat_clients: dict[str, Any]            # role -> ChatClient-like
    models: dict[str, str]                  # role -> ollama tag
    enabled_roles: tuple[str, ...]          # subset of ROLES, in pipeline order
    cwd: str
    tool_executor: Optional[Callable[..., str]] = None
    tool_specs: list[dict] = field(default_factory=list)
    grounding_msgs: list[dict] = field(default_factory=list)
    think_by_role: dict[str, Any] = field(default_factory=dict)
    # When ``True``, the synthesizer takes on critic duty inside its
    # own prompt (see SYNTHESIZER_SELF_CRITIC_SYSTEM). The runner
    # sets this when effort < high so we save a full LLM round.
    synthesizer_self_critic: bool = False
    # Skip the per-node InMemoryCache. Runner sets at effort=high|max
    # so power runs always exercise every node fresh.
    disable_cache: bool = False
    # Optional MessageRecorder (Phase 1 SQLite-backed event recorder).
    # When set, every llm_call / tool_call / node_enter / node_exit
    # lands in <cwd>/.claude-hooks/consultants/<sid>/transcript.db.
    # None for unit tests that don't care about the transcript.
    recorder: Optional[Any] = None
    # Phase 5 (v1.1): per-role parent threads to extend in a follow-up.
    # Keys: 'researcher' / 'synthesizer'. Values: the prior LLM
    # message threads loaded by load_role_messages and attached by
    # the follow-up runner. When a key is present, the corresponding
    # node uses that thread + the follow-up question as its message
    # base instead of building from scratch via
    # build_*_messages(). Empty / missing keys -> existing build
    # path (fresh consultation, v1.0 parent without transcript.db).
    prior_messages_by_role: dict[str, list[dict]] = field(
        default_factory=dict,
    )
    # Phase 9 (v1.1): multi-model fan-out at x-prefixed effort tiers.
    # Keys: 'researcher' (Phase 9) / 'critic' (Phase 10). Values:
    # additional Ollama tags (already dedup'd against the primary by
    # the config loader). When non-empty, the planner->researcher
    # dispatcher spawns one researcher per [primary] + extras combo
    # per plan-item lane, and the per-lane state slice carries
    # ``model_override`` so the role node uses the right model.
    # Empty or missing key -> classic single-model fan-out.
    extra_models_by_role: dict[str, list[str]] = field(
        default_factory=dict,
    )
    # 2026-05-07: serial fallback chain for the synthesizer when its
    # primary model exhausts its retry budget on a cloud flap. NOT a
    # fan-out — the synthesizer always tries primary first and only
    # walks this list on Exception. Cloud 500's that persist past the
    # ChatClient retry budget (15 attempts / ~15 min) are the
    # motivating case (csl-2026-05-07-2158-7c75). Populated from
    # ``cfg.roles["synthesizer"].extra_models`` at every effort tier
    # (not gated by x-prefix — cloud flaps don't care about effort).
    synthesizer_fallback_models: list[str] = field(default_factory=list)


# ----------------------- node wrappers --------------------------- #
# Each wrapper picks the role's chat_client + model out of deps and
# delegates to council.<role>_node. Closures keep the signature LangGraph
# expects: (state) -> state_update.

def _think_for(deps: GraphDeps, role: str) -> Any:
    """Resolve the think value for a role; default ``True`` for any
    role missing from ``think_by_role`` so legacy callers (and
    tests) keep their existing behavior."""
    return deps.think_by_role.get(role, True)


def _wrap_planner(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.planner_node(
            state,
            chat_client=deps.chat_clients["planner"],
            model=deps.models["planner"],
            think=_think_for(deps, "planner"),
            recorder=deps.recorder,
        )
    return _node


def _wrap_researcher(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.researcher_node(
            state,
            chat_client=deps.chat_clients["researcher"],
            tool_executor=deps.tool_executor,
            tool_specs=deps.tool_specs,
            grounding_msgs=deps.grounding_msgs,
            model=deps.models["researcher"],
            cwd=deps.cwd,
            think=_think_for(deps, "researcher"),
            recorder=deps.recorder,
            prior_messages=deps.prior_messages_by_role.get("researcher"),
        )
    return _node


def _wrap_critic(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.critic_node(
            state,
            chat_client=deps.chat_clients["critic"],
            model=deps.models["critic"],
            think=_think_for(deps, "critic"),
            recorder=deps.recorder,
        )
    return _node


def _wrap_synthesizer(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.synthesizer_node(
            state,
            chat_client=deps.chat_clients["synthesizer"],
            model=deps.models["synthesizer"],
            think=_think_for(deps, "synthesizer"),
            self_critic=deps.synthesizer_self_critic,
            recorder=deps.recorder,
            prior_messages=deps.prior_messages_by_role.get("synthesizer"),
            fallback_models=list(deps.synthesizer_fallback_models),
        )
    return _node


def _wrap_meta_critic(deps: GraphDeps):
    """Phase 10: meta-critic wrapper. Uses the primary critic model
    (deps.models['critic']) and the critic role's think value. Only
    instantiated at xmax with critic extras populated — the graph
    builder routes around this node otherwise."""
    def _node(state: dict) -> dict:
        return council.meta_critic_node(
            state,
            chat_client=deps.chat_clients["critic"],
            model=deps.models["critic"],
            think=_think_for(deps, "critic"),
            recorder=deps.recorder,
        )
    return _node


# ----------------------- topology helper -------------------------- #

def plan_topology(enabled: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return the list of (from, to) edges for the council graph,
    given the enabled-role tuple. Pure function — testable without
    LangGraph. ``"START"`` and ``"END"`` are sentinels that match
    LangGraph's constants of the same names.

    Critic appears as ``"critic"`` on the LHS only when it leads
    into a CONDITIONAL edge — caller adds that separately. Here we
    only emit unconditional edges; conditional from critic is
    handled in ``build_council_graph``.
    """
    enabled_set = set(enabled)
    if "synthesizer" not in enabled_set:
        raise ValueError("synthesizer is mandatory")

    edges: list[tuple[str, str]] = []
    # Find pipeline order through enabled roles excluding synthesizer
    pipeline = [r for r in ("planner", "researcher", "critic")
                if r in enabled_set]

    if not pipeline:
        # Pathological: only synthesizer enabled. validate_pipeline
        # rejects this, but be defensive.
        edges.append(("START", "synthesizer"))
        edges.append(("synthesizer", "END"))
        return edges

    edges.append(("START", pipeline[0]))
    for i in range(len(pipeline) - 1):
        edges.append((pipeline[i], pipeline[i + 1]))
    # Last non-synth role -> synthesizer (unconditional UNLESS that
    # role is critic; critic's outgoing edge is conditional).
    last = pipeline[-1]
    if last != "critic":
        edges.append((last, "synthesizer"))
    edges.append(("synthesizer", "END"))
    return edges


# ----------------------- builder --------------------------------- #

def build_council_graph(deps: GraphDeps,
                        *, checkpointer: Optional[Any] = None,
                        tracer: Optional[Any] = None):
    """Compile the LangGraph state machine.

    Imports langgraph lazily so this module is importable in envs
    without the package. The compiled graph's ``invoke(state)``
    runs the council; ``ainvoke`` is also available.

    ``tracer`` is an optional ``consultants.engine.trace.Tracer``
    instance; when provided, every node's enter/exit emits a span.
    A disabled tracer (``CONSULTANTS_TRACE`` unset) is a no-op so
    the wrap is safe to apply unconditionally.
    """
    try:
        from langgraph.graph import StateGraph, START, END
        from langgraph.types import Send
    except ImportError as e:  # pragma: no cover — exercised in consultants env
        raise RuntimeError(
            "langgraph is not installed. The /consultants engine "
            "requires the dedicated `claude-hooks-consultants` conda "
            "env (run `python install.py` to set it up). "
            f"Underlying error: {e}"
        ) from e

    # Optional node cache. The InMemoryCache lives for the lifetime
    # of the graph instance — so it's effectively per-consultation
    # right now (every consult call rebuilds the graph). Useful for
    # critic re-routes that re-enter the planner / synthesizer with
    # the same upstream state. A persistent SQLite-backed cache
    # (TTL-keyed across consults) is a follow-up; we keep this
    # in-memory because the planner and synthesizer are pure
    # functions of (question, plan, research) and re-running them
    # at the same step inside one consult is the only realistic
    # repeat. Skipped at effort=high so a power-user run always
    # exercises every node fresh.
    cache = None
    cache_policy_planner = None
    cache_policy_synthesizer = None
    enable_cache = True
    # Effort can be discovered from the chat client model dict's
    # presence; we don't have direct access to cfg here, so deps
    # gets a flag instead. Default: cache enabled. Disable via
    # ``deps.disable_cache = True`` (set by runner at high/max).
    if getattr(deps, "disable_cache", False):
        enable_cache = False
    if enable_cache:
        try:
            from langgraph.cache.memory import InMemoryCache
            from langgraph.types import CachePolicy
            # Some langgraph versions (0.3.x) ship CachePolicy as a
            # no-field stub — calling CachePolicy(ttl=...) raises
            # ``__new__() got an unexpected keyword argument 'ttl'``.
            # Probe the signature; if it doesn't accept ttl, skip
            # the cache wiring entirely (it'd be a no-op anyway).
            try:
                _probe = CachePolicy(ttl=3600)
                cache = InMemoryCache()
                # 1h TTL — long enough to dedupe within an
                # interactive session, short enough that real
                # config / code changes invalidate quickly.
                cache_policy_planner = _probe
                cache_policy_synthesizer = CachePolicy(ttl=3600)
            except TypeError:
                log.info(
                    "langgraph CachePolicy in this version does not "
                    "accept ttl; skipping node cache (upgrade "
                    "langgraph to >= 0.4 to enable)",
                )
                cache = None
        except ImportError:  # langgraph too old to expose either
            log.info("langgraph cache API unavailable; skipping node cache")
            cache = None

    enabled = tuple(deps.enabled_roles)
    if "synthesizer" not in enabled:
        raise ValueError("synthesizer must be enabled")

    def _wrap(role: str, fn):
        if tracer is None:
            return fn
        from consultants.engine.trace import traced_node
        return traced_node(fn, role=role, tracer=tracer)

    sg = StateGraph(CouncilState)

    # Wrappers per role (closures over deps). Cache policies attach
    # only to nodes whose output is a pure function of inputs at the
    # entry point — planner (depends on question only) and
    # synthesizer (depends on question + plan + research). The
    # researcher is intentionally uncached: tool outputs reflect the
    # live filesystem, and the critic loop exists to drive multiple
    # researcher invocations with different state.
    def _maybe_cached(name: str, fn, policy):
        if cache_policy_planner is not None and policy is not None:
            sg.add_node(name, fn, cache_policy=policy)
        else:
            sg.add_node(name, fn)

    if "planner" in enabled:
        _maybe_cached(
            "planner",
            _wrap("planner", _wrap_planner(deps)),
            cache_policy_planner,
        )
    if "researcher" in enabled:
        sg.add_node("researcher",
                    _wrap("researcher", _wrap_researcher(deps)))
    # Phase 10: at xmax with critic extras, fan out the critic
    # across [primary] + extras and add a meta_critic node that
    # synthesizes the verdicts. multi_critic_active flips the
    # graph topology — see the conditional-edge wiring below.
    multi_critic_active = (
        "critic" in enabled
        and bool(deps.extra_models_by_role.get("critic"))
    )
    if "critic" in enabled:
        sg.add_node("critic", _wrap("critic", _wrap_critic(deps)))
    if multi_critic_active:
        sg.add_node(
            "meta_critic",
            _wrap("meta_critic", _wrap_meta_critic(deps)),
        )
    _maybe_cached(
        "synthesizer",
        _wrap("synthesizer", _wrap_synthesizer(deps)),
        cache_policy_synthesizer,
    )

    # Researcher fan-out: when the planner produced
    # ``len(plan_items) >= FANOUT_MIN_ITEMS`` independent steps,
    # spawn one parallel researcher invocation per step via
    # LangGraph's ``Send``. Otherwise fall through to a single
    # researcher pass with the full plan (existing behavior).
    #
    # Phase 9 (v1.1) — multi-model x-tier: when
    # ``deps.extra_models_by_role["researcher"]`` is non-empty (set
    # by the runner only when effort starts with ``x``), each lane
    # spawns ``1 + len(extras)`` parallel researchers, each using a
    # different model. Lanes interleave a stable
    # (item_lane, model_index) -> global lane_idx so the recorder's
    # transcript.db rows are uniquely identifiable. The primary
    # model is always lane index 0 within each plan-item slice.
    fanout_router = None
    if "planner" in enabled and "researcher" in enabled:
        researcher_extras = list(
            deps.extra_models_by_role.get("researcher") or []
        )

        def _fanout_after_planner(state: dict) -> Any:
            items = state.get("plan_items") or []
            if len(items) < council.FANOUT_MIN_ITEMS:
                # Below fan-out threshold: single researcher with the
                # full plan. The non-fan-out path doesn't attempt
                # multi-model; the user opted into x-tier expecting
                # multi-perspective per-lane evidence, not parallel
                # full-plan replays. (This path only fires when the
                # planner produces 1-2 items, which is rare.)
                return "researcher"
            lanes = council.group_items_into_lanes(
                items, council.FANOUT_MAX_LANES,
            )
            primary = deps.models.get("researcher", "")
            models_per_lane: list[str] = [primary] + researcher_extras
            sends: list[Send] = []
            global_idx = 0
            for item_idx, lane_items in enumerate(lanes):
                joined = council.join_lane_items(lane_items)
                for model_idx, model_tag in enumerate(models_per_lane):
                    sends.append(Send(
                        "researcher",
                        {
                            "question": state.get("question"),
                            "plan": state.get("plan", ""),
                            "cwd": state.get("cwd"),
                            "effort": state.get("effort"),
                            "models": state.get("models", {}),
                            "topology": state.get("topology"),
                            "plan_item": joined,
                            # Globally-unique lane_idx so recorder
                            # rows + per-lane reconstruction in
                            # load_role_messages stay clean. The
                            # original per-item lane is implied by
                            # `global_idx // len(models_per_lane)`.
                            "lane_idx": global_idx,
                            # Phase 9: per-lane model selection.
                            # researcher_node reads this and falls
                            # back to deps.models["researcher"] when
                            # absent (legacy fan-out path).
                            "model_override": model_tag,
                            "research": [],
                            "turns": [],
                            "research_rounds_used": 0,
                            "total_prompt_tokens": 0,
                            "total_completion_tokens": 0,
                        },
                    ))
                    global_idx += 1
            return sends
        fanout_router = _fanout_after_planner

    # Phase 10: when multi_critic_active, build a Send-based
    # dispatcher into the "critic" node. Each Send carries one
    # critic instance (lane_idx + model_override). LangGraph's
    # barrier waits for all C parallel returns before transitioning
    # to meta_critic. The predecessor is whichever role feeds critic
    # in plan_topology — researcher when enabled, planner otherwise.
    critic_predecessor: Optional[str] = None
    critic_fanout_router = None
    if multi_critic_active:
        critic_extras = list(
            deps.extra_models_by_role.get("critic") or []
        )

        def _fanout_to_critics(state: dict) -> Any:
            primary = deps.models.get("critic", "")
            models_per_lane: list[str] = [primary] + critic_extras
            sends: list[Send] = []
            for idx, model_tag in enumerate(models_per_lane):
                sends.append(Send(
                    "critic",
                    {
                        "question": state.get("question"),
                        "plan": state.get("plan", ""),
                        "cwd": state.get("cwd"),
                        "effort": state.get("effort"),
                        "models": state.get("models", {}),
                        "topology": state.get("topology"),
                        # Each critic sees the merged research from
                        # the upstream researcher fan-out.
                        "research": list(state.get("research") or []),
                        "research_rounds_used": int(
                            state.get("research_rounds_used") or 0
                        ),
                        # lane_idx flags this critic as part of a
                        # fanout (critic_node suppresses its
                        # decision/reroute deltas in that mode).
                        "lane_idx": idx,
                        "model_override": model_tag,
                        # Empty deltas so additive reducers don't
                        # double-count.
                        "turns": [],
                        "total_prompt_tokens": 0,
                        "total_completion_tokens": 0,
                        "critic_reroutes_used": 0,
                    },
                ))
            return sends

        critic_fanout_router = _fanout_to_critics
        # Determine the immediate predecessor of critic in the
        # plan_topology output — it's the source whose dst is
        # "critic" in the unconditional-edge list. The skip-and-
        # replace pattern below mirrors the planner->researcher
        # case from Phase 9.
        for src, dst in plan_topology(enabled):
            if dst == "critic":
                critic_predecessor = src
                break

    # Unconditional edges from plan_topology — skip:
    # 1. The planner -> researcher edge when researcher fan-out is
    #    wired (replaced by Phase 9 conditional below).
    # 2. The {predecessor} -> critic edge when multi-critic is
    #    active (replaced by the Phase 10 conditional fan-out).
    for src, dst in plan_topology(enabled):
        if (fanout_router is not None
                and src == "planner" and dst == "researcher"):
            continue
        if (multi_critic_active and dst == "critic"
                and src == critic_predecessor):
            continue
        src_node = START if src == "START" else src
        dst_node = END if dst == "END" else dst
        sg.add_edge(src_node, dst_node)

    if fanout_router is not None:
        sg.add_conditional_edges(
            "planner",
            fanout_router,
            ["researcher"],
        )

    if multi_critic_active and critic_predecessor is not None:
        # Phase 10a fix: insert a pass-through barrier node between
        # the (Send-multiplexed) researcher and the critic-fanout
        # dispatcher. LangGraph's add_conditional_edges fires
        # ONCE PER UPSTREAM INVOCATION when its source is
        # Send-multiplexed — wiring it directly to ``researcher``
        # (which has N×M parallel invocations from Phase 9 fan-out)
        # means the conditional fires N×M times, each emitting C
        # Sends → N×M×C critic invocations instead of C.
        #
        # An UNCONDITIONAL edge from a Send-multiplexed source DOES
        # barrier-merge before the next node fires. So we:
        #   researcher (×N×M) ─unconditional, barriers─▶ research_barrier (1 fire)
        #   research_barrier ─conditional fan-out─▶ critic (×C)
        # The barrier node itself returns {} — pure pass-through.
        # Detected on the first live xmax smoke (csl-...-2a8f) where
        # 6 researcher lanes spawned 18 critic invocations.
        def _research_barrier(state: dict) -> dict:
            return {}
        sg.add_node("research_barrier", _research_barrier)

        # Unconditional edge from the predecessor barriers any
        # upstream Send fan-out (researcher).
        pred_node = START if critic_predecessor == "START" else critic_predecessor
        sg.add_edge(pred_node, "research_barrier")

        # Conditional fan-out from the single-invocation barrier
        # node fires exactly once → exactly C critic Sends.
        sg.add_conditional_edges(
            "research_barrier",
            critic_fanout_router,
            ["critic"],
        )
        # critic (×C, barrier) -> meta_critic. Unconditional edge
        # already barriers — by the time meta_critic runs, the
        # additive reducer on ``turns`` has merged C critic
        # critiques into state["turns"].
        sg.add_edge("critic", "meta_critic")

    # Critic's conditional edge: needs_more_research -> researcher,
    # else -> synthesizer. In single-critic mode the critic itself
    # owns the conditional; in multi-critic mode meta_critic does.
    # The critic re-route uses the SINGLE-researcher path (no Send)
    # — by the time critic / meta_critic fires, the upstream
    # fan-out has already merged its results into state['research'];
    # the re-route does targeted follow-up on whatever gaps were
    # named.
    if multi_critic_active:
        decider = "meta_critic"
    elif "critic" in enabled:
        decider = "critic"
    else:
        decider = None
    if decider is not None:
        if "researcher" in enabled:
            sg.add_conditional_edges(
                decider,
                council.route_after_critic,
                {
                    council.ROUTE_RESEARCHER: "researcher",
                    council.ROUTE_SYNTHESIZER: "synthesizer",
                },
            )
        else:
            # No researcher to loop back to; the critic-side is
            # effectively advisory — straight to synthesizer.
            sg.add_edge(decider, "synthesizer")

    if cache is not None:
        return sg.compile(checkpointer=checkpointer, cache=cache)
    return sg.compile(checkpointer=checkpointer)


# ----------------------- follow-up builder ----------------------- #
# Live-session iteration: a follow-up reuses the parent's plan +
# research + critique + warm ChatClients. The graph is a SHORTENED
# variant — no planner (parent's plan stands), no researcher
# fan-out (one focused lane), critic only at high/max effort. This
# is what makes a follow-up cheap relative to a fresh consult: we
# do at most one researcher round + one synthesizer round, with
# the parent's evidence already merged into initial state.

def build_follow_up_graph(deps: GraphDeps,
                          *, checkpointer: Optional[Any] = None,
                          tracer: Optional[Any] = None):
    """Compile the SHORTENED follow-up graph.

    Topology (same conventions as ``build_council_graph``):

        START → researcher → synthesizer → END               (low/medium)
        START → researcher → critic → ?                      (high/max)
                                  │
                                  +─ ready ──────→ synthesizer
                                  │
                                  +─ needs_more ─→ researcher  (loop)

    The researcher's input state is pre-populated by the runner
    with the parent's plan + research as ``prior_rounds`` and the
    follow-up question as ``plan_item``. Synthesizer produces a
    fresh ``final_answer`` based on the merged evidence.
    """
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "langgraph is not installed. The /consultants engine "
            "requires the dedicated `claude-hooks-consultants` conda "
            f"env. Underlying error: {e}"
        ) from e

    enabled = tuple(deps.enabled_roles)
    if "synthesizer" not in enabled:
        raise ValueError("synthesizer must be enabled for follow-up")
    if "researcher" not in enabled:
        raise ValueError("researcher must be enabled for follow-up")

    def _wrap(role: str, fn):
        if tracer is None:
            return fn
        from consultants.engine.trace import traced_node
        return traced_node(fn, role=role, tracer=tracer)

    sg = StateGraph(CouncilState)
    sg.add_node("researcher", _wrap("researcher", _wrap_researcher(deps)))
    if "critic" in enabled:
        sg.add_node("critic", _wrap("critic", _wrap_critic(deps)))
    sg.add_node("synthesizer", _wrap("synthesizer", _wrap_synthesizer(deps)))

    sg.add_edge(START, "researcher")
    if "critic" in enabled:
        sg.add_edge("researcher", "critic")
        sg.add_conditional_edges(
            "critic",
            council.route_after_critic,
            {
                council.ROUTE_RESEARCHER: "researcher",
                council.ROUTE_SYNTHESIZER: "synthesizer",
            },
        )
    else:
        sg.add_edge("researcher", "synthesizer")
    sg.add_edge("synthesizer", END)

    return sg.compile(checkpointer=checkpointer)
