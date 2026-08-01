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
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Optional,
    TypedDict,
)

if TYPE_CHECKING:
    # The CouncilStateV2 schema below refers to these by quoted
    # forward-ref so the module stays importable without pulling in
    # state_v2 at load time (see the note on ``tool_plan_item``). The
    # quotes alone leave the names genuinely undefined — a type checker
    # cannot resolve them and ``get_type_hints()`` would raise — so
    # bind them here, where the cost is zero at runtime.
    from consultants.engine.state_v2 import (  # noqa: F401
        CoderArtifact,
        CoderTaskItem,
        Doc,
        ToolPlanItem,
        ToolResult,
    )

from consultants.engine import council


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
    # M3: the opt-in post-synthesis adversary refuter writes its raw
    # REFUTATION block here and a ``none``/``issues`` verdict in
    # ``adversary_decision``. Both non-additive (the adversary is a
    # singleton node that runs once after the synthesizer). Absent on
    # every default run (role off) — cohort-2 parity.
    final_answer_refutation: Optional[str]
    adversary_decision: Optional[str]
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
    model_override: Optional[str]
    # M6: per-lane Send-injected payload for the tool_executor node.
    # Carries exactly one ToolPlanItem the lane will execute. The
    # quoted forward-ref keeps the v1 schema importable without
    # eager-importing state_v2 at module load.
    tool_plan_item: Optional["ToolPlanItem"]

    # ---- v2 channels (M2, M5, M6, M7) ----
    # M2: mutable runtime knobs. Merged with a shallow dict-merge
    # so partial updates (e.g. ``{"max_rounds": 5}``) don't clobber
    # the rest. Same reducer as state_v2.merge_runtime_control.
    runtime_control: Annotated[dict, "merge_runtime_control"]
    # M5: append-only injected context, hash-deduped by reducer.
    additional_context: Annotated[list["Doc"], "append_doc"]
    # M6: append-only across re-routes / parallel researcher lanes.
    tool_plan: Annotated[list["ToolPlanItem"], operator.add]
    # M6: append-only across parallel tool_executor Send lanes.
    tool_results: Annotated[list["ToolResult"], operator.add]
    # M6: non-additive flag flipped True after PLAN mode and back to
    # False after REPORT mode. Read by route_after_researcher.
    awaiting_tool_results: Optional[bool]
    # M10: planner-emitted coder gate + tasks. ``requires_code_generation``
    # is non-additive (planner sets it once); ``coder_tasks`` is additive
    # so a future re-plan path appends rather than clobbers.
    requires_code_generation: Optional[bool]
    coder_tasks: Annotated[list["CoderTaskItem"], operator.add]
    # M10: per-lane Send-injected coder task + additive merge of
    # coder outputs across Send lanes.
    coder_task_item: Optional["CoderTaskItem"]
    coder_artifacts: Annotated[list["CoderArtifact"], operator.add]


# M5/M6: lazily attach the real reducer callables to the
# ``Annotated`` metadata. TypedDict class-body string forward refs
# are evaluated by ``get_type_hints`` at StateGraph construction
# time, but they can't see callable objects defined in another
# module. So we patch the Annotated metadata in place at import
# time once state_v2 is loaded — gives LangGraph the actual
# reducer function it needs.

def _wire_v2_reducers():
    """Resolve the string forward-refs in CouncilState's v2
    channel annotations to the actual callables from state_v2.

    Called at module import time. Idempotent — re-running is a
    no-op because we replace strings with callables only when
    they're still strings.
    """
    try:
        from consultants.engine.state_v2 import (
            CoderArtifact, CoderTaskItem,
            Doc, ToolPlanItem, ToolResult, append_doc,
            merge_runtime_control,
        )
    except ImportError:  # pragma: no cover
        return
    hints = CouncilState.__annotations__
    from typing import Annotated as _Ann
    # M2: runtime_control shallow-merge reducer so partial updates
    # from any node (xauto escalator, /control endpoint via
    # update_state) preserve sibling fields.
    hints["runtime_control"] = _Ann[dict, merge_runtime_control]
    # M5: additional_context with hash-dedup append.
    hints["additional_context"] = _Ann[list[Doc], append_doc]
    # M6: tool_plan / tool_results additive concat across Send lanes.
    hints["tool_plan"] = _Ann[list[ToolPlanItem], operator.add]
    hints["tool_results"] = _Ann[list[ToolResult], operator.add]
    hints["tool_plan_item"] = Optional[ToolPlanItem]
    # M10: coder_tasks / coder_artifacts additive concat across Send
    # lanes; coder_task_item is the per-lane non-additive slice.
    hints["coder_tasks"] = _Ann[list[CoderTaskItem], operator.add]
    hints["coder_artifacts"] = _Ann[list[CoderArtifact], operator.add]
    hints["coder_task_item"] = Optional[CoderTaskItem]


_wire_v2_reducers()

log = logging.getLogger("consultants.engine.graph")


# ----------------------- builder config --------------------------- #
# Knobs are bundled into a dataclass so the caller (server / cli)
# constructs it once and graph.py doesn't grow a 12-arg signature.

#: Roles that ran a single tool-free call before M-B. ``researcher`` is
#: absent because its tool loop predates this gate and is wired directly.
TOOLABLE_ROLES: tuple[str, ...] = (
    "planner", "critic", "meta_critic", "synthesizer", "adversary",
)


