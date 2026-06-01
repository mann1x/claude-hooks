"""CouncilStateV2 — the state schema for the v2 council overhaul.

This module is the single source of truth for what a v2 council
session carries across nodes. The schema is **forward-compatible with
v1**: every v1 channel keeps its name and reducer semantics, so v1
nodes can read/write v2 state during the milestone-by-milestone
migration without breakage.

The new v2-specific channels are:

- ``runtime_control`` — mutable per-session knobs (deadlines, caps,
  thresholds, enabled roles, tool permissions). Reduced with a
  shallow merge so partial ``graph.update_state`` mutations preserve
  other keys.
- ``additional_context`` — append-only list of Docs injected via
  ``POST /v1/consult/<sid>/inject`` mid-flight. Researcher / planner
  / synthesizer surface unconsumed entries on the next node entry.
- ``confidence`` — append-only list of float scores emitted by the
  synthesizer + critic. The xauto escalator reads the latest entry.
- ``partial_synthesis`` — overwritten on each synthesizer iteration;
  the HITL ``interrupt_before=["synthesizer"]`` flow returns this so
  the user can preview before approving.
- ``interrupt_state`` — set by the dynamic ``interrupt()`` call,
  cleared on ``Command(resume=...)``. Lets the server expose "what
  did the council ask the human" without scraping log events.

Lives in its own module (not ``graph.py``) so the M2 async migration
+ M3 stall + M4 events work can land before the graph builder
itself sees the new schema. ``CouncilStateV2`` is meant to be
imported by the eventual M2 graph rewrite; v1 graph code keeps
working because it consumes a dict-shaped state in either schema.
"""

from __future__ import annotations

import operator
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional, TypedDict


# ---------- typed helpers ----------------------------------------- #

@dataclass(frozen=True)
class Doc:
    """One inject payload. Travels through the ``additional_context``
    channel with an append-only reducer. ``role`` is the **target**
    role the doc is meant for (e.g. ``"researcher"`` means surface
    this on the next researcher node entry); ``"any"`` surfaces it
    everywhere. ``ts`` is the server-side receive timestamp so the
    transcript reconstructs ordering correctly even when concurrent
    fanout lanes write the same channel.

    Stored as a frozen dataclass so the channel reducer can dedup
    by ``content_hash`` if a client retries an inject.
    """
    role: str
    text: str
    ts: float = field(default_factory=time.time)
    source: str = "user"
    # Stable hash for inject-retry idempotency; computed lazily by
    # ``content_hash`` property so this stays a frozen dataclass.

    @property
    def content_hash(self) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(self.role.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.text.encode("utf-8"))
        return h.hexdigest()


# ---------- M6: tool_executor channels ----------------------------- #

@dataclass(frozen=True)
class ToolPlanItem:
    """One entry in the researcher's ``tool_plan`` — a semantic
    intent ("find the function that handles X and list its
    callers") the tool_executor lane will execute via its own
    agent_loop tool subloop.

    ``why`` is the researcher's reason for the plan item, surfaced
    to the tool_executor's prompt so the specialist model has the
    *intent* not just the literal request. ``lane_idx`` is the
    plan-item index within a single researcher's PLAN-mode output
    (set by :func:`parse_tool_plan` as the enumerate position).
    ``parent_lane_idx`` identifies WHICH researcher lane emitted
    this plan item — the globally-unique ``lane_idx`` from the
    Phase 9 multi-model researcher fanout, or ``None`` on the
    single-researcher path (base tiers + the xtier short-circuit
    when planner emits < FANOUT_MIN_ITEMS). ``parent_round``
    records which researcher round emitted this plan (so a
    critic-reroute round-2 plan's results don't merge into the
    round-1 result set).

    The ``parent_lane_idx`` field is the #103 proper-composition
    bridge: the graph's post-tool_executor fanback uses it to
    re-fire each researcher lane in REPORT mode against only its
    own results, eliminating the cross-pollution that the M6
    scope note (``graph.py:1025-1030``) documents.
    """
    intent: str
    why: str = ""
    lane_idx: Optional[int] = None
    parent_lane_idx: Optional[int] = None
    parent_round: int = 1
    suggested_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolResult:
    """One ``tool_executor`` lane's output, merged via additive
    reducer back into state. ``content`` is the final text the
    specialist model produced after running its tool subloop;
    ``transcript_summary`` is a short tracer of which tools were
    called (e.g. "read_file, grep, read_file → 3 results") so the
    researcher's round-2 prompt can decide whether the plan item
    was actually satisfied.

    ``error`` is set when the lane failed (model exception or
    tool-loop runaway); the researcher should treat that intent as
    unaddressed and re-plan around it. ``duration_ms`` lets the
    M11c bench correlate per-lane wall time with answer quality.

    ``parent_lane_idx`` mirrors the field on :class:`ToolPlanItem`
    — the tool_executor lane stamps the value it received via its
    per-lane Send slice, so the researcher's REPORT-mode prompt
    builder can filter ``tool_results`` to only the results from
    its own original plan items. ``None`` on the single-researcher
    path.
    """
    intent: str
    content: str = ""
    transcript_summary: str = ""
    tools_called: list[str] = field(default_factory=list)
    lane_idx: Optional[int] = None
    parent_lane_idx: Optional[int] = None
    parent_round: int = 1
    duration_ms: int = 0
    error: Optional[str] = None


