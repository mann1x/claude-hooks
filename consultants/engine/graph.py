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
        )
    return _node


def _wrap_critic(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.critic_node(
            state,
            chat_client=deps.chat_clients["critic"],
            model=deps.models["critic"],
            think=_think_for(deps, "critic"),
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
    if "critic" in enabled:
        sg.add_node("critic", _wrap("critic", _wrap_critic(deps)))
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
    fanout_router = None
    if "planner" in enabled and "researcher" in enabled:
        def _fanout_after_planner(state: dict) -> Any:
            items = state.get("plan_items") or []
            if len(items) < council.FANOUT_MIN_ITEMS:
                return "researcher"
            return [
                Send(
                    "researcher",
                    {
                        # Inherit the parent state fields the
                        # researcher needs and add the lane payload.
                        "question": state.get("question"),
                        "plan": state.get("plan", ""),
                        "cwd": state.get("cwd"),
                        "effort": state.get("effort"),
                        "models": state.get("models", {}),
                        "topology": state.get("topology"),
                        "plan_item": item,
                        "lane_idx": idx,
                        # Empty research/turns so the lane's delta is
                        # additive only with no double-count.
                        "research": [],
                        "turns": [],
                        "research_rounds_used": 0,
                        "total_prompt_tokens": 0,
                        "total_completion_tokens": 0,
                    },
                )
                for idx, item in enumerate(items)
            ]
        fanout_router = _fanout_after_planner

    # Unconditional edges from plan_topology — skip the
    # planner -> researcher edge when fan-out is wired (we replace
    # it with the conditional edge below).
    for src, dst in plan_topology(enabled):
        if (fanout_router is not None
                and src == "planner" and dst == "researcher"):
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

    # Critic's conditional edge: needs_more_research -> researcher,
    # else -> synthesizer. Only added when critic is enabled. The
    # critic re-route uses the SINGLE-researcher path (no Send) —
    # by the time critic fires, the fan-out has already merged its
    # results into state['research']; the re-route does targeted
    # follow-up on whatever gaps the critic named.
    if "critic" in enabled:
        if "researcher" in enabled:
            sg.add_conditional_edges(
                "critic",
                council.route_after_critic,
                {
                    council.ROUTE_RESEARCHER: "researcher",
                    council.ROUTE_SYNTHESIZER: "synthesizer",
                },
            )
        else:
            # No researcher to loop back to; critic is effectively
            # advisory — straight to synthesizer.
            sg.add_edge("critic", "synthesizer")

    if cache is not None:
        return sg.compile(checkpointer=checkpointer, cache=cache)
    return sg.compile(checkpointer=checkpointer)