def _tools_for(deps: "GraphDeps", role: str) -> dict:
    """Kwargs handing ``role`` its tools, or an empty dict.

    Returning ``{}`` rather than ``tool_specs=None`` matters: the node
    signatures default these to None, so an ungated role is called with
    exactly the argument list it had before M-B.
    """
    if role not in (deps.tooled_roles or ()):
        return {}
    if not deps.tool_specs or deps.tool_executor is None:
        return {}
    return {"tool_specs": deps.tool_specs,
            "tool_executor": deps.tool_executor,
            "cwd": deps.cwd}


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
    # M-B: roles that receive ``tool_specs`` + ``tool_executor``.
    # ``researcher`` is not listed because its tool loop is wired
    # directly rather than through this gate. Empty (the default)
    # reproduces pre-M-B behaviour exactly: every role here runs a
    # single tool-free call.
    tooled_roles: tuple[str, ...] = ()
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
    # M8: optional LangGraph BaseStore for cross-lane / cross-session
    # recall. ``None`` means "no store wired" — the researcher's
    # recall helpers no-op and the council behaves exactly as in
    # pre-M8. Built by the runner via
    # :func:`consultants.engine.store.make_consultants_store`.
    store: Optional[Any] = None
    # M8: session id, needed to namespace store entries. Constant
    # for the lifetime of a single consultation. ``None`` is safe
    # — recall/record helpers tolerate it.
    sid: Optional[str] = None
    # M10: sandbox caps for the coder role. Consulted only when
    # ``coder`` appears in enabled_roles. Defaults mirror the
    # CoderLimitsConfig dataclass so a test caller can build a
    # GraphDeps without threading cfg through.
    coder_max_file_bytes: int = 50 * 1024
    coder_max_total_bytes: int = 1024 * 1024
    coder_max_files: int = 16
    # Task #111: per-model ChatClient dict for the coder role's
    # failover chain. Keys are model names (Ollama tags); values
    # are ChatClient-like objects. The runner materialises one
    # client per UNIQUE model named across the per-language map +
    # default route + legacy fallback model. Empty dict ⇒ no
    # per-language routing (the coder uses ``chat_clients["coder"]``
    # alone, full v1 back-compat).
    coder_chat_clients_by_model: dict[str, Any] = field(
        default_factory=dict,
    )
    # Task #111: per-language route map for the coder role. Same
    # shape as ``RoleConfig.routes_by_language``; the
    # ``_wrap_coder`` resolver closure walks this. Empty dict ⇒
    # no per-language routing.
    coder_routes_by_language: dict[str, Any] = field(
        default_factory=dict,
    )
    # Task #111: global default route for the coder role. ``None``
    # ⇒ fall through to the legacy ``chat_clients["coder"]`` +
    # ``models["coder"]``.
    coder_default_route: Optional[Any] = None
    # M3: strictness dial for the opt-in adversary refuter
    # (soft|normal|strict). Set by the runner from
    # ``cfg.adversary_strictness``. Consulted only when ``adversary``
    # is in ``enabled_roles``; ``normal`` is inert (the default-shape
    # prompt) so a build without the role is unaffected.
    adversary_strictness: str = "normal"


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
    # M10: when the coder role is enabled in this build, surface the
    # opt-in gate to the planner so the model can declare
    # ``requires_code_generation`` + emit ``coder_tasks``. Closure
    # captures the flag at compile time so per-invocation overhead
    # is one boolean read.
    coder_on = "coder" in deps.enabled_roles

    def _node(state: dict) -> dict:
        return council.planner_node(
            state,
            chat_client=deps.chat_clients["planner"],
            model=deps.models["planner"],
            think=_think_for(deps, "planner"),
            recorder=deps.recorder,
            coder_enabled=coder_on,
            **_tools_for(deps, "planner"),
        )
    return _node


def _wrap_researcher(deps: GraphDeps):
    # M6: when the tool_executor role is enabled in this build, flip
    # the researcher into PLAN/REPORT mode (no inline tool subloop —
    # the executor lanes are the only tool callers). Closure captures
    # the flag at compile time so the per-invocation overhead is one
    # boolean read.
    tool_exec_on = "tool_executor" in deps.enabled_roles

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
            tool_executor_enabled=tool_exec_on,
            store=deps.store,
            sid=deps.sid,
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
            **_tools_for(deps, "critic"),
        )
    return _node