# ---------- M10: coder channels ------------------------------------ #

@dataclass(frozen=True)
class CoderLanguageRoute:
    """Task #111 — model routing entry for one language (or the global
    default). ``primary`` is tried first; on **any error** OR when the
    tool-loop tombstones with no artifacts written, the lane falls
    through to ``fallback``. Empty ``fallback`` means "no failover —
    tombstone on first failure".

    Used both as a per-language entry (keyed by language id, e.g.
    ``"csharp"``) and as the global default route (used when the
    detected language has no per-language entry). The chain is always
    primary → fallback → tombstone; the global default is *not* a
    third tier inside an in-language chain.
    """
    primary: str
    fallback: str = ""


@dataclass(frozen=True)
class CoderTaskItem:
    """One entry in the planner's ``coder_tasks`` block — a code-
    generation intent ("Write parse_iso8601(s: str) -> datetime in
    iso8601.py") the coder lane will execute via its own agent_loop
    tool subloop with a sandboxed ``write_file`` tool.

    ``task`` is the natural-language description of what to write;
    ``path`` (optional, relative) is the planner's suggested filename
    inside the coder sandbox — the coder honors it unless the model
    decides a different name fits the result better. ``why`` is the
    planner's reason, surfaced in the coder's prompt so the
    specialist model has the *intent* not just the literal request.
    ``lane_idx`` is set by the dispatcher; ``parent_round`` records
    which planner round emitted this task (rare today because the
    planner runs once, but the channel reducer mirrors M6's so a
    re-plan path lands cleanly).
    """
    task: str
    path: str = ""
    why: str = ""
    lane_idx: Optional[int] = None
    parent_round: int = 1


@dataclass(frozen=True)
class CoderArtifact:
    """One coder lane's output. The lane wrote zero or more files
    via ``write_file`` inside its sandbox; this dataclass summarizes
    what landed on disk so the synthesizer can reference the
    artifacts in the final answer without re-reading them.

    ``files`` is a list of ``{path, bytes, sha256}`` dicts where
    ``path`` is the sandbox-relative path; the absolute on-disk path
    is ``<cwd>/.claude-hooks/consultants/<sid>/coder-out/<path>``.
    ``summary`` is the coder's terminal message — typically a short
    explanation of what it wrote and why. ``error`` is set when the
    lane crashed or every write was rejected by the cap guards; the
    synthesizer treats the task as unaddressed and surfaces the gap.
    """
    task: str
    summary: str = ""
    files: list[dict] = field(default_factory=list)
    lane_idx: Optional[int] = None
    parent_round: int = 1
    duration_ms: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class InterruptState:
    """What the dynamic ``interrupt()`` call posted. Stored on state
    so the server's ``GET /v1/consult/<sid>/state`` returns a usable
    "the council is waiting for X" payload without parsing event
    streams. Cleared (set to ``None``) when ``Command(resume=...)``
    fires."""
    kind: Literal["review", "approve", "edit", "low_confidence",
                  "tool_permission", "custom"]
    prompt: str
    posted_at: float
    # Free-form details the node serializing the interrupt cares about
    # (the current draft for "review", the tool name for
    # "tool_permission", ...). Kept as Any so node implementations
    # don't need to round-trip through a polymorphic dataclass.
    payload: dict = field(default_factory=dict)


