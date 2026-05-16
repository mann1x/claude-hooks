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
    *intent* not just the literal request. ``lane_idx`` is set by
    the dispatcher; ``parent_round`` records which researcher round
    emitted this plan (so a critic-reroute round-2 plan's results
    don't merge into the round-1 result set).
    """
    intent: str
    why: str = ""
    lane_idx: Optional[int] = None
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
    """
    intent: str
    content: str = ""
    transcript_summary: str = ""
    tools_called: list[str] = field(default_factory=list)
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
    critic_strictness: Literal["lax", "normal", "strict"]
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
    # prompt renders unconsumed results (filtered by parent_round)
    # so it can reason over the evidence without re-running tools.
    tool_results: Annotated[list[ToolResult], operator.add]
    # ``awaiting_tool_results`` flips True after the researcher
    # emits a plan in PLAN MODE; the graph's route_after_researcher
    # reads it to decide between tool_executor fanout vs the
    # critic/synthesizer continuation. Cleared back to False by
    # the researcher's REPORT MODE return so subsequent rounds
    # don't loop forever. Non-additive (last-writer-wins).
    awaiting_tool_results: Optional[bool]


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


def tool_results_for_round(state: dict, round: int) -> list["ToolResult"]:
    """Return tool_executor results matching ``round`` so the
    researcher's prompt at round N+1 only sees the round-N evidence.

    The reducer is additive (results from every round persist on
    state for post-mortems), so the filter is the read-side
    contract: caller asks for "round 1" and gets round-1 results
    only. Robust against a row where ``parent_round`` is missing —
    treats it as 1.
    """
    out: list[ToolResult] = []
    for r in state.get("tool_results") or []:
        if int(getattr(r, "parent_round", 1) or 1) == int(round):
            out.append(r)
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