def _wrap_xauto_escalator(deps: GraphDeps):
    """M7: build the xauto escalator pass-through node.

    The node runs after the critic / meta_critic and BEFORE
    ``route_after_critic`` fires. It inspects state via
    :func:`consultants.engine.escalation.next_escalation` and, when
    a decision is returned, emits a ``RuntimeMutation`` event +
    returns the matching state delta. The next routing pass sees
    the updated ``runtime_control`` caps and behaves accordingly
    (e.g. higher ``max_rounds`` allows another researcher round).

    Returns ``{}`` (pass-through, no mutation) when:
    - The run is not xauto.
    - No escalation signal is active.
    - Already at the ceiling tier.

    All cases are safe to wire unconditionally; the node is fast
    when there's nothing to do.
    """
    from consultants.engine.escalation import (
        apply_escalation,
        next_escalation,
        runtime_mutation_event_data,
    )

    def _node(state: dict) -> dict:
        decision = next_escalation(state)
        if decision is None:
            return {}
        # Emit the RuntimeMutation event so SSE consumers / live
        # dashboards see the topology grow in real time. Defensive
        # — emit() is a no-op outside a runnable context, so this
        # is safe in plain-Python unit tests.
        try:
            from consultants.engine.events import (
                RuntimeMutation, emit,
            )
            ev_data = runtime_mutation_event_data(decision)
            emit(RuntimeMutation(
                changes=ev_data["changes"],
                reason=ev_data["reason"],
            ))
        except Exception:  # pragma: no cover — defensive
            log.exception("xauto escalator: emit RuntimeMutation "
                           "raised; ignoring")
        # Recorder receives the same event for post-mortem audit.
        if deps.recorder is not None:
            try:
                deps.recorder.record_event(
                    kind="runtime_mutation",
                    role="xauto_escalator",
                    round=int(state.get("research_rounds_used") or 0),
                    lane_idx=None,
                    payload=runtime_mutation_event_data(decision),
                )
            except Exception:  # pragma: no cover
                log.exception(
                    "xauto escalator: recorder.record_event raised",
                )
        return apply_escalation(state, decision)
    return _node


def _wrap_tool_executor(deps: GraphDeps):
    """Bind the role's deps to ``tool_executor_node`` so the
    LangGraph node closure signature stays ``(state) -> dict``.

    The tool_executor uses its own ChatClient (per-role pool) +
    the SHARED ``deps.tool_executor`` callable + ``deps.tool_specs``
    from the runner. Recorder rows land tagged
    ``role="tool_executor"`` — the audit-trail separation that
    motivated the dedicated lane.
    """
    from consultants.engine.tool_executor import tool_executor_node
    role = "tool_executor"

    def _node(state: dict) -> dict:
        return tool_executor_node(
            state,
            chat_client=deps.chat_clients[role],
            tool_executor=deps.tool_executor,
            tool_specs=deps.tool_specs,
            grounding_msgs=deps.grounding_msgs,
            model=deps.models[role],
            cwd=deps.cwd,
            think=_think_for(deps, role),
            recorder=deps.recorder,
        )
    return _node


def _wrap_coder(deps: GraphDeps, *, max_file_bytes: int,
                max_total_bytes: int, max_files: int):
    """M10: bind the role's deps to ``coder_node``. The coder uses
    its own ChatClient + the dedicated sandboxed write_file tool
    (built per-lane inside ``coder_node`` so the audit buffer stays
    isolated). ``sid`` comes from ``deps.sid`` — required for the
    sandbox-root path; if ``deps.sid`` is None the coder tombstones
    with a "missing sid" error (preserves audit-trail correctness
    over silent fallback to a shared dir).

    Task #111: when ``deps.coder_chat_clients_by_model`` is non-
    empty AND (``coder_routes_by_language`` is non-empty OR
    ``coder_default_route`` is set), build a resolver closure
    that returns the (primary, fallback) chain per detected
    language. Otherwise pass ``model_chain_resolver=None`` and the
    node behaves as the single-attempt v1 callable.
    """
    from consultants.engine.coder import coder_node
    role = "coder"

    # Build the per-call resolver once at wrap time so the closure
    # body is a fast dict lookup, not a config-walk.
    resolver = _build_coder_chain_resolver(deps)

    def _node(state: dict) -> dict:
        return coder_node(
            state,
            chat_client=deps.chat_clients.get(role),
            grounding_msgs=deps.grounding_msgs,
            model=deps.models.get(role, ""),
            cwd=deps.cwd,
            sid=deps.sid or "_unknown_",
            think=_think_for(deps, role),
            recorder=deps.recorder,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_files=max_files,
            model_chain_resolver=resolver,
        )
    return _node


def _build_coder_chain_resolver(deps: GraphDeps):
    """Task #111 — return a closure mapping language id (or None) to
    the failover chain ``[(client, model_name), ...]``.

    The resolver is ``None`` when no per-language routing is
    configured AND no global default is set — that's the v1
    back-compat case where ``coder_node`` should use its single
    ``chat_client`` + ``model`` kwargs.
    """
    routes = deps.coder_routes_by_language or {}
    default_route = deps.coder_default_route
    clients_by_model = deps.coder_chat_clients_by_model or {}
    legacy_client = deps.chat_clients.get("coder")
    legacy_model = deps.models.get("coder", "")

    if not routes and default_route is None:
        return None  # back-compat fast path

    def _resolver(language: Optional[str]):
        # 1. per-language entry
        route = None
        if language and language in routes:
            route = routes[language]
        elif default_route is not None:
            route = default_route
        if route is None:
            # Shouldn't happen given the guard above, but be safe.
            if legacy_client is not None and legacy_model:
                return [(legacy_client, legacy_model)]
            return []
        chain: list[tuple[Any, str]] = []
        primary_name = (getattr(route, "primary", "") or "").strip()
        if primary_name:
            client = clients_by_model.get(primary_name)
            if client is None:
                # Fall back to the legacy client only if the model
                # name happens to match — otherwise the runner
                # forgot to materialise this client (graph-wiring
                # bug; surface it by skipping rather than crashing).
                if legacy_client is not None \
                        and legacy_model == primary_name:
                    client = legacy_client
            if client is not None:
                chain.append((client, primary_name))
        fallback_name = (getattr(route, "fallback", "") or "").strip()
        if fallback_name and fallback_name != primary_name:
            client = clients_by_model.get(fallback_name)
            if client is None \
                    and legacy_client is not None \
                    and legacy_model == fallback_name:
                client = legacy_client
            if client is not None:
                chain.append((client, fallback_name))
        return chain

    return _resolver


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
            **_tools_for(deps, "synthesizer"),
        )
    return _node