class RuntimeControl(TypedDict, total=False):
    """Per-session mutable knobs. Every node consults this at entry
    and honors the live values rather than the boot-time effort tier.
    Reduced with ``merge_runtime_control`` so ``graph.update_state(...,
    {"runtime_control": {"max_rounds": 5}})`` merges into existing
    fields instead of clobbering them.

    All fields optional; ``runtime_control_defaults`` (in
    ``engine/control.py``, M2) seeds boot-time values from the
    effort tier and the timing formula. Server-side handlers mutate
    selectively from there.
    """
    # ---- time budgets ----
    deadline_ts: float          # absolute time.time() ceiling
    soft_target_ts: float       # advisory; planner/researcher prompt sees the
                                # remainder
    per_lane_hard_s: float      # stall.hard_cap_s
    # ---- iteration caps ----
    max_rounds: int             # researcher rounds
    max_reroutes: int           # critic re-routes
    # ---- topology gates ----
    enabled_roles: list[str]    # subset of the project's roles
    # ---- quality thresholds ----
    confidence_target: float    # synthesizer self-rating cutoff
    # M4 dynamic critic dial. ``adversarial`` is reachable only via a
    # live POST /control mutation; the boot-time seed maps the static
    # ``adversary_strictness`` config (soft|normal|strict) into the
    # lax|normal|strict subset.
    critic_strictness: Literal["lax", "normal", "strict", "adversarial"]
    adversarial_focus: str      # M4 free-text attack brief ('' = none)
    # ---- stall + retry policy ----
    stall_threshold_s: float    # M3
    stall_retries: int          # M3
    # ---- tool permissions ----
    # Map of tool_name -> "allow" | "deny" | "ask". The
    # tool_executor (M6) honors this; "ask" → dynamic interrupt at
    # the first call site.
    tool_permissions: dict[str, str]
    # ---- xauto book-keeping ----
    xauto_tier: Literal["xmedium", "xhigh", "xmax"]
    xauto_escalations: int


def merge_runtime_control(left: Optional[RuntimeControl],
                          right: Optional[RuntimeControl]) -> RuntimeControl:
    """Reducer for the ``runtime_control`` channel.

    Treats ``right`` as a partial override on top of ``left``. ``None``
    values in ``right`` are ignored (use a sentinel value like
    ``-1`` to clear an int field; the explicit clear is rarely
    needed and the API is more forgiving if we just merge non-None).

    Used both by LangGraph's channel reducer machinery and by the
    HTTP control endpoint's request handler so the merge logic is
    one definition.
    """
    out: RuntimeControl = dict(left or {})  # type: ignore[assignment]
    for k, v in (right or {}).items():
        if v is None:
            continue
        out[k] = v  # type: ignore[literal-required]
    return out


def append_doc(left: Optional[list[Doc]],
               right: Optional[list[Doc]]) -> list[Doc]:
    """Reducer for ``additional_context``. Append-only with hash-based
    dedup so inject retries are idempotent.

    Ordering is preserved (left first, then right; both kept stable
    within each side) — the transcript shows injects in the order
    they reached the server.
    """
    seen = {d.content_hash for d in (left or [])}
    out: list[Doc] = list(left or [])
    for d in (right or []):
        if d.content_hash in seen:
            continue
        seen.add(d.content_hash)
        out.append(d)
    return out


