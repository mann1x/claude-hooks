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
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypedDict

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
    research: list
    critique: Optional[str]
    critic_decision: Optional[str]
    final_answer: str
    turns: list
    research_rounds_used: int
    critic_reroutes_used: int
    total_prompt_tokens: int
    total_completion_tokens: int
    retries_by_role: dict
    error: Optional[str]
    _role_failed: Optional[str]

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
    """
    chat_clients: dict[str, Any]            # role -> ChatClient-like
    models: dict[str, str]                  # role -> ollama tag
    enabled_roles: tuple[str, ...]          # subset of ROLES, in pipeline order
    cwd: str
    tool_executor: Optional[Callable[..., str]] = None
    tool_specs: list[dict] = field(default_factory=list)
    grounding_msgs: list[dict] = field(default_factory=list)


# ----------------------- node wrappers --------------------------- #
# Each wrapper picks the role's chat_client + model out of deps and
# delegates to council.<role>_node. Closures keep the signature LangGraph
# expects: (state) -> state_update.

def _wrap_planner(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.planner_node(
            state,
            chat_client=deps.chat_clients["planner"],
            model=deps.models["planner"],
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
        )
    return _node


def _wrap_critic(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.critic_node(
            state,
            chat_client=deps.chat_clients["critic"],
            model=deps.models["critic"],
        )
    return _node


def _wrap_synthesizer(deps: GraphDeps):
    def _node(state: dict) -> dict:
        return council.synthesizer_node(
            state,
            chat_client=deps.chat_clients["synthesizer"],
            model=deps.models["synthesizer"],
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
                        *, checkpointer: Optional[Any] = None):
    """Compile the LangGraph state machine.

    Imports langgraph lazily so this module is importable in envs
    without the package. The compiled graph's ``invoke(state)``
    runs the council; ``ainvoke`` is also available.
    """
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as e:  # pragma: no cover — exercised in consultants env
        raise RuntimeError(
            "langgraph is not installed. The /consultants engine "
            "requires the dedicated `claude-hooks-consultants` conda "
            "env (run `python install.py` to set it up). "
            f"Underlying error: {e}"
        ) from e

    enabled = tuple(deps.enabled_roles)
    if "synthesizer" not in enabled:
        raise ValueError("synthesizer must be enabled")

    sg = StateGraph(CouncilState)

    # Wrappers per role (closures over deps).
    if "planner" in enabled:
        sg.add_node("planner", _wrap_planner(deps))
    if "researcher" in enabled:
        sg.add_node("researcher", _wrap_researcher(deps))
    if "critic" in enabled:
        sg.add_node("critic", _wrap_critic(deps))
    sg.add_node("synthesizer", _wrap_synthesizer(deps))

    # Unconditional edges from plan_topology.
    for src, dst in plan_topology(enabled):
        src_node = START if src == "START" else src
        dst_node = END if dst == "END" else dst
        sg.add_edge(src_node, dst_node)

    # Critic's conditional edge: needs_more_research -> researcher,
    # else -> synthesizer. Only added when critic is enabled.
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

    return sg.compile(checkpointer=checkpointer)