def _wrap_adversary(deps: GraphDeps):
    """M3: post-synthesis adversary refuter wrapper. Uses the
    ``adversary`` role's own model + think level + the cfg strictness
    dial. Only instantiated when ``adversary`` is in enabled_roles."""
    def _node(state: dict) -> dict:
        return council.adversary_node(
            state,
            chat_client=deps.chat_clients["adversary"],
            model=deps.models["adversary"],
            think=_think_for(deps, "adversary"),
            strictness=deps.adversary_strictness,
            recorder=deps.recorder,
            **_tools_for(deps, "adversary"),
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
            **_tools_for(deps, "meta_critic"),
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

    M10 note: ``coder`` is NOT in the natural pipeline — it's
    inserted by a conditional edge from the synthesizer-predecessor
    in ``build_council_graph`` only when both the role is enabled
    AND ``state["requires_code_generation"]`` is True. The topology
    here describes the v1-shape "default" flow that fires when the
    coder gate is closed.
    """
    enabled_set = set(enabled)
    if "synthesizer" not in enabled_set:
        raise ValueError("synthesizer is mandatory")

    # M3: the opt-in adversary refuter is a singleton that runs once
    # after the synthesizer. When enabled, the synthesizer's terminal
    # edge becomes synthesizer → adversary → END (no Send → x-tier
    # untouched). Off by default → synthesizer → END (v1 shape).
    adversary_on = "adversary" in enabled_set

    def _append_synth_tail(es: list[tuple[str, str]]) -> None:
        if adversary_on:
            es.append(("synthesizer", "adversary"))
            es.append(("adversary", "END"))
        else:
            es.append(("synthesizer", "END"))

    edges: list[tuple[str, str]] = []
    # Find pipeline order through enabled roles excluding synthesizer
    pipeline = [r for r in ("planner", "researcher", "critic")
                if r in enabled_set]

    if not pipeline:
        # Pathological: only synthesizer enabled. validate_pipeline
        # rejects this, but be defensive.
        edges.append(("START", "synthesizer"))
        _append_synth_tail(edges)
        return edges

    edges.append(("START", pipeline[0]))
    for i in range(len(pipeline) - 1):
        edges.append((pipeline[i], pipeline[i + 1]))
    # Last non-synth role -> synthesizer (unconditional UNLESS that
    # role is critic; critic's outgoing edge is conditional).
    last = pipeline[-1]
    if last != "critic":
        edges.append((last, "synthesizer"))
    _append_synth_tail(edges)
    return edges


# ----------------------- builder --------------------------------- #

def build_council_graph(deps: GraphDeps,
                        *, checkpointer: Optional[Any] = None,
                        tracer: Optional[Any] = None,
                        interrupt_before: Optional[list[str]] = None):
    """Compile the LangGraph state machine.

    Imports langgraph lazily so this module is importable in envs
    without the package. The compiled graph's ``invoke(state)``
    runs the council; ``ainvoke`` is also available.

    ``tracer`` is an optional ``consultants.engine.trace.Tracer``
    instance; when provided, every node's enter/exit emits a span.
    A disabled tracer (``CONSULTANTS_TRACE`` unset) is a no-op so
    the wrap is safe to apply unconditionally.

    ``interrupt_before`` (M5) is a list of node names to pause
    execution before (LangGraph's static HITL primitive). Callers
    typically pass ``["synthesizer"]`` when
    ``cfg.runtime.review_before_synthesis`` is on so the human can
    preview research + inject context before the final answer is
    composed. ``None`` (default) — no static interrupts.
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
    # M6: register the tool_executor node when the role is enabled.
    # The conditional edge from researcher (added below) decides per
    # invocation whether to fan out to this node via Send. Disabled
    # by default — when absent, the researcher's classic inline tool
    # subloop runs unchanged.
    tool_executor_enabled = "tool_executor" in enabled
    if tool_executor_enabled and "researcher" in enabled:
        sg.add_node("tool_executor",
                    _wrap("tool_executor", _wrap_tool_executor(deps)))
    # M10: register the coder node when the role is enabled. The
    # conditional edge from the synthesizer-predecessor (added below)
    # decides per invocation whether to fan out to one coder lane
    # per ``coder_tasks`` entry, gated on
    # ``state["requires_code_generation"]``. Disabled by default;
    # when absent, the graph behaves exactly as in pre-M10.
    coder_enabled = "coder" in enabled
    # When coder is enabled, every edge that would target
    # ``synthesizer`` in the v1 topology is redirected through a
    # ``coder_router`` pass-through node first. The router fires a
    # conditional edge that either fans out Sends to ``coder`` (one
    # per declared task) OR routes straight to ``synthesizer``. The
    # substitution is a single string everywhere — the rest of the
    # topology wiring is unaware of the coder layer.
    synthesizer_target = "coder_router" if coder_enabled else "synthesizer"
    if coder_enabled:
        sg.add_node("coder", _wrap("coder", _wrap_coder(
            deps,
            max_file_bytes=deps.coder_max_file_bytes,
            max_total_bytes=deps.coder_max_total_bytes,
            max_files=deps.coder_max_files,
        )))
        def _coder_router(state: dict) -> dict:
            # Pass-through; gating logic lives in the conditional
            # edge below so the static topology hints LangGraph
            # builds at compile time enumerate all possible targets.
            return {}
        sg.add_node("coder_router", _coder_router)
    _maybe_cached(
        "synthesizer",
        _wrap("synthesizer", _wrap_synthesizer(deps)),
        cache_policy_synthesizer,
    )
    # M3: register the opt-in adversary refuter. plan_topology emits
    # synthesizer → adversary → END when the role is enabled, so the
    # node must exist before the edge loop below wires it. Singleton
    # (no Send) → the per-lane x-tier fanout is untouched.
    if "adversary" in enabled:
        sg.add_node("adversary", _wrap("adversary", _wrap_adversary(deps)))

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

    # M6: when tool_executor is enabled, the researcher's downstream
    # is the conditional dispatcher (Send fanout to tool_executor OR
    # falls through to the natural-topology next role). Find that
    # natural next role here so the conditional knows where to fall
    # through on REPORT-mode completion.
    researcher_downstream_natural: Optional[str] = None
    if tool_executor_enabled and "researcher" in enabled:
        for src, dst in plan_topology(enabled):
            if src == "researcher":
                researcher_downstream_natural = (
                    None if dst == "END" else dst
                )
                break

    # Unconditional edges from plan_topology — skip:
    # 1. The planner -> researcher edge when researcher fan-out is
    #    wired (replaced by Phase 9 conditional below).
    # 2. The {predecessor} -> critic edge when multi-critic is
    #    active (replaced by the Phase 10 conditional fan-out).
    # 3. M6: The researcher -> {next} edge when tool_executor is
    #    enabled (replaced by the route_after_researcher conditional
    #    below).
    for src, dst in plan_topology(enabled):
        if (fanout_router is not None
                and src == "planner" and dst == "researcher"):
            continue
        if (multi_critic_active and dst == "critic"
                and src == critic_predecessor):
            continue
        if tool_executor_enabled and src == "researcher":
            continue
        src_node = START if src == "START" else src
        # M10: redirect every "synthesizer" target through the
        # coder_router when the role is enabled.
        if dst == "synthesizer" and coder_enabled:
            dst = synthesizer_target
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

    # M6: tool_executor wiring.
    #
    # When the role is enabled, the researcher becomes a PLAN/REPORT
    # alternator (see researcher_node's tool_executor_enabled branch).
    # The conditional below reads ``state["awaiting_tool_results"]``:
    #
    # - True  -> the researcher just emitted a tool_plan; fan out one
    #             Send per item to the tool_executor node.
    # - False -> we're either past the lanes (REPORT mode just
    #             completed) or skipping tool_executor entirely (empty
    #             plan / fallback path); route to the next pipeline
    #             role from plan_topology (critic or synthesizer).
    #
    # After the lanes complete, an unconditional edge from
    # tool_executor back to researcher re-enters REPORT mode. The
    # additive tool_results reducer barriers Send-multiplexed
    # tool_executor invocations before the next researcher fire — the
    # researcher's PRIOR TOOL RESULTS block sees all merged lanes in
    # one prompt.
    #
    # #103 proper composition: the M6-era scope note that gated
    # tool_executor at non-x effort tiers only is now obsolete.
    # ``_route_after_researcher`` derives the dispatch decision
    # from ``tool_plan`` vs ``tool_results`` (no scalar flag —
    # see ``state_v2.py`` for the dropped field), and the
    # post-tool_executor edge fans back out to each researcher
    # lane in REPORT mode against ONLY its own ToolResults
    # (filtered by ``parent_lane_idx``). x-tier multi-model
    # researcher fanout now composes cleanly: 2 researcher
    # lanes × 3 plan items each produces 6 ToolResults, each
    # researcher's REPORT mode prompt sees only its 3.
    if tool_executor_enabled and "researcher" in enabled:
        def _route_after_researcher(state: dict) -> Any:
            """Decide where to route after a researcher invocation.

            Derives the dispatch decision purely from
            ``tool_plan`` vs ``tool_results`` at the current
            round: are there unconsumed plan items? If yes,
            emit one Send per item to tool_executor. If no,
            fall through to the next role (REPORT-mode
            completion, empty-plan fallback, or M6 error path
            — all surface the same way).

            The ``completed`` set uses the full per-lane identity
            tuple ``(parent_round, lane_idx, parent_lane_idx)``
            so x-tier sibling researcher lanes don't mask each
            other's unconsumed items.
            """
            plan = list(state.get("tool_plan") or [])
            results = list(state.get("tool_results") or [])
            rounds_used = int(state.get("research_rounds_used") or 0)
            current_round = rounds_used + 1
            completed: set[
                tuple[int, Optional[int], Optional[int]]
            ] = set()
            for r in results:
                rr = int(getattr(r, "parent_round", 1) or 1)
                if rr == current_round:
                    completed.add((
                        rr,
                        getattr(r, "lane_idx", None),
                        getattr(r, "parent_lane_idx", None),
                    ))
            sends: list[Send] = []
            for item in plan:
                pr = int(getattr(item, "parent_round", 1) or 1)
                if pr != current_round:
                    continue
                lane = getattr(item, "lane_idx", None)
                parent_lane = getattr(item, "parent_lane_idx", None)
                if (pr, lane, parent_lane) in completed:
                    continue
                sends.append(Send(
                    "tool_executor",
                    {
                        "question": state.get("question"),
                        "cwd": state.get("cwd"),
                        "effort": state.get("effort"),
                        "models": state.get("models", {}),
                        "topology": state.get("topology"),
                        # The per-lane item the executor node will
                        # consume; carries intent + parent_round so
                        # the lane stamps its ToolResult correctly.
                        "tool_plan_item": item,
                        # ``lane_idx`` is the plan-item index
                        # within the researcher's PLAN-mode output;
                        # ``parent_lane_idx`` is the researcher
                        # lane that emitted it (#103). Both round-
                        # trip onto the resulting ToolResult so
                        # the post-tool_executor fanback can route
                        # back correctly.
                        "lane_idx": lane,
                        "parent_lane_idx": parent_lane,
                        # Empty deltas so additive reducers don't
                        # double-count anything from the outer
                        # researcher state.
                        "turns": [],
                        "total_prompt_tokens": 0,
                        "total_completion_tokens": 0,
                    },
                ))
            if not sends:
                # No unconsumed items: REPORT-mode return /
                # empty-plan fallback / M6 error — all converge
                # here. Fall through to the next role.
                return researcher_downstream_natural or synthesizer_target
            return sends

        def _fanout_after_tool_executor(state: dict) -> Any:
            """Fan back out to researcher lanes in REPORT mode
            after the Send-multiplexed tool_executor barrier
            completes.

            #103 proper composition: when N×M researcher lanes
            (Phase 9 multi-model fanout) emitted plan items,
            each lane must re-enter REPORT mode against ONLY
            its own ToolResults. The legacy unconditional
            ``tool_executor → researcher`` edge barriered all
            lanes and fired researcher once with merged state —
            that produced cross-pollination.

            Decision tree:
              - No tool_results at the current round with a non-
                None ``parent_lane_idx`` → single-researcher
                path (base tiers + xtier <FANOUT_MIN short-
                circuit). Return the string ``"researcher"`` —
                identical behavior to the old unconditional
                edge (one re-entry with merged state).
              - Otherwise, re-derive each lane's
                ``(plan_item, model_override)`` deterministically
                from ``state.plan_items`` using the same
                ``group_items_into_lanes`` + ``[primary] +
                extras`` shape ``_fanout_after_planner`` uses.
                Emit one Send per distinct ``parent_lane_idx``.
                ``research_rounds_used`` is carried through from
                state (mirroring ``_fanout_to_critics``) so
                each per-lane researcher sees the same round
                number it ran under in PLAN mode.
            """
            results = list(state.get("tool_results") or [])
            rounds_used = int(state.get("research_rounds_used") or 0)
            current_round = rounds_used + 1
            distinct: list[int] = []
            seen: set[int] = set()
            for r in results:
                rr = int(getattr(r, "parent_round", 1) or 1)
                if rr != current_round:
                    continue
                pli = getattr(r, "parent_lane_idx", None)
                if pli is None or pli in seen:
                    continue
                seen.add(pli)
                distinct.append(pli)
            if not distinct:
                # Single-researcher path — same as the old
                # unconditional edge.
                return "researcher"
            # Multi-researcher path: re-derive the per-lane
            # (plan_item, model_override) tuple deterministically.
            items_for_partition = state.get("plan_items") or []
            try:
                lanes_partition = council.group_items_into_lanes(
                    items_for_partition, council.FANOUT_MAX_LANES,
                )
            except Exception:  # pragma: no cover — defensive
                log.exception(
                    "fanback re-partition failed; degrading to "
                    "single re-entry"
                )
                return "researcher"
            primary = deps.models.get("researcher", "")
            extras = list(
                deps.extra_models_by_role.get("researcher") or []
            )
            models_per_lane: list[str] = [primary] + extras
            n_models = max(len(models_per_lane), 1)
            sends: list[Send] = []
            # Sorted order so the post-mortem transcript is
            # deterministic (matches _fanout_after_planner's
            # global_idx enumeration).
            for global_idx in sorted(distinct):
                item_idx = global_idx // n_models
                model_idx = global_idx % n_models
                if not (0 <= item_idx < len(lanes_partition)):
                    # parent_lane_idx outside the current
                    # partition — e.g. a stale checkpoint or a
                    # critic-reroute changed plan_items mid-cycle.
                    # Skip the malformed lane rather than crash.
                    continue
                joined = council.join_lane_items(
                    lanes_partition[item_idx],
                )
                model_tag = (
                    models_per_lane[model_idx]
                    if 0 <= model_idx < n_models else primary
                )
                # M13 live smoke (2026-05-17) surfaced a Send
                # state-isolation bug: LangGraph Send delivers
                # ONLY the dict's keys to the target node;
                # channels not present in the Send dict are
                # absent in the receiver's state (even with an
                # ``operator.add`` reducer at the global level).
                # The fanback must therefore explicitly hand
                # this lane's ToolResults to the receiver, or
                # ``tool_results_for_round`` returns empty and
                # the researcher falls back to PLAN mode again
                # — producing the "researcher kept re-planning,
                # synthesizer got nothing" failure pattern.
                # Filter to (parent_round == current_round) AND
                # (parent_lane_idx == this lane OR None) so the
                # composition guarantee from #103 still holds:
                # no sibling-lane results leak into this lane's
                # appendix, and legacy ``None`` rows (mid-rollout
                # checkpoints) remain readable.
                lane_results = [
                    r for r in results
                    if int(getattr(r, "parent_round", 1) or 1)
                    == current_round
                    and (
                        getattr(r, "parent_lane_idx", None) is None
                        or getattr(r, "parent_lane_idx", None)
                        == global_idx
                    )
                ]
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
                        # Globally-unique lane_idx matching the
                        # value the PLAN-mode researcher ran
                        # under, so the round-filter inside the
                        # node finds its own ToolResults via
                        # ``tool_results_for_round(state,
                        # this_round, parent_lane_idx=lane_idx)``.
                        "lane_idx": global_idx,
                        "model_override": model_tag,
                        "research": [],
                        # Carry research_rounds_used through —
                        # this is a REPORT-mode re-entry within
                        # the same round, not a new round start.
                        "research_rounds_used": rounds_used,
                        # M13 fix: pre-filtered per-lane results
                        # so REPORT-mode finds non-empty input.
                        "tool_results": lane_results,
                        "turns": [],
                        "total_prompt_tokens": 0,
                        "total_completion_tokens": 0,
                    },
                ))
            if not sends:
                # Defensive: every parent_lane_idx fell outside
                # the partition. Degrade to single re-entry.
                return "researcher"
            return sends

        # The conditional's target list must enumerate every node
        # the router can route to. Include the natural downstream +
        # tool_executor; LangGraph uses these to build the static
        # graph topology hints.
        targets: list[str] = ["tool_executor"]
        if researcher_downstream_natural:
            targets.append(researcher_downstream_natural)
        elif "synthesizer" in enabled:
            targets.append(synthesizer_target)
        sg.add_conditional_edges(
            "researcher", _route_after_researcher, targets,
        )
        # #103: replace the M6 unconditional ``tool_executor →
        # researcher`` edge with a conditional fanback that
        # respects per-researcher-lane composition. The single-
        # researcher path returns the string ``"researcher"`` so
        # behavior is identical to the old unconditional edge at
        # base tiers; the multi-researcher path emits one Send
        # per parent_lane_idx so each lane re-enters REPORT
        # mode against only its own ToolResults.
        sg.add_conditional_edges(
            "tool_executor",
            _fanout_after_tool_executor,
            ["researcher"],
        )

    # M7: xauto escalator wiring.
    #
    # Inserts a pass-through node BEFORE the critic's conditional
    # edge. The node inspects state via next_escalation() and, when
    # the xauto signals fire, mutates runtime_control (max_rounds,
    # max_reroutes, confidence_target, multi_critic) so the
    # subsequent route_after_critic call sees the bigger caps and
    # allows another reroute that previously would have been
    # disallowed.
    #
    # Active only on xauto runs — the runner sets cfg.effort first
    # at session start; the escalator node short-circuits to {}
    # (no mutation) when state.effort != "xauto", so wiring it
    # unconditionally is safe and avoids a topology-time branch.
    # (There was a ``xauto_active = "xauto" == "xauto"`` placeholder
    # here — always True, never read. The gating it described really
    # does live in the node body's is_xauto_run guard, so the variable
    # was documentation pretending to be code.)
    use_escalator = ("critic" in enabled) and "researcher" in enabled
    if use_escalator:
        sg.add_node(
            "xauto_escalator",
            _wrap("xauto_escalator", _wrap_xauto_escalator(deps)),
        )

    # Critic's conditional edge: needs_more_research -> researcher,
    # else -> synthesizer. In single-critic mode the critic itself
    # owns the conditional; in multi-critic mode meta_critic does.
    # The critic re-route uses the SINGLE-researcher path (no Send)
    # — by the time critic / meta_critic fires, the upstream
    # fan-out has already merged its results into state['research'];
    # the re-route does targeted follow-up on whatever gaps were
    # named.
    #
    # M7: when the escalator is wired, the conditional source
    # becomes the escalator (unconditional edge from
    # critic/meta_critic to escalator, then conditional from
    # escalator to researcher/synthesizer). The escalator's
    # state-delta merges into runtime_control before the conditional
    # reads it.
    if multi_critic_active:
        decider = "meta_critic"
    elif "critic" in enabled:
        decider = "critic"
    else:
        decider = None
    if decider is not None:
        if use_escalator:
            # Route critic/meta_critic → escalator unconditionally,
            # then escalator → researcher/synthesizer conditionally.
            sg.add_edge(decider, "xauto_escalator")
            conditional_source = "xauto_escalator"
        else:
            conditional_source = decider
        if "researcher" in enabled:
            sg.add_conditional_edges(
                conditional_source,
                council.route_after_critic,
                {
                    council.ROUTE_RESEARCHER: "researcher",
                    council.ROUTE_SYNTHESIZER: synthesizer_target,
                },
            )
        else:
            # No researcher to loop back to; the critic-side is
            # effectively advisory — straight to synthesizer (or
            # the coder gate when M10 active).
            sg.add_edge(conditional_source, synthesizer_target)

    # M10: coder router conditional. When the coder role is enabled,
    # every "synthesizer" target was redirected to ``coder_router``
    # above; here we wire the router's outgoing conditional. The
    # router returns:
    # - list of Sends to ``coder`` (one per ``coder_tasks`` entry)
    #   when the planner declared ``requires_code_generation=True``
    #   AND the channel is non-empty;
    # - the string ``"synthesizer"`` otherwise (no coder work needed
    #   for this consultation).
    # After the Sends complete, an unconditional edge from coder to
    # synthesizer fires — LangGraph barriers Send-multiplexed sources
    # before unconditional successors, so the synthesizer's
    # ``coder_artifacts`` read sees every lane's output merged.
    if coder_enabled:
        def _route_after_coder_gate(state: dict) -> Any:
            wants = bool(state.get("requires_code_generation"))
            tasks = list(state.get("coder_tasks") or [])
            if not wants or not tasks:
                return "synthesizer"
            # Filter to the round whose tasks haven't been executed
            # yet. For M10's planner-emits-once flow this is always
            # round 1; the filter is forward-compat with a future
            # re-plan path.
            artifacts = list(state.get("coder_artifacts") or [])
            current_round = int(
                getattr(tasks[0], "parent_round", 1) or 1
            )
            completed: set[tuple[int, Optional[int]]] = set()
            for a in artifacts:
                ar = int(getattr(a, "parent_round", 1) or 1)
                if ar == current_round:
                    completed.add(
                        (ar, getattr(a, "lane_idx", None))
                    )
            sends: list[Send] = []
            for item in tasks:
                pr = int(getattr(item, "parent_round", 1) or 1)
                if pr != current_round:
                    continue
                lane = getattr(item, "lane_idx", None)
                if (pr, lane) in completed:
                    continue
                sends.append(Send(
                    "coder",
                    {
                        "question": state.get("question"),
                        "plan": state.get("plan", ""),
                        "cwd": state.get("cwd"),
                        "effort": state.get("effort"),
                        "models": state.get("models", {}),
                        "topology": state.get("topology"),
                        # Researcher findings are needed for the coder
                        # to fit into existing code structure.
                        "research": list(state.get("research") or []),
                        "coder_task_item": item,
                        "lane_idx": lane,
                        # Empty deltas so additive reducers don't
                        # double-count outer state.
                        "turns": [],
                        "total_prompt_tokens": 0,
                        "total_completion_tokens": 0,
                    },
                ))
            if not sends:
                return "synthesizer"
            return sends
        sg.add_conditional_edges(
            "coder_router", _route_after_coder_gate,
            ["coder", "synthesizer"],
        )
        # After coder Sends complete, the additive ``coder_artifacts``
        # reducer barriers them — synthesizer sees every lane's
        # output on its first read.
        sg.add_edge("coder", "synthesizer")

    # M5: static interrupt_before plumbing. LangGraph 1.2's
    # ``StateGraph.compile(interrupt_before=[...])`` parks execution
    # before each named node so the consumer (HTTP /state endpoint,
    # SSE client) can inject context + approve before continuing.
    # We filter to the set of nodes that actually exist in this
    # compiled graph — passing a name that wasn't added is a
    # langgraph TypeError at compile time, which would surprise the
    # user. The ``compile_kwargs`` dict is the single point of
    # collecting these so the conditional cache/no-cache fallthrough
    # below doesn't duplicate the option-handling logic.
    compile_kwargs: dict[str, Any] = {"checkpointer": checkpointer}
    if cache is not None:
        compile_kwargs["cache"] = cache
    if interrupt_before:
        # Names we know are in the graph at this point — see the
        # ``sg.add_node(...)`` calls above. Filter so a caller passing
        # ["synthesizer"] when synthesizer was disabled doesn't crash.
        existing_nodes: set[str] = {
            "planner", "researcher", "tool_executor",
            "critic", "meta_critic", "coder", "coder_router",
            "synthesizer",
        }
        valid = [n for n in interrupt_before if n in existing_nodes]
        if valid:
            compile_kwargs["interrupt_before"] = valid
    # M8: thread the BaseStore through compile so LangGraph wires
    # it into the runtime context. Nodes still read it via
    # ``deps.store`` (the GraphDeps field) — this just makes the
    # store available to any future code paths that prefer
    # LangGraph's ``get_store()`` runtime helper.
    if deps.store is not None:
        compile_kwargs["store"] = deps.store
    return sg.compile(**compile_kwargs)


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
                          interrupt_before: Optional[list[str]] = None,
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
    # M3: the adversary refuter composes with follow-ups too — same
    # singleton synthesizer → adversary → END tail as the main council.
    adversary_on = "adversary" in enabled
    if adversary_on:
        sg.add_node("adversary", _wrap("adversary", _wrap_adversary(deps)))

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
    if adversary_on:
        sg.add_edge("synthesizer", "adversary")
        sg.add_edge("adversary", END)
    else:
        sg.add_edge("synthesizer", END)

    # M8: follow-up graphs share the same store as the parent.
    compile_kwargs: dict[str, Any] = {"checkpointer": checkpointer}
    if deps.store is not None:
        compile_kwargs["store"] = deps.store
    # #314: honor interrupt_before (the runner always passes
    # ["synthesizer"] so a mid-flight follow-up inject can rewind to a
    # researcher round). Filter to nodes actually present, mirroring
    # build_council_graph, so a stripped follow-up topology can't 500
    # on an unknown interrupt target.
    if interrupt_before:
        existing_nodes = {"researcher", "synthesizer"}
        if "critic" in enabled:
            existing_nodes.add("critic")
        valid = [n for n in interrupt_before if n in existing_nodes]
        if valid:
            compile_kwargs["interrupt_before"] = valid
    return sg.compile(**compile_kwargs)