def latest_or_none(left: Optional[list[Any]],
                   right: Optional[list[Any]]) -> list[Any]:
    """Reducer that keeps the full series (for telemetry / xauto
    escalation reading the trend) but exposes a ``latest`` helper.
    Lists are concatenated; callers read ``state["confidence"][-1]``
    to get the latest score.
    """
    return list(left or []) + list(right or [])


# ---------- checkpointer serde allowlist -------------------------- #
# The M9 control surface attaches a MemorySaver checkpointer (#214 +
# the 2026-05-24 follow-up fix), so every get_state/update_state
# round-trips the CouncilState channels through LangGraph's msgpack
# serde. LangGraph 1.2 deserializes our custom frozen-dataclass
# channel values with a deprecation *warning* today, but it is
# signposting a future "strict" mode where unregistered types are
# silently downgraded back to plain dicts on read — at which point
# ``additional_context_for`` / ``_additional_context_block`` (which do
# ``doc.role`` / ``doc.text`` / ``doc.content_hash``) and the turn /
# tool / coder readers would AttributeError mid-graph and crash the
# council. Registering the types in the serde allowlist keeps them as
# real instances across the round-trip AND silences the warning.
#
# This is the exhaustive set of CUSTOM (non-builtin, non-langchain)
# types that appear in the graph's ``CouncilState`` channels (see
# ``consultants/engine/graph.py:CouncilState``) plus ``RoleTurn`` from
# the ``turns`` channel. ``CoderLanguageRoute`` is deliberately absent
# — it lives only in ``GraphDeps``, never in serialized state.
# Over-inclusion is harmless (a listed type that never appears costs
# nothing); under-inclusion is the dangerous direction, so the
# drift-guard test asserts the graph schema's dataclass channels are
# all covered.

def checkpointer_msgpack_types() -> tuple[type, ...]:
    """Custom types that flow through the LangGraph CouncilState
    channels and so get serialized into the checkpointer. Single
    source of truth for :func:`make_checkpointer_serde`'s allowlist."""
    from consultants.engine.storage import RoleTurn
    return (
        Doc, ToolPlanItem, ToolResult, InterruptState,
        CoderTaskItem, CoderArtifact, RoleTurn,
    )


def make_checkpointer_serde():
    """Return a ``JsonPlusSerializer`` whose msgpack allowlist includes
    the consultants custom state types, to pass as
    ``MemorySaver(serde=...)``. Returns ``None`` when langgraph isn't
    importable (the main ``claude-hooks`` env) — ``MemorySaver`` is
    never constructed there anyway, and ``serde=None`` is a valid
    "use the default" sentinel.

    The base serializer is built strict (``allowed_msgpack_modules=
    None``) and then extended with our types via
    ``with_msgpack_allowlist`` — the permissive default (``True``)
    makes ``with_msgpack_allowlist`` a no-op, so a strict base is the
    only way to actually register the types. Built-in safe types
    (langchain messages, common stdlib) stay allowed under the strict
    base; only genuinely-unknown types are blocked, which is the
    posture we want.
    """
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    except ImportError:  # pragma: no cover — main env without langgraph
        return None
    return JsonPlusSerializer(
        allowed_msgpack_modules=None,
    ).with_msgpack_allowlist(list(checkpointer_msgpack_types()))


# ---------- the state schema -------------------------------------- #
# Imports of LangGraph's Annotated reducer machinery happen here. The
# module imports cleanly without langgraph because Annotated is a
# stdlib typing construct — only the reducer FUNCTIONS need it, and
# they're plain callables.

class CouncilStateV2(TypedDict, total=False):
    """The v2 state schema.

    Every channel either matches v1's name + reducer semantics so v1
    node code keeps working, or is new (the bottom group). The
    Annotated reducer types tell LangGraph how to merge partial
    returns across parallel fanout lanes.

    ``total=False`` so partial Sends (the researcher fanout payload)
    are valid TypedDict shapes — LangGraph treats a missing channel
    as "use the default" rather than a type error.
    """
    # ---- inputs (set at session start, never overwritten) ----
    question: str
    cwd: str
    models: dict[str, str]
    topology: str
    effort: str

    # ---- planner output ----
    plan: str
    plan_items: list[str]

    # ---- researcher fanout outputs (additive reducers across lanes) ----
    research: Annotated[list[str], operator.add]
    research_rounds_used: Annotated[int, operator.add]

    # ---- critic + synthesizer ----
    critique: Optional[str]
    critic_decision: Optional[str]
    critic_reroutes_used: Annotated[int, operator.add]
    final_answer: str

    # ---- per-turn recording (for storage.RoleTurn) ----
    turns: Annotated[list[Any], operator.add]
    total_prompt_tokens: Annotated[int, operator.add]
    total_completion_tokens: Annotated[int, operator.add]
    retries_by_role: dict[str, int]

    # ---- error tombstones ----
    error: Optional[str]
    _role_failed: Optional[str]

    # ---- Send-injected per-lane fields ----
    plan_item: Optional[str]
    lane_idx: Optional[int]
    model_override: Optional[str]
    # M6: when the graph dispatches a tool_executor lane, the per-
    # lane slice carries exactly one ToolPlanItem to execute.
    # tool_executor_node reads it (not the aggregate tool_plan
    # channel) so each lane is independent.
    tool_plan_item: Optional[ToolPlanItem]

    # ---- v2-only channels (below this line) ----

    # Runtime mutable knobs. Shallow-merge reducer so partial updates
    # preserve other fields.
    runtime_control: Annotated[RuntimeControl, merge_runtime_control]

    # Mid-flight injected context. Append-only with hash dedup.
    additional_context: Annotated[list[Doc], append_doc]

    # Synthesizer + critic emit floats here. Append-only series so
    # we keep history for escalation + telemetry; readers use [-1].
    confidence: Annotated[list[float], latest_or_none]

    # Synthesizer's work-in-progress draft. Overwritten on each
    # iteration. The HITL interrupt-before-synthesis flow shows this
    # to the user.
    partial_synthesis: Optional[str]

    # Set by the dynamic interrupt() call; cleared on Command(resume).
    interrupt_state: Optional[InterruptState]

    # M6: ``tool_plan`` carries the full set of plan items the
    # researcher emitted for this round (the graph dispatcher reads
    # it to emit one Send per item). Additive across re-routes so
    # a critic-driven round-2 plan appends to the historical
    # transcript rather than overwriting.
    tool_plan: Annotated[list[ToolPlanItem], operator.add]
    # ``tool_results`` is the corresponding additive merge of every
    # tool_executor lane's output. The researcher's next-round
    # prompt renders unconsumed results (filtered by parent_round
    # AND parent_lane_idx — see :func:`tool_results_for_round`)
    # so it can reason over the evidence without re-running tools
    # AND without seeing sibling lanes' results under x-tier
    # multi-model researcher fanout (the #103 fix).
    tool_results: Annotated[list[ToolResult], operator.add]
    # NOTE: the M6 ``awaiting_tool_results: Optional[bool]`` flag
    # was dropped by the #103 proper-composition refactor. The
    # router (``_route_after_researcher`` in ``graph.py``) now
    # derives the dispatch decision directly from ``tool_plan`` vs
    # ``tool_results`` at the current round — strictly more robust
    # than a last-writer-wins scalar under N×M parallel researcher
    # lanes. Existing checkpoints that still carry the key are
    # ignored on read (LangGraph's TypedDict state tolerates
    # unknown keys); we simply stopped writing it.

    # ---- M10: coder channels ----
    # When the planner declares the question needs code generation
    # AND ``cfg.roles.coder.enabled``, the graph's
    # ``route_after_research`` fans out one Send per ``coder_tasks``
    # entry to the coder node. ``requires_code_generation`` is the
    # gate; non-additive (last-writer-wins).
    requires_code_generation: Optional[bool]
    # Planner-emitted code-generation tasks. Additive so a (future)
    # re-plan path appends rather than clobbers; today's planner
    # emits these once at session start.
    coder_tasks: Annotated[list[CoderTaskItem], operator.add]
    # Per-lane Send-injected slice — exactly one task the coder lane
    # will execute. Non-additive; each Send is a separate state.
    coder_task_item: Optional[CoderTaskItem]
    # Coder lane outputs, additive merge across Send lanes. The
    # synthesizer renders these alongside research findings so the
    # final answer can reference the on-disk artifacts.
    coder_artifacts: Annotated[list[CoderArtifact], operator.add]


# ---------- public helpers --------------------------------------- #

def latest_confidence(state: dict) -> Optional[float]:
    """Return the most recent confidence score, or ``None`` if no
    score has been emitted yet. Used by ``EscalationDecider`` and
    the HITL interrupt policies."""
    series = state.get("confidence") or []
    if not series:
        return None
    try:
        return float(series[-1])
    except (TypeError, ValueError):
        return None


def unconsumed_context_for(state: dict, role: str) -> list[Doc]:
    """Return injected Docs intended for ``role`` (or "any") that the
    given role hasn't already seen.

    The "seen" tracking is implicit: node prompts re-render the full
    list each entry, and the reducer's hash-dedup ensures retries
    don't double-up. Roles can filter further by inspecting
    ``ctx.role`` against their own; this helper does the standard
    "role == X or role == any" filter.
    """
    out: list[Doc] = []
    for d in state.get("additional_context") or []:
        if d.role == role or d.role == "any":
            out.append(d)
    return out


def tool_results_for_round(
    state: dict,
    round: int,
    *,
    parent_lane_idx: Optional[int] = None,
) -> list["ToolResult"]:
    """Return tool_executor results matching ``round`` so the
    researcher's prompt at round N+1 only sees the round-N evidence.

    The reducer is additive (results from every round persist on
    state for post-mortems), so the filter is the read-side
    contract: caller asks for "round 1" and gets round-1 results
    only. Robust against a row where ``parent_round`` is missing —
    treats it as 1.

    ``parent_lane_idx`` is the #103 per-researcher-lane filter:
    when set, only results whose ``parent_lane_idx`` matches OR
    whose ``parent_lane_idx is None`` (legacy / single-researcher
    path) are returned. The None-tolerance matters during the
    rollout: a checkpoint persisted before the #103 refactor
    landed has ``parent_lane_idx=None`` on every ToolResult, and
    the post-refactor researcher should still see those rather
    than getting an empty filter result. When the caller passes
    ``parent_lane_idx=None`` (the pre-#103 contract, still used by
    the single-researcher non-fanout path), every matching-round
    result is returned regardless of which lane emitted it.

    Order-preserving so the REPORT-mode prompt sees results in the
    order tool_executor returned them.
    """
    target_lane = parent_lane_idx
    out: list[ToolResult] = []
    for r in state.get("tool_results") or []:
        if int(getattr(r, "parent_round", 1) or 1) != int(round):
            continue
        if target_lane is not None:
            r_lane = getattr(r, "parent_lane_idx", None)
            # Legacy tolerance: a None on the ToolResult is the
            # pre-#103 / single-researcher shape — surface it to
            # every lane filter rather than masking it.
            if r_lane is not None and r_lane != target_lane:
                continue
        out.append(r)
    return out


def coder_artifacts_for_round(state: dict, round: int) -> list["CoderArtifact"]:
    """Return coder lane outputs matching ``round``. Mirrors
    :func:`tool_results_for_round` so the synthesizer's prompt
    builder can filter to the round whose code generation is being
    summarized. Robust against rows where ``parent_round`` is
    missing — treats it as 1 (the only round the M10 planner emits).
    """
    out: list[CoderArtifact] = []
    for a in state.get("coder_artifacts") or []:
        if int(getattr(a, "parent_round", 1) or 1) == int(round):
            out.append(a)
    return out


def time_remaining_s(state: dict) -> Optional[float]:
    """Return seconds remaining until ``deadline_ts``, or ``None`` if
    no deadline is set. Negative values mean we're past the
    deadline — caller decides whether to soft-warn or hard-stop.
    """
    rc = state.get("runtime_control") or {}
    deadline = rc.get("deadline_ts")
    if not deadline:
        return None
    return float(deadline) - time.time()
