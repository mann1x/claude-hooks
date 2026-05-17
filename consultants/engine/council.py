"""Council pipeline — pure-logic role nodes + routing.

Each role node is a free function ``(state, **deps) -> state_update``
where ``state`` is a plain dict and the deps (``chat_client``,
``tool_executor``, ``tool_specs``) are duck-typed. The LangGraph
wiring that strings these together as a StateGraph lives in
``graph.py`` so this module stays importable without LangGraph
installed — that matters because the main ``claude-hooks`` test env
intentionally does NOT have LangGraph; only the dedicated
``claude-hooks-consultants`` conda env does.

Reuses:

- ``claude_hooks.get_advice.chat_client.ChatClient`` — duck-typed via
  ``.chat(payload) -> response_dict``. Already retries on cloud
  flakes and translates OpenAI <-> Ollama wire shapes.
- ``claude_hooks.agent_loop.runner.run_loop`` — the researcher's
  per-turn tool sub-loop. Same loop the advisor and caliber-grounding
  proxy use; we set ``force_first_tool_call=False`` because the
  researcher already has plan-level direction from the planner.
- ``claude_hooks.caliber_proxy.tools`` — ``openai_tool_specs()`` and
  ``execute()`` give us the same six tools (survey_project,
  list_files, read_file, glob, grep, recall_memory) the rest of the
  stack uses. Researcher only.
- ``claude_hooks.caliber_proxy.prompt.build_grounding_messages`` —
  prepended to the researcher's system messages so it gets the same
  project anchor + structure_map blocks as caliber.

Effort-to-budget mapping is a pure dict here; the LangGraph
conditional edge in ``graph.py`` reads it. Critic re-route counts and
researcher round counts live in the state dict.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from consultants.engine.storage import RoleTurn

log = logging.getLogger("consultants.engine.council")


# ---------- v2 event emission (M4) -------------------------------- #
# ``events.emit`` is a defensive no-op outside a LangGraph runnable
# context, so sprinkling these calls in node bodies is safe for
# plain-Python unit tests. When the node runs through a compiled
# graph, the events surface on ``astream_events``' custom channel
# and the SSE bridge demuxes them to the wire.

def _emit_started(role: str, *,
                  round: int = 1,
                  lane_idx: Optional[int] = None,
                  model: Optional[str] = None) -> None:
    """Emit a :class:`NodeStarted` event. Never raises."""
    try:
        from consultants.engine.events import NodeStarted, emit
        emit(NodeStarted(role=role, round=round,
                          lane_idx=lane_idx, model=model))
    except Exception:  # pragma: no cover
        log.exception("emit NodeStarted raised; ignored")


def _emit_finished(role: str, *,
                   round: int = 1,
                   lane_idx: Optional[int] = None,
                   duration_ms: int = 0,
                   ok: bool = True,
                   error: Optional[str] = None) -> None:
    """Emit a :class:`NodeFinished` event. Never raises."""
    try:
        from consultants.engine.events import NodeFinished, emit
        emit(NodeFinished(role=role, round=round,
                           lane_idx=lane_idx,
                           duration_ms=duration_ms,
                           ok=ok, error=error))
    except Exception:  # pragma: no cover
        log.exception("emit NodeFinished raised; ignored")


# ---------- v2 additional_context channel (M5) -------------------- #
# The v2 state schema adds an ``additional_context`` channel — an
# append-only list of ``Doc`` records the HTTP ``/inject`` endpoint
# populates mid-flight. Each Doc targets a specific role ("researcher",
# "planner", "synthesizer", "any"). Node prompt builders call
# ``_additional_context_for(state, role)`` to retrieve the docs that
# should be surfaced on this role's next entry.
#
# When ``state`` carries no v2 channel (v1 path), the helper returns
# an empty list — message builders see ``additional_context=[]`` and
# render byte-identical to the v1 prompt shape. The full migration
# to ``state_v2.unconsumed_context_for`` is gated on whether
# ``state_v2`` imports cleanly (it should always; the module is
# pure-Python with no langgraph dep), but we keep the local helper
# defensive against partial installs.

def _additional_context_for(state: dict, role: str) -> list:
    """Return injected Docs targeted at ``role`` or ``"any"``.

    Wrapping the v2 helper here keeps council.py importable on hosts
    where the v2 state module is missing for any reason — the
    fallback (empty list) preserves v1 prompt rendering exactly.
    """
    try:
        from consultants.engine.state_v2 import (
            unconsumed_context_for,
        )
    except ImportError:  # pragma: no cover — state_v2 ships in-tree
        return []
    try:
        return unconsumed_context_for(state, role)
    except Exception:  # pragma: no cover — defensive
        log.exception("_additional_context_for: helper raised; "
                       "falling back to empty list")
        return []


# ----------------------- effort -> budget ------------------------- #
# Each effort tier maps to (researcher_rounds_max, critic_reroutes_max,
# critic_enabled_when_optional). Critic enabledness here is the budget
# guidance only — config.role.critic.enabled is the real switch.
@dataclass(frozen=True)
class EffortCaps:
    researcher_rounds_max: int
    critic_reroutes_max: int
    researcher_loop_iters: int  # max_iterations passed to run_loop
    researcher_force_answer_after: int


EFFORT_CAPS: dict[str, EffortCaps] = {
    # Tuned from the 2026-05-07 trace battery (csl-...-b9d0,
    # csl-...-bee0). Diminishing returns past iter 6; with the
    # empty-output fallback in researcher_node, even tighter caps
    # remain reliable. Send-API fan-out at medium runs 3 lanes in
    # parallel so per-lane iter budget multiplies effective
    # exploration.
    #
    # low: planner -> ≤4 researcher iters -> synthesizer (no critic)
    "low":    EffortCaps(1, 0,  4, 3),
    # medium: 3 fan-out lanes × 4 iters each, 1 round, ≤1 reroute
    "medium": EffortCaps(1, 1,  4, 3),
    # high: full council, ≤3 rounds, ≤2 reroutes, ≤10 iters
    "high":   EffortCaps(3, 2, 10,  8),
    # max: large but bounded — token spend caps in practice
    "max":    EffortCaps(8, 5, 25, 18),
}


def caps_for(effort: str) -> EffortCaps:
    """Return the budget caps for an effort tier. Phase 9 x-prefixed
    tiers (xmedium / xhigh / xmax) inherit caps from their base tier
    — the engine still respects the same per-lane iter limits and
    re-route count; the multi-model fan-out happens orthogonally
    via ``deps.extra_models_by_role``.
    """
    if effort.startswith("x") and effort[1:] in EFFORT_CAPS:
        effort = effort[1:]
    return EFFORT_CAPS.get(effort, EFFORT_CAPS["medium"])


# ----------------------- prompts ---------------------------------- #
# Prompt design philosophy is borrowed verbatim from
# ``claude_hooks.get_advice.cli.ADVISOR_PREAMBLE``: an LLM-to-LLM
# preamble that pins concise / high-density / decisive replies + no
# filler, prepended to every role's system message. The role-specific
# tail then gives each agent its actual job. This split lets us tune
# token economics globally (one constant) without scattering tone
# directives across four prompts.
#
# Original v1 ran the audit query at 42k tokens (8.5k of which was
# the synthesizer wrapping a 4-line list in multiple paragraphs); v2
# inherits get-advice's "no filler, no sign-offs, no restating the
# question" rules to shrink that.

COUNCIL_PREAMBLE = (
    "You are an LLM agent in a direct LLM-to-LLM council, NOT talking "
    "to a human. The other roles (planner, researcher, critic, "
    "synthesizer) are also LLMs reading your output as input on the "
    "next hop. Optimize for: concise replies, high information "
    "density per token, decisive recommendations where you have "
    "grounds, explicit uncertainty where you do not. Avoid filler, "
    "avoid restating the question, avoid sign-offs, avoid markdown "
    "ceremony when bullets or short prose are clearer. When the "
    "question is technical, ground claims in the actual project "
    "files (path:line) — do not guess from names alone."
)


def _role_prompt(role_specific: str) -> str:
    """Prepend the council preamble to a role's specific job
    description. Single source of truth for tone."""
    return COUNCIL_PREAMBLE + "\n\n" + role_specific


PLANNER_SYSTEM = _role_prompt(
    "ROLE: planner. Decompose the question into 3-7 concrete "
    "investigation steps the researcher will execute with project "
    "tools. Each step is one sentence pointing at WHERE to look "
    "(file/path/symbol) or WHAT to verify. Reject vague steps like "
    "'understand the code'. You are NOT answering the question — "
    "your output is the researcher's plan.\n\n"
    "Output: numbered list, nothing else. No preamble, no closing."
)

RESEARCHER_SYSTEM = _role_prompt(
    "ROLE: researcher. Execute the planner's numbered plan with the "
    "available tools (read_file, grep, glob, list_files, "
    "survey_project, recall_memory). Cite findings as `path:line`. "
    "When the plan suggests a line number, verify it with grep first "
    "before trusting — line numbers in the prompt may be stale.\n\n"
    "BATCH TOOL CALLS. When you need multiple files / patterns, "
    "request them all in ONE turn's tool_calls list — sequential "
    "single-tool turns waste 20-40s of cloud round-trip per turn. "
    "Concretely: if the plan says 'read foo.py and grep for bar', "
    "emit both calls in one assistant turn, not two.\n\n"
    "After tool calls, write a focused report: one short bullet per "
    "finding, each with a `path:line` reference. Do NOT speculate "
    "beyond evidence. Do NOT answer the user's question — that's "
    "the synthesizer's job. The critic reads this; verbosity costs "
    "another full council round."
)

CRITIC_SYSTEM = _role_prompt(
    "ROLE: critic. Decide whether the researcher's evidence is "
    "sufficient for the synthesizer.\n\n"
    "Output line 1: `DECISION: ready` OR `DECISION: needs_more_research`.\n"
    "Output line 2+: if ready, one short paragraph explaining why; "
    "if more research is needed, 1-3 specific gaps as `path:line` or "
    "symbol pointers.\n\n"
    "Default to ready unless you can name a concrete missing fact. "
    "Do not request research for theoretical completeness — each "
    "extra round costs another full agent loop."
)

# Phase 10: meta-critic synthesizes the C parallel-critic verdicts
# at xmax. Same output contract as CRITIC_SYSTEM (DECISION: line
# first, then justification) so the existing parse_critic_decision
# + route_after_critic conditional edge work unchanged. The prompt
# emphasizes weighing diverse model perspectives — that's the whole
# point of multi-critic — without caving to any single voice.
META_CRITIC_SYSTEM = _role_prompt(
    "ROLE: meta-critic. Multiple critics have independently judged "
    "whether the researcher's evidence is sufficient. Their training "
    "differs and they may disagree. Your job: weigh the verdicts and "
    "emit ONE final decision the synthesizer will see.\n\n"
    "Output line 1: `DECISION: ready` OR `DECISION: needs_more_research`.\n"
    "Output line 2+: a synthesized critique. When critics agreed, "
    "consolidate their reasoning in 1-3 sentences. When they "
    "disagreed, name the disagreement explicitly and pick a side — "
    "do NOT punt with 'depends'. Cite `path:line` when the gap is "
    "concrete.\n\n"
    "Bias toward `ready` unless at least one critic named a "
    "concrete missing fact AND that fact is plausibly load-bearing "
    "for the synthesizer's answer. A critic raising a theoretical "
    "concern that another critic credibly dismisses is NOT grounds "
    "for more research."
)


def build_meta_critic_messages(
    question: str, plan: str,
    research_rounds: list[str],
    critic_verdicts: list[str],
) -> list[dict]:
    """Build the meta-critic's prompt. Critics are anonymized as
    ``Critic 1``, ``Critic 2``, ... in the order ``critic_verdicts``
    arrives — the recorder's per-row ``model`` column is the audit
    map back to which Ollama tag produced which verdict.

    Identity is anonymized to avoid biasing the meta-critic toward
    a model it 'knows' performs better. The recorder is the source
    of truth for who-said-what.
    """
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    if not critic_verdicts:
        parts.append(
            "\n(no critic verdicts — meta-critic invoked with no "
            "input; default to ready unless evidence is obviously "
            "thin)"
        )
    else:
        for i, v in enumerate(critic_verdicts, start=1):
            parts.append(f"\nCRITIC {i} VERDICT:\n{v.strip()}")
    parts.append(
        "\nSynthesize. Emit the final DECISION line and a single "
        "consolidated critique paragraph."
    )
    return [
        {"role": "system", "content": META_CRITIC_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]

SYNTHESIZER_SYSTEM = _role_prompt(
    "ROLE: synthesizer. Write the final answer the user will see, "
    "consuming the planner's plan, researcher's report(s), and "
    "critic's verdict. Lead with the bottom line on the first line. "
    "Cite `path:line` for every codebase-dependent claim.\n\n"
    "Output budget: match the question's shape. List-shaped "
    "questions get a bulleted list, no preamble. Yes/no questions "
    "get one decisive sentence + one short justification paragraph. "
    "Do NOT mention the council, the roles, or the process — the "
    "user only wants the answer. Do NOT restate the question. Do "
    "NOT add markdown headings unless the answer genuinely has 3+ "
    "distinct sections.\n\n"
    "If any researcher report is a failure tombstone — a string that "
    "begins with `(researcher lane failed:` or `(planner failed:` — "
    "do NOT silently synthesize over the gap. Either name the "
    "specific plan item or sub-question that could not be verified "
    "(\"could not verify <X> because <lane> failed: <error>\"), or, "
    "when the surviving evidence is too thin to answer at all, "
    "state that plainly and stop. The tombstone is the system's "
    "signal that a lane crashed; never treat it as evidence."
)

# Self-critic variant — used at effort=low/medium when the critic
# node is dropped from the graph to save a full LLM round (192s on
# the 2026-05-07 trace's smoke). The synthesizer does the
# critic-duty internally before producing the answer. At effort=high
# the dedicated critic stays. Keep this prompt short — the
# synthesizer's reasoning chain handles the actual self-critique;
# the prompt just authorizes it.
SYNTHESIZER_SELF_CRITIC_SYSTEM = _role_prompt(
    "ROLE: synthesizer (with self-critic). Write the final answer "
    "the user will see, consuming the planner's plan and "
    "researcher's report(s). There is no separate critic in this "
    "run — before answering, internally identify the weakest "
    "claim in the research and either bolster it from the "
    "evidence or downgrade it to a labeled uncertainty in your "
    "output. Lead with the bottom line. Cite `path:line` for "
    "every codebase-dependent claim.\n\n"
    "Output budget: match the question's shape. List-shaped "
    "questions get a bulleted list, no preamble. Yes/no questions "
    "get one decisive sentence + one short justification paragraph. "
    "Do NOT mention the council, the roles, or the process — the "
    "user only wants the answer. Do NOT restate the question. Do "
    "NOT add markdown headings unless the answer genuinely has 3+ "
    "distinct sections.\n\n"
    "If any researcher report is a failure tombstone — a string that "
    "begins with `(researcher lane failed:` or `(planner failed:` — "
    "do NOT silently synthesize over the gap. Either name the "
    "specific plan item or sub-question that could not be verified "
    "(\"could not verify <X> because <lane> failed: <error>\"), or, "
    "when the surviving evidence is too thin to answer at all, "
    "state that plainly and stop. The tombstone is the system's "
    "signal that a lane crashed; never treat it as evidence."
)


def _additional_context_block(additional_context) -> str:
    """Render the v2 ``additional_context`` channel as a tail block
    on the user message.

    Accepts a list of Doc-shaped objects (dataclass with ``role`` +
    ``text`` attributes) or a falsy value. Returns the empty string
    when nothing to surface — call sites can blindly concatenate the
    result, no None-guard needed.

    Format::

        ADDITIONAL CONTEXT (injected after session start, in order received):
        1. <text>
        2. <text>

    The trailing newline-prefix is the responsibility of the caller
    (they use ``"\\n".join([..., block])`` style) — keeping the block
    body free of leading whitespace makes the helper testable in
    isolation.
    """
    if not additional_context:
        return ""
    lines = ["ADDITIONAL CONTEXT (injected after session start, "
             "in order received):"]
    for i, doc in enumerate(additional_context, start=1):
        text = getattr(doc, "text", "") or ""
        lines.append(f"{i}. {text.strip()}")
    return "\n".join(lines)


def build_planner_messages(question: str,
                           *,
                           additional_context=None) -> list[dict]:
    """Planner's conversation seed.

    ``additional_context`` is the v2 ``additional_context`` channel
    filtered to docs targeted at the planner (or ``"any"``). Appended
    as a final user-message block so injected context is visible to
    the planner on its next entry (re-entry case: critic re-route
    triggered another planning round). Empty / None → unchanged
    behavior, preserves v1 byte-for-byte.
    """
    user_text = question.strip()
    extra = _additional_context_block(additional_context)
    if extra:
        user_text = user_text + "\n\n" + extra
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": user_text},
    ]


def build_researcher_messages(question: str, plan: str,
                              prior_rounds: list[str],
                              grounding_msgs: list[dict],
                              *,
                              additional_context=None,
                              peer_findings: Optional[str] = None) -> list[dict]:
    """Researcher's conversation seed.

    Grounding (anchor files + structure map + addendum) goes first, so
    it sits at the start of context regardless of multi-round growth.

    ``additional_context`` (v2 channel, see ``build_planner_messages``)
    appends a final block to the user message so injected docs are
    surfaced on each researcher round entry — covers both the
    fanout-lane case (Send-injected) and the multi-round re-entry
    case (critic asked for more research).

    ``peer_findings`` (M8) is the rendered block from
    :func:`consultants.engine.store.format_findings_block`, surfaced
    just before ``additional_context`` so the researcher sees what
    sibling lanes / prior rounds already discovered before formulating
    its own report. Empty / None -> no block emitted (zero-cost path
    when the store is disabled).
    """
    msgs: list[dict] = list(grounding_msgs)
    msgs.append({"role": "system", "content": RESEARCHER_SYSTEM})
    user_parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLAN FROM PLANNER:\n{plan.strip()}",
    ]
    for i, rep in enumerate(prior_rounds, start=1):
        user_parts.append(
            f"\nYOUR PRIOR REPORT (round {i}):\n{rep.strip()}"
        )
    if prior_rounds:
        user_parts.append(
            "\nThe critic asked for MORE research. Address the gaps "
            "in the next round; do not repeat findings already "
            "covered above."
        )
    if peer_findings and peer_findings.strip():
        user_parts.append("\n" + peer_findings.rstrip())
    extra = _additional_context_block(additional_context)
    if extra:
        user_parts.append("\n" + extra)
    msgs.append({"role": "user", "content": "\n".join(user_parts)})
    return msgs


def build_critic_messages(question: str, plan: str,
                          research_rounds: list[str],
                          *,
                          additional_context=None) -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    extra = _additional_context_block(additional_context)
    if extra:
        parts.append("\n" + extra)
    return [
        {"role": "system", "content": CRITIC_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_synthesizer_messages(question: str, plan: str,
                               research_rounds: list[str],
                               critique: Optional[str],
                               *, self_critic: bool = False,
                               additional_context=None,
                               coder_artifacts: Optional[list] = None) -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    if critique:
        parts.append(f"\nCRITIC'S VERDICT:\n{critique.strip()}")
    # M10: surface coder lane outputs (when present) so the
    # synthesizer can reference the files it wrote in the final
    # answer. Falsy / empty list -> block omitted entirely so the
    # v1 prompt shape is byte-identical when the channel is unused.
    if coder_artifacts:
        try:
            from consultants.engine.coder import build_coder_artifacts_block
            block = build_coder_artifacts_block(coder_artifacts)
            if block:
                parts.append("\n" + block)
        except ImportError:  # pragma: no cover — coder ships in-tree
            pass
    extra = _additional_context_block(additional_context)
    if extra:
        parts.append("\n" + extra)
    parts.append(
        "\nNow write the final answer for the user. Direct, concrete, "
        "cite `path:line` for any code-dependent claim."
    )
    sys_prompt = (SYNTHESIZER_SELF_CRITIC_SYSTEM
                  if self_critic else SYNTHESIZER_SYSTEM)
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ----------------------- plan parser ------------------------------ #
# Extract numbered items from the planner's output so the researcher
# can fan-out one sub-research lane per item via LangGraph's Send API.
# The planner's prompt asks for a numbered list; tolerate variants
# like "1.", "1)", "(1)", and bullets "- ", "* ". An item ends at the
# next item start or end-of-string.

_PLAN_ITEM_RX = re.compile(
    r"^\s*(?:\(?\d+[\.\)]|[-*])\s+(.+?)(?=^\s*(?:\(?\d+[\.\)]|[-*])\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_plan_items(plan_text: str) -> list[str]:
    """Return the numbered/bulleted items in ``plan_text``, in order.

    Each item is one logical step (multi-line allowed). Empty / whitespace
    items are dropped. Strips trailing blank lines.
    """
    if not plan_text:
        return []
    items = [m.group(1).strip() for m in _PLAN_ITEM_RX.finditer(plan_text)]
    return [it for it in items if it]


# Minimum number of plan items below which we don't bother
# fanning out — overhead of separate sub-researcher invocations
# outweighs the parallelism win for a single lane.
FANOUT_MIN_ITEMS = 2

# Maximum number of parallel lanes. The cloud upstream
# (kimi-k2.6:cloud through the 192.168.178.2:11433 proxy)
# serializes concurrent calls to ~2-3 slots, so fan-out beyond 3
# lanes pays redundant per-lane work without true parallelism. The
# 2026-05-07 audit v4 trace measured 6 lanes at 19.7 min cumulative
# LLM time / 2.5x effective parallelism = 9.6 min researcher wall —
# WORSE than the un-fanned 3.1 min baseline. Capped to 3 lanes,
# items beyond the first are folded back into earlier lanes.
FANOUT_MAX_LANES = 3


def group_items_into_lanes(items: list[str], max_lanes: int) -> list[list[str]]:
    """Distribute ``items`` across at most ``max_lanes`` lanes.

    Round-robin chunking — earlier lanes get the larger group when
    the count doesn't divide evenly. Returns a list of sub-lists,
    each non-empty. With ``len(items) <= max_lanes`` returns one
    item per lane.
    """
    if not items:
        return []
    n = min(len(items), max(1, max_lanes))
    # Ceiling division so earlier lanes are at most one item heavier.
    base = len(items) // n
    extra = len(items) % n
    lanes: list[list[str]] = []
    pos = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        lanes.append(items[pos:pos + size])
        pos += size
    return [g for g in lanes if g]


def join_lane_items(lane_items: list[str]) -> str:
    """Render a lane's grouped plan items back into a numbered list
    that the researcher can execute as a focused sub-plan.
    """
    return "\n".join(f"{i + 1}. {it}" for i, it in enumerate(lane_items))


# ----------------------- critic decision parser ------------------- #

_DECISION_RX = re.compile(
    r"^\s*DECISION\s*:\s*(ready|needs_more_research)\b",
    re.IGNORECASE | re.MULTILINE,
)


def parse_critic_decision(text: str) -> str:
    """Extract the critic's decision token. Falls back to 'ready' so
    a malformed critic reply doesn't strand the council."""
    m = _DECISION_RX.search(text or "")
    if not m:
        log.warning(
            "critic produced no parseable DECISION line; defaulting to ready",
        )
        return "ready"
    return m.group(1).lower()


# ----------------------- routing ---------------------------------- #

ROUTE_SYNTHESIZER = "synthesizer"
ROUTE_RESEARCHER = "researcher"


def route_after_critic(state: dict) -> str:
    """Decide whether to loop back to researcher or fall through to
    synthesizer. Pure function of the state — LangGraph's conditional
    edge calls this.

    v2 (2026-05-16): reads ``runtime_control.max_rounds`` and
    ``max_reroutes`` when present, falling back to the v1 effort
    caps when absent. This makes a mid-flight ``graph.update_state(
    {"runtime_control": {"max_rounds": 5}})`` immediately tighten or
    loosen the loop ceiling. Also short-circuits to synthesizer if
    the runtime deadline has passed — the synthesizer composes from
    whatever evidence is ready rather than burning more time.
    """
    decision = state.get("critic_decision") or "ready"
    if decision == "ready":
        return ROUTE_SYNTHESIZER
    # Lazy import — keeps council.py importable without state_v2's
    # deps if a caller has a stripped-down env. control imports only
    # config + stdlib.
    from consultants.engine.control import (
        runtime_max_rounds, runtime_max_reroutes, runtime_deadline_passed,
    )
    rounds_used = int(state.get("research_rounds_used") or 1)
    reroutes_used = int(state.get("critic_reroutes_used") or 0)
    effort = str(state.get("effort") or "medium")
    caps = caps_for(effort)
    if runtime_deadline_passed(state):
        log.info(
            "route_after_critic: runtime deadline passed; -> synthesizer",
        )
        return ROUTE_SYNTHESIZER
    max_reroutes = runtime_max_reroutes(
        state, fallback=caps.critic_reroutes_max,
    )
    max_rounds = runtime_max_rounds(
        state, fallback=caps.researcher_rounds_max,
    )
    if reroutes_used >= max_reroutes:
        log.info(
            "route_after_critic: reroute cap hit (%d >= %d); -> synthesizer",
            reroutes_used, max_reroutes,
        )
        return ROUTE_SYNTHESIZER
    if rounds_used >= max_rounds:
        log.info(
            "route_after_critic: researcher round cap hit (%d >= %d); "
            "-> synthesizer",
            rounds_used, max_rounds,
        )
        return ROUTE_SYNTHESIZER
    return ROUTE_RESEARCHER


# ----------------------- chat helper ------------------------------ #

def _extract_text(response: dict) -> str:
    """Pull the assistant text out of a chat-completion response.
    Also tolerates the Ollama-native shape in case a caller passes
    an un-translated response."""
    if "choices" in response:
        choices = response.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return (msg.get("content") or "").strip()
    msg = response.get("message") or {}
    return (msg.get("content") or "").strip()


def _usage_from(response: dict) -> tuple[int, int]:
    u = response.get("usage") or {}
    return (
        int(u.get("prompt_tokens") or u.get("prompt_eval_count") or 0),
        int(u.get("completion_tokens") or u.get("eval_count") or 0),
    )


def _single_shot(chat_client, model: str, messages: list[dict],
                 *, think: Any = True,
                 recorder=None, role: Optional[str] = None,
                 round: int = 1,
                 lane_idx: Optional[int] = None) -> tuple[str, int, int]:
    """Run a one-call chat (no tools, no loop). Returns
    (text, prompt_tokens, completion_tokens).

    When ``recorder`` is provided, the call (success or failure) is
    appended to the recorder's ``events`` table as one ``llm_call``
    row. ``role`` is required for recording — pass it from the
    caller's node identity. Errors are re-raised after recording so
    the existing tombstone branches still fire.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
    }
    t0 = time.monotonic()
    try:
        response = chat_client.chat(payload)
    except Exception as exc:
        if recorder is not None and role is not None:
            try:
                recorder.record_llm(
                    role=role, round=round, lane_idx=lane_idx, model=model,
                    request=payload, response=None,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # pragma: no cover — recorder must not mask
                log.exception("recorder.record_llm raised; ignored")
        raise
    dt_ms = int((time.monotonic() - t0) * 1000)
    pt, ct = _usage_from(response)
    if recorder is not None and role is not None:
        try:
            recorder.record_llm(
                role=role, round=round, lane_idx=lane_idx, model=model,
                request=payload, response=response,
                prompt_tokens=pt, completion_tokens=ct,
                duration_ms=dt_ms,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_llm raised; ignored")
    return (_extract_text(response), pt, ct)


def _compose_degraded_answer(state: dict, *, error: str) -> str:
    """Build a fallback ``final_answer`` from researcher + critic work
    when the synthesizer fails after exhausting its retry budget.

    The researcher reports and critic verdict are the most expensive
    work in a consultation (often 2-3 minutes each at xhigh effort)
    and they live in state by the time the synthesizer runs. When
    the cloud flaps a 500 on the synthesizer alone, ditching all
    that work and returning ``(consultation incomplete)`` is much
    worse than handing the user the raw findings with a clear
    "synthesizer failed, this is not a synthesis" banner.

    Result shape (markdown):

        # Degraded answer (synthesizer failed)
        > {error string, indented}
        > The findings below are the researcher's raw reports and
        > the critic's verdict.

        ## Researcher findings
        ### Round 1
        <full content>
        ### Round 2
        ...

        ## Critic verdict
        <full content>

        ## How to recover
        <how-to-resume hint pointing at follow-up>

    Returns the placeholder error string only when neither research
    nor critic content survived (rare — would mean the failure
    happened before researcher even produced output). The caller
    still treats the consultation as ``status=failed``.
    """
    research = [
        r for r in (state.get("research") or [])
        if isinstance(r, str) and r.strip()
    ]
    critique = (state.get("critique") or "").strip()
    if not research and not critique:
        return f"(consultation incomplete: synthesizer error: {error})"

    parts: list[str] = []
    parts.append("# Degraded answer (synthesizer failed)")
    parts.append("")
    parts.append("> **synthesizer error:**")
    for line in error.splitlines() or [error]:
        parts.append(f"> {line}")
    parts.append(">")
    parts.append("> The synthesizer could not compose a final answer "
                 "after exhausting its retry budget. The findings "
                 "below are the researcher's raw reports and the "
                 "critic's verdict — uncombined and unsmoothed. "
                 "See **How to recover** at the bottom for the "
                 "cheapest way to get a real synthesis.")
    parts.append("")
    if research:
        parts.append("## Researcher findings")
        if len(research) == 1:
            parts.append("")
            parts.append(research[0].strip())
        else:
            for i, rep in enumerate(research, start=1):
                parts.append("")
                parts.append(f"### Round {i}")
                parts.append("")
                parts.append(rep.strip())
        parts.append("")
    if critique:
        parts.append("## Critic verdict")
        parts.append("")
        parts.append(critique)
        parts.append("")
    parts.append("## How to recover")
    parts.append("")
    parts.append(
        "Follow up against THIS session's sid (not the original "
        "parent) to inherit researcher + critic threads — the "
        "synthesizer can then compose with the work above already "
        "warm. Cost is one synthesizer call, not a full re-run."
    )
    parts.append("")
    parts.append(
        "    claude-consultants follow-up <THIS_SID> "
        "--message \"compose a final answer from the prior research and critic\""
    )
    return "\n".join(parts)


# ----------------------- node implementations -------------------- #
# Each node mutates only its own slice of state. The LangGraph
# Reducer pattern would let us return partial updates that are
# merged into state; we follow the same convention here so graph.py
# can plug these in directly.

def planner_node(state: dict, *, chat_client, model: str,
                 think: Any = True, recorder=None,
                 coder_enabled: bool = False) -> dict:
    t0 = time.monotonic()
    _emit_started("planner", round=1, model=model)
    if recorder is not None:
        try:
            recorder.record_node(role="planner", kind="node_enter")
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    # M5: surface injected ``additional_context`` docs targeted at the
    # planner. Returns [] if no v2 channel on state or no matching
    # docs; the builder appends a final block to the user message
    # only when non-empty so v1 prompt shape is byte-identical when
    # the channel is absent.
    extra_ctx_planner = _additional_context_for(state, "planner")
    msgs = build_planner_messages(
        state["question"], additional_context=extra_ctx_planner,
    )
    # M10: when the coder role is enabled, append the PLANNER_CODER_GATE_BLOCK
    # to the planner's system message so the model knows it can
    # opt into code-generation tasks. The block is intentionally
    # surgical (single fenced JSON appendix) so a planner that
    # decides code isn't needed emits exactly the v1 plan shape.
    if coder_enabled:
        try:
            from consultants.engine.coder import PLANNER_CODER_GATE_BLOCK
        except ImportError:  # pragma: no cover — coder ships in-tree
            PLANNER_CODER_GATE_BLOCK = ""
        if PLANNER_CODER_GATE_BLOCK:
            # The first message is the system message; append, don't
            # replace, so the v1 planner instructions still anchor
            # the conversation.
            sys_msg = msgs[0]
            msgs[0] = {
                "role": sys_msg.get("role", "system"),
                "content": (sys_msg.get("content") or "")
                            + PLANNER_CODER_GATE_BLOCK,
            }
    try:
        plan, pt, ct = _single_shot(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="planner", round=1,
        )
    except Exception as e:
        log.exception("planner_node failed: %s", e)
        _emit_finished(
            "planner", round=1,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        # Tombstone return: keep the graph progressing with a
        # visible failure marker. ``plan`` is non-additive so the
        # err_text replaces the empty initial value, which the
        # researcher will see as its plan; ``plan_items`` stays
        # empty so the fan-out router falls through to the
        # single-researcher path; ``turns`` (additive reducer)
        # records the failure so the transcript shows it.
        err_text = f"(planner failed: {e})"
        return {
            "error": f"planner failed: {e}",
            "_role_failed": "planner",
            "plan": err_text,
            "plan_items": [],
            "turns": [RoleTurn(
                role="planner", round=1, content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    turn = RoleTurn(
        role="planner", round=1, content=plan,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    plan_items = parse_plan_items(plan)
    if recorder is not None:
        try:
            recorder.record_node(
                role="planner", kind="node_exit",
                duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("planner", round=1,
                    duration_ms=int(dt * 1000), ok=True)
    # M10: when the coder gate was offered, parse the planner's
    # opt-in declaration. Three outcomes:
    # - explicit ``true`` + non-empty tasks -> graph fans out to coder
    # - explicit ``false`` or missing tasks -> bypass coder cleanly
    # - parse failure on a malformed block -> bypass + log (the
    #   planner output stays usable for the rest of the graph)
    coder_delta: dict = {}
    if coder_enabled:
        try:
            from consultants.engine.coder import (
                parse_coder_preamble, parse_coder_tasks,
            )
            wants_code = parse_coder_preamble(plan)
            if wants_code:
                tasks = parse_coder_tasks(plan, parent_round=1)
                if tasks:
                    coder_delta = {
                        "requires_code_generation": True,
                        "coder_tasks": tasks,
                    }
                else:
                    # Asserted code generation but emitted no tasks —
                    # the planner is confused; treat as a non-coder
                    # plan so the graph stays predictable.
                    log.info(
                        "planner asserted requires_code_generation=true "
                        "but emitted no coder_tasks; treating as no-op",
                    )
                    coder_delta = {"requires_code_generation": False}
            elif wants_code is False:
                coder_delta = {"requires_code_generation": False}
            # wants_code is None -> emit nothing; downstream readers
            # default to no-coder behavior on missing channel.
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "planner coder-gate parse raised; skipping coder route",
            )
    # Delta-only return — additive reducers in CouncilState merge
    # ``turns``, ``total_*_tokens``, ``research_rounds_used`` across
    # parallel fan-out lanes.
    return {
        "plan": plan,
        "plan_items": plan_items,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
        **coder_delta,
    }


def researcher_node(state: dict, *,
                    chat_client,
                    tool_executor,
                    tool_specs: list[dict],
                    grounding_msgs: list[dict],
                    model: str,
                    cwd: str,
                    think: Any = True,
                    loop_runner=None,
                    recorder=None,
                    prior_messages: Optional[list[dict]] = None,
                    tool_executor_enabled: bool = False,
                    store: Any = None,
                    sid: Optional[str] = None) -> dict:
    """Researcher uses agent_loop.runner.run_loop for a tool sub-loop.

    ``loop_runner`` defaults to ``claude_hooks.agent_loop.runner.run_loop``
    but is injectable for tests. We import lazily to keep the module
    importable in environments where claude_hooks isn't on the path
    (although in practice it always is — this is just defensive).

    M6: when ``tool_executor_enabled`` is True (set by the graph
    wrapper when the tool_executor role is in deps.enabled_roles),
    the researcher operates in a two-phase mode:

    - **PLAN mode** (first entry of a research cycle): emit a
      JSON ``tool_plan`` block instead of running tools inline.
      Returns ``{"tool_plan": [items], "awaiting_tool_results":
      True}``; the graph fans the items out to tool_executor
      Send lanes.
    - **REPORT mode** (re-entry after lanes complete): consume
      ``state.tool_results`` filtered to the current round, weave
      them into the standard research report, clear the awaiting
      flag. No inline tool loop in either mode — the executor
      lanes are the only tool callers.

    ``tool_executor_enabled=False`` (default) preserves v1
    behavior bit-for-bit: full inline agent_loop subloop.
    """
    if loop_runner is None:
        from claude_hooks.agent_loop.runner import run_loop  # lazy
        loop_runner = run_loop
    # Re-import only the LoopConfig surface we need; tests pass a
    # stub loop_runner so this import is skipped on that path.
    try:
        from claude_hooks.agent_loop.runner import LoopConfig
    except Exception:  # pragma: no cover — only if claude_hooks missing
        LoopConfig = None  # type: ignore[assignment]

    # Single timestamp covers both the M6 PLAN/REPORT branch and
    # the v1 inline-agent-loop branch — each branch's exit emits
    # its own duration delta from this anchor.
    t0 = time.monotonic()
    rounds_used = int(state.get("research_rounds_used") or 0)
    this_round = rounds_used + 1
    prior_rounds: list[str] = list(state.get("research") or [])
    lane_idx = state.get("lane_idx")

    # M8: closure that writes a successful research report to the
    # shared store. Captures store + sid + lane + plan_item so each
    # of the three "successful report" return sites (v1 inline,
    # M6 REPORT, M6 PLAN-mode fallback) calls the same single
    # function. No-op when store is None / sid missing / text empty.
    def _record_finding_to_store(report_text: str) -> None:
        if store is None or not sid:
            return
        try:
            from consultants.engine.store import record_research
            record_research(
                store, sid,
                lane_idx=lane_idx,
                plan_item=state.get("plan_item"),
                finding=report_text,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "record_research raised in researcher lane "
                "%s; lane continues",
                lane_idx,
            )
    # Phase 9: per-lane multi-model fan-out. The dispatcher sets
    # ``state["model_override"]`` on each Send so different lanes
    # talk to different Ollama models. Falls back to the role's
    # configured primary when absent (single-model fan-out, the
    # base-tier path).
    model_override = state.get("model_override")
    if isinstance(model_override, str) and model_override.strip():
        model = model_override.strip()
    _emit_started("researcher", round=this_round,
                   lane_idx=lane_idx, model=model)
    if recorder is not None:
        try:
            recorder.record_node(
                role="researcher", kind="node_enter",
                round=this_round, lane_idx=lane_idx,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")

    # Phase 5 (v1.1): when the parent's researcher message thread is
    # available (set by the follow-up runner from
    # parent_state._role_messages), extend it with the follow-up
    # question instead of rebuilding from scratch. The prior thread
    # already contains grounding + system + the original user task +
    # the parent researcher's assistant turns + tool results; the
    # run_loop's next iter sees the conversation as a natural
    # continuation and can reuse prior tool results without
    # re-fetching them. ``state["question"]`` carries the focused
    # follow-up question (the runner sets it).
    #
    # Falls back to today's build_researcher_messages path when
    # prior_messages is None (warm-parent-without-recorder, v1.0
    # artifacts, or first turn of a fresh consultation).
    if prior_messages is not None:
        follow_up_q = (state.get("question") or "").strip() or "Continue."
        msgs = list(prior_messages) + [{
            "role": "user",
            "content": (
                "FOLLOW-UP QUESTION:\n" + follow_up_q + "\n\n"
                "You may reuse the tool results above; do not re-fetch "
                "files you've already read unless they have changed. "
                "If your prior findings already cover this question, "
                "summarize them; otherwise extend research as needed."
            ),
        }]
    else:
        # Fan-out path: when invoked via Send with a ``plan_item``,
        # work only on that sub-question. The single-researcher path
        # (no plan_item) — used for critic re-routes and unfanned
        # topologies — uses the full plan + prior research rounds
        # for context.
        # M5: surface injected docs targeted at the researcher. Same
        # filter logic for both lane-focused and full-plan paths — a
        # mid-flight inject should reach every researcher lane the
        # next time it enters.
        extra_ctx_res = _additional_context_for(state, "researcher")
        plan_item = state.get("plan_item")
        # M8: shared-store recall — ask the store for findings that
        # other lanes (this session) or prior consultations (when
        # cross-session namespaces are wired) already produced for
        # this plan item. Cheap protection against duplicate work,
        # no-op when store is disabled. Query prefers the focused
        # plan_item (high signal) and falls back to the full plan
        # for the single-researcher path. Recall failures are
        # swallowed inside the helper — they never break the lane.
        peer_findings_block = None
        if store is not None and sid:
            from consultants.engine.store import (
                format_findings_block,
                recall_research,
            )
            recall_query = (
                plan_item
                if plan_item
                else (state.get("plan") or state.get("question") or "")
            )
            hits = recall_research(
                store, sid, recall_query, limit=5,
            )
            # Optional dedup: don't surface this lane's own prior
            # findings (they're already in ``prior_rounds`` above).
            my_lane = state.get("lane_idx")
            if my_lane is not None:
                hits = [
                    h for h in hits
                    if (getattr(h, "value", None) or {}).get(
                        "lane_idx") != my_lane
                ]
            peer_findings_block = (
                format_findings_block(hits) if hits else None
            )
        if plan_item:
            focused_plan = (
                f"Sub-research lane {state.get('lane_idx', 0) + 1}: "
                f"{plan_item}"
            )
            msgs = build_researcher_messages(
                state["question"], focused_plan, [], grounding_msgs,
                additional_context=extra_ctx_res,
                peer_findings=peer_findings_block,
            )
        else:
            msgs = build_researcher_messages(
                state["question"], state["plan"], prior_rounds,
                grounding_msgs,
                additional_context=extra_ctx_res,
                peer_findings=peer_findings_block,
            )
    # ---------- M6: tool_executor branch ----------------------- #
    # When the tool_executor role is in the enabled set, the
    # researcher does NOT run an inline tool subloop. Instead it
    # alternates between two single-shot LLM calls:
    #
    #   PLAN MODE  — first entry of this research cycle.
    #     Append the PLAN_MODE_BLOCK to the user message, call
    #     chat once (no agent_loop, no tools_available), parse
    #     the JSON ``tool_plan`` from the response, return
    #     ``{"tool_plan": [items]}`` with each item stamped with
    #     this lane's ``parent_lane_idx`` (#103). The graph fans
    #     out one tool_executor Send per item.
    #
    #   REPORT MODE — re-entry after lanes complete.
    #     Append the PRIOR TOOL RESULTS block (rendered from
    #     ``tool_results_for_round(state, this_round,
    #     parent_lane_idx=lane_idx)``) to the user message, call
    #     chat once, return the v1 shape ``{"research": [text],
    #     "research_rounds_used": 1}``.
    #
    # The mode is decided by whether tool_results exist for
    # ``(this_round, parent_lane_idx=lane_idx)``: if any do, the
    # lanes already ran for this round and this researcher lane
    # and we're consuming their output (REPORT); otherwise we're
    # emitting the plan (PLAN). The graph re-enters each lane in
    # REPORT mode via the post-tool_executor fanback conditional
    # edge (#103); LangGraph's state-merge ensures the new
    # tool_results are visible here.
    #
    # #103 dropped the M6-era ``awaiting_tool_results: bool``
    # scalar flag from the state schema — under x-tier multi-
    # model researcher fanout, last-writer-wins on a scalar
    # produced ambiguous routing. The router now derives the
    # dispatch decision from ``tool_plan`` vs ``tool_results``
    # directly (see ``_route_after_researcher`` in ``graph.py``).
    if tool_executor_enabled:
        from consultants.engine.state_v2 import (
            tool_results_for_round,
        )
        from consultants.engine.tool_executor import (
            RESEARCHER_PLAN_MODE_BLOCK,
            build_tool_plan_user_appendix,
            parse_tool_plan,
        )

        # #103 proper composition: pass this lane's identity as
        # ``parent_lane_idx`` so REPORT mode reads only the
        # ToolResults the dispatcher emitted for our own plan.
        # ``lane_idx=None`` (single-researcher non-fanout path)
        # is forwarded as-is — the helper's None branch returns
        # all matching-round results, preserving the M6 contract.
        prior_for_round = tool_results_for_round(
            state, this_round, parent_lane_idx=lane_idx,
        )
        report_mode = bool(prior_for_round)
        # Append the mode-specific appendix to the user message.
        # Both append to msgs[-1] (the v1 user message) so the
        # researcher's existing context (plan, prior rounds,
        # additional_context) stays intact.
        appendix = (
            build_tool_plan_user_appendix(prior_for_round)
            if report_mode else RESEARCHER_PLAN_MODE_BLOCK
        )
        msgs = list(msgs)
        msgs[-1] = dict(msgs[-1])
        msgs[-1]["content"] = msgs[-1]["content"] + (
            "\n\n" + appendix if appendix and report_mode else appendix
        )

        try:
            text, pt, ct = _single_shot(
                chat_client, model, msgs, think=think,
                recorder=recorder, role="researcher",
                round=this_round, lane_idx=lane_idx,
            )
        except Exception as e:
            log.exception("researcher_node (M6 mode) failed: %s", e)
            dt_ms = int((time.monotonic() - t0) * 1000)
            _emit_finished(
                "researcher", round=this_round, lane_idx=lane_idx,
                duration_ms=dt_ms, ok=False,
                error=f"{type(e).__name__}: {e}",
            )
            tomb_text = f"(researcher lane failed: {e})"
            # #103: the legacy ``awaiting_tool_results=False`` key
            # is dropped — the router now derives the dispatch
            # decision from tool_plan vs tool_results directly,
            # making the scalar flag redundant.
            return {
                "error": f"researcher failed: {e}",
                "_role_failed": "researcher",
                "research": [tomb_text] if not report_mode else [],
                "turns": [RoleTurn(
                    role="researcher", round=this_round,
                    content=tomb_text,
                    prompt_tokens=0, completion_tokens=0,
                    duration_seconds=0.0,
                )],
            }
        dt = time.monotonic() - t0
        if report_mode:
            # Tools already ran — write the report as the v1 shape.
            turn = RoleTurn(
                role="researcher", round=this_round, content=text,
                prompt_tokens=pt, completion_tokens=ct,
                duration_seconds=dt,
            )
            if recorder is not None:
                try:
                    recorder.record_node(
                        role="researcher", kind="node_exit",
                        round=this_round, lane_idx=lane_idx,
                        duration_ms=int(dt * 1000),
                    )
                except Exception:  # pragma: no cover
                    log.exception("recorder.record_node raised")
            _emit_finished(
                "researcher", round=this_round, lane_idx=lane_idx,
                duration_ms=int(dt * 1000), ok=True,
            )
            _record_finding_to_store(text)
            return {
                "research": [text],
                "research_rounds_used": 1,
                "turns": [turn],
                "total_prompt_tokens": pt,
                "total_completion_tokens": ct,
            }
        # PLAN MODE — parse the tool_plan JSON. An empty parse
        # is a soft failure: the dispatcher's conditional edge
        # falls through to the standard continuation rather than
        # looping forever on an empty Send list.
        # #103: stamp each emitted item with this researcher
        # lane's identity so tool_executor → researcher fanback
        # can route ToolResults back to the originating lane.
        items = parse_tool_plan(
            text,
            parent_round=this_round,
            parent_lane_idx=lane_idx,
        )
        turn = RoleTurn(
            role="researcher", round=this_round, content=text,
            prompt_tokens=pt, completion_tokens=ct,
            duration_seconds=dt,
        )
        if recorder is not None:
            try:
                recorder.record_node(
                    role="researcher", kind="node_exit",
                    round=this_round, lane_idx=lane_idx,
                    duration_ms=int(dt * 1000),
                )
            except Exception:  # pragma: no cover
                log.exception("recorder.record_node raised")
        _emit_finished(
            "researcher", round=this_round, lane_idx=lane_idx,
            duration_ms=int(dt * 1000), ok=True,
        )
        if not items:
            # Empty plan — degrade to the v1 inline-report shape:
            # treat the raw researcher text as the research report
            # so the council still produces an answer. The graph's
            # router falls through automatically because no plan
            # items survive the "current-round + unconsumed" filter
            # (#103 drops the scalar awaiting_tool_results flag in
            # favour of deriving the dispatch decision from
            # tool_plan vs tool_results directly).
            log.warning(
                "researcher M6 PLAN-mode returned empty/unparseable "
                "tool_plan; falling back to inline research from raw text"
            )
            _record_finding_to_store(text)
            return {
                "research": [text],
                "research_rounds_used": 1,
                "turns": [turn],
                "total_prompt_tokens": pt,
                "total_completion_tokens": ct,
            }
        # #103: ``awaiting_tool_results`` is no longer written —
        # the router infers PLAN-mode-just-completed from the
        # presence of current-round tool_plan items that lack
        # matching tool_results.
        return {
            "tool_plan": items,
            "turns": [turn],
            "total_prompt_tokens": pt,
            "total_completion_tokens": ct,
        }
    # ---------- end M6 branch ------------------------------------ #

    caps = caps_for(state.get("effort") or "medium")

    payload = {
        "model": model,
        "messages": msgs,
        "stream": False,
    }

    cfg = None
    if LoopConfig is not None:
        cfg = LoopConfig(
            max_iterations=caps.researcher_loop_iters,
            force_answer_after=caps.researcher_force_answer_after,
            tools_available=True,
            think=think,
            force_first_tool_call=False,
        )

    # Bind recorder callbacks to the loop_runner so per-iteration LLM
    # calls AND tool executions land in the events table. Both
    # callbacks are no-ops when recorder is None (legacy / test path).
    on_iter_cb = None
    on_tool_cb = None
    if recorder is not None:
        def _on_iter(_idx: int, req: dict, resp: dict, dt_ms: int) -> None:
            pt_l, ct_l = _usage_from(resp)
            recorder.record_llm(
                role="researcher", round=this_round, lane_idx=lane_idx,
                model=model, request=req, response=resp,
                prompt_tokens=pt_l, completion_tokens=ct_l,
                duration_ms=dt_ms,
            )

        def _on_tool(name: str, args: str, output: str,
                     dt_ms: int, err: Optional[str]) -> None:
            recorder.record_tool(
                role="researcher", round=this_round, lane_idx=lane_idx,
                tool=name, args=args, output=output,
                duration_ms=dt_ms, error=err,
            )
        on_iter_cb = _on_iter
        on_tool_cb = _on_tool

    # M3 (v2): when state["runtime_control"] is wired through, route
    # each iteration's chat call through a stall monitor. The
    # researcher is the first role to consume this — the cloud
    # gemini-3-flash stall pathology in csl-2026-05-15-1439-4ff0
    # showed up on a researcher lane. Critic / synthesizer can
    # follow in a later milestone if their failure mode shows up
    # in the wild.
    #
    # When runtime_control isn't on state (v1 legacy path), we pass
    # ``chat_client.chat`` unwrapped — preserves v1 behavior bit-for-bit.
    chat_fn = chat_client.chat
    rc = state.get("runtime_control") or {}
    if rc and hasattr(chat_client, "chat_streamed"):
        try:
            # Lazy import — keeps council.py importable in envs
            # where the consultants package partial-installs (the
            # main claude-hooks test env runs without langgraph,
            # but stall_chat is pure-Python and imports cleanly).
            from consultants.engine.stall_chat import (
                stall_protected_chat_fn_for,
            )
            stall_event_sink = None
            if recorder is not None and hasattr(
                    recorder, "record_event"):
                def stall_event_sink(  # noqa: E306
                        ev: dict, _round=this_round,
                        _lane=lane_idx) -> None:
                    try:
                        recorder.record_event(
                            kind=ev.get("kind") or "stall.event",
                            role="researcher", round=_round,
                            lane_idx=_lane, payload=ev,
                        )
                    except Exception:  # pragma: no cover
                        log.exception(
                            "recorder.record_event raised; ignored")
            chat_fn = stall_protected_chat_fn_for(
                chat_client,
                stall_threshold_s=float(
                    rc.get("stall_threshold_s") or 300.0),
                hard_cap_s=float(
                    rc.get("per_lane_hard_s") or 3600.0),
                retries=int(rc.get("stall_retries") or 1),
                on_event=stall_event_sink,
            )
        except Exception:  # pragma: no cover
            # Never break the researcher because of stall-wrapper
            # plumbing — fall back to the unwrapped chat fn.
            log.exception(
                "researcher: stall_protected_chat_fn_for failed; "
                "falling back to chat_client.chat unwrapped"
            )
            chat_fn = chat_client.chat

    # Re-anchor t0 just before the inline agent_loop so its duration
    # is measured from when the loop actually starts (not from the
    # node entry — M6 branch hoists the anchor earlier for its own
    # exit paths, so legacy timing semantics stay intact here).
    t0 = time.monotonic()
    try:
        # Tests pass a stub loop_runner; inspect its signature so we
        # only forward the new callbacks when the runner accepts them.
        # Default `claude_hooks.agent_loop.runner.run_loop` does.
        loop_kwargs = dict(
            config=cfg,
            tool_specs=tool_specs,
            chat_fn=chat_fn,
            tool_executor=tool_executor,
        )
        if on_iter_cb is not None or on_tool_cb is not None:
            try:
                import inspect
                sig = inspect.signature(loop_runner)
                if "on_iter" in sig.parameters:
                    loop_kwargs["on_iter"] = on_iter_cb
                if "on_tool" in sig.parameters:
                    loop_kwargs["on_tool"] = on_tool_cb
            except (TypeError, ValueError):
                pass
        final = loop_runner(payload, cwd, **loop_kwargs)
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        _emit_finished(
            "researcher", round=this_round, lane_idx=lane_idx,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        # Tombstone return so the additive reducers record the
        # failure visibly. Without this, a crashed lane silently
        # contributes nothing — the synthesizer never sees the gap
        # and writes a confidently-degraded answer over the surviving
        # lanes. This was the audit-high (csl-...-faa2) finding.
        err_text = f"(researcher lane failed: {e})"
        return {
            "error": f"researcher failed: {e}",
            "_role_failed": "researcher",
            "research": [err_text],
            "research_rounds_used": 1,
            "turns": [RoleTurn(
                role="researcher", round=this_round, content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    text = _extract_text(final)
    pt, ct = _usage_from(final)

    # Defensive fallback: if the loop hit max_iterations while the
    # model was still emitting tool_calls (or hallucinated tool calls
    # with empty content after tools were stripped), the final
    # response has no textual report. We saw this in the 2026-05-07
    # audit run (all 6 fan-out lanes returned ""). Force one more
    # tool-stripped call asking for an explicit summary so the
    # synthesizer always sees a real report.
    if not text.strip():
        log.warning(
            "researcher: empty final text after %d iters — forcing "
            "summary call (lane=%s)",
            (cfg.max_iterations if cfg else -1),
            state.get("lane_idx"),
        )
        # CRITICAL: use the run_loop's final conversation transcript
        # (with tool calls + results) rather than the original
        # pre-loop messages. The latter contains zero evidence; the
        # model would correctly answer "I have no findings". Caught
        # in the 2026-05-07 audit v3 trace where the lanes had run
        # 27 read_file + 29 grep calls but the fallback summary
        # said "no useful evidence" because it didn't see the tool
        # results. ``_loop_messages`` is set by run_loop on every
        # return.
        loop_msgs = final.get("_loop_messages") or payload["messages"]
        summary_msgs = list(loop_msgs) + [{
            "role": "user",
            "content": (
                "Write your findings now as a focused report based on "
                "the tool outputs above. One bullet per finding with "
                "`path:line` reference. Do NOT call tools. If the "
                "tool outputs above are genuinely empty (no matches, "
                "no readable files), say so explicitly — never return "
                "empty."
            ),
        }]
        try:
            fb_t0 = time.monotonic()
            fb_payload = {
                "model": model,
                "messages": summary_msgs,
                "stream": False,
                "think": think,
            }
            follow_up = chat_client.chat(fb_payload)
            text2 = _extract_text(follow_up)
            pt2, ct2 = _usage_from(follow_up)
            if recorder is not None:
                try:
                    recorder.record_llm(
                        role="researcher", round=this_round,
                        lane_idx=lane_idx, model=model,
                        request=fb_payload, response=follow_up,
                        prompt_tokens=pt2, completion_tokens=ct2,
                        duration_ms=int((time.monotonic() - fb_t0) * 1000),
                    )
                except Exception:  # pragma: no cover
                    log.exception("recorder.record_llm (fallback) raised; ignored")
            pt += pt2
            ct += ct2
            if text2.strip():
                text = text2
            else:
                # Both attempts empty — emit a stub so the synthesizer
                # at least sees that this lane found nothing concrete.
                text = (
                    "(researcher lane produced no findings within the "
                    "iteration budget; gaps remain — see plan)"
                )
        except Exception as e:  # pragma: no cover — defensive
            log.exception(
                "researcher summary fallback failed: %s; emitting stub", e,
            )
            text = (
                "(researcher lane failed to produce a written report)"
            )

    # Walk the run_loop's transcript-equivalent to summarize tool usage.
    # run_loop returns only the final response, so we don't have the
    # full tool trace here — that's fine for the summary; the full
    # transcript can be added in v1.1 by piping run_loop's seen_calls
    # back out. For now, RoleTurn just records the text + tokens.
    turn = RoleTurn(
        role="researcher", round=this_round, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    # ``research`` uses an additive reducer in CouncilState so
    # parallel sub-researchers fanned out via Send merge their
    # findings cleanly. Return the DELTA only (this report's text)
    # — the reducer concatenates with prior entries.
    #
    # ``research_rounds_used`` and ``total_*_tokens`` similarly use
    # additive reducers so concurrent fan-out lanes' counts merge.
    # ``turns`` likewise.
    if recorder is not None:
        try:
            recorder.record_node(
                role="researcher", kind="node_exit",
                round=this_round, lane_idx=lane_idx,
                duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("researcher", round=this_round, lane_idx=lane_idx,
                    duration_ms=int(dt * 1000), ok=True)
    _record_finding_to_store(text)
    return {
        "research": [text],
        "research_rounds_used": 1,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def critic_node(state: dict, *, chat_client, model: str,
                think: Any = True, recorder=None) -> dict:
    rounds_used_pre = int(state.get("research_rounds_used") or 0)
    # Phase 10: per-lane multi-model fan-out for critics. Same shape
    # as researcher's model_override path. lane_idx propagates from
    # the dispatcher (=critic_idx) so the recorder can map each row
    # back to its critic instance for audit.
    model_override = state.get("model_override")
    if isinstance(model_override, str) and model_override.strip():
        model = model_override.strip()
    lane_idx = state.get("lane_idx")
    _emit_started("critic", round=max(rounds_used_pre, 1),
                   lane_idx=lane_idx, model=model)
    if recorder is not None:
        try:
            recorder.record_node(
                role="critic", kind="node_enter",
                round=max(rounds_used_pre, 1), lane_idx=lane_idx,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    # M5: surface docs targeted at the critic (rare in practice;
    # mostly "any" docs naming a quality bar like "must cite path:line
    # for every claim"). Same defensive helper as the other roles.
    extra_ctx_critic = _additional_context_for(state, "critic")
    msgs = build_critic_messages(
        state["question"], state["plan"], state.get("research") or [],
        additional_context=extra_ctx_critic,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="critic",
            round=max(rounds_used_pre, 1),
            lane_idx=lane_idx,
        )
    except Exception as e:
        log.exception("critic_node failed: %s", e)
        _emit_finished(
            "critic", round=max(rounds_used_pre, 1), lane_idx=lane_idx,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        # On critic failure default to "ready" so the council still
        # produces an answer rather than stalling forever. Tombstone
        # the critique so the synthesizer sees the failure note and
        # the transcript records it.
        err_text = f"(critic failed: {e}; defaulting to ready)"
        rounds_used = int(state.get("research_rounds_used") or 0)
        return {
            "error": f"critic failed: {e}",
            "_role_failed": "critic",
            "critic_decision": "ready",
            "critique": err_text,
            "turns": [RoleTurn(
                role="critic", round=max(rounds_used, 1),
                content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    decision = parse_critic_decision(text)
    # critic_reroutes_used uses the additive reducer; we contribute
    # +1 for a re-route or 0 for ready, and the merge concatenates.
    rerouted_delta = 1 if decision == "needs_more_research" else 0
    rounds_used = int(state.get("research_rounds_used") or 0)
    turn = RoleTurn(
        role="critic", round=max(rounds_used, 1), content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    if recorder is not None:
        try:
            recorder.record_node(
                role="critic", kind="node_exit",
                round=max(rounds_used, 1), lane_idx=lane_idx,
                duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    # In multi-critic mode (Phase 10 xmax+critic-extras), the
    # presence of ``lane_idx`` on this invocation means the critic
    # is one of C parallel instances. Two consequences:
    #
    # 1. Don't write critic_decision / critique — they're racy
    #    (last-writer-wins across C parallel lanes) and the
    #    meta-critic downstream overwrites them with the consolidated
    #    values anyway. Suppressing the write keeps the merged state
    #    cleaner pre-meta-critic and saves a no-op overwrite.
    # 2. Don't add to critic_reroutes_used. The additive reducer
    #    would otherwise tick the counter by up to C per round,
    #    blowing the re-route cap C× faster than intended. Meta-
    #    critic owns the single increment based on ITS final
    #    decision.
    _emit_finished("critic", round=max(rounds_used, 1),
                    lane_idx=lane_idx,
                    duration_ms=int(dt * 1000), ok=True)
    if lane_idx is not None:
        return {
            "turns": [turn],
            "total_prompt_tokens": pt,
            "total_completion_tokens": ct,
        }
    # Single-critic path (every base tier + xmedium/xhigh + xmax
    # without critic extras): emit the full delta as before.
    return {
        "critique": text,
        "critic_decision": decision,
        "critic_reroutes_used": rerouted_delta,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def meta_critic_node(state: dict, *, chat_client, model: str,
                     think: Any = True, recorder=None) -> dict:
    """Phase 10: synthesize the C parallel-critic verdicts at xmax
    into one final decision. Reads the C critic critiques from the
    additive ``turns`` list (filtering by ``role == 'critic'`` and
    the current round), feeds them to ``model`` (the primary critic
    model) anonymized, and writes the consolidated
    ``critic_decision`` + ``critique`` to state — overwriting the
    racy values left by the C parallel critics.

    Always runs at xmax+critic-extras even when all C critics
    agree: the synthesizer's prompt always sees a single
    consolidated critique, so the user's experience is predictable
    regardless of consensus.
    """
    rounds_used = int(state.get("research_rounds_used") or 0)
    this_round = max(rounds_used, 1)

    _emit_started("meta_critic", round=this_round, model=model)
    if recorder is not None:
        try:
            recorder.record_node(
                role="meta_critic", kind="node_enter",
                round=this_round,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")

    # Pull the C parallel critiques out of the turns reducer. Order
    # is whatever LangGraph's barrier delivered — anonymized
    # numbering, not stable across runs. Audit-mapping back to
    # models is done via the recorder's per-row ``model`` column.
    critic_verdicts: list[str] = [
        t.content for t in (state.get("turns") or [])
        if getattr(t, "role", None) == "critic"
        and getattr(t, "round", 1) == this_round
        and (getattr(t, "content", "") or "").strip()
    ]

    msgs = build_meta_critic_messages(
        state["question"], state.get("plan", ""),
        state.get("research") or [],
        critic_verdicts,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="meta_critic",
            round=this_round,
        )
    except Exception as e:
        log.exception("meta_critic_node failed: %s", e)
        _emit_finished(
            "meta_critic", round=this_round,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        # Failure mode: keep the council moving by defaulting to
        # ready and surfacing the error in critique. The C critics'
        # raw turns are still on the transcript so an audit can see
        # what they said.
        err_text = (
            f"(meta-critic failed: {e}; defaulting to ready. "
            f"Raw critic verdicts available in transcript.)"
        )
        return {
            "error": f"meta_critic failed: {e}",
            "_role_failed": "meta_critic",
            "critic_decision": "ready",
            "critique": err_text,
            "turns": [RoleTurn(
                role="meta_critic", round=this_round, content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    decision = parse_critic_decision(text)
    # Meta-critic owns the single critic_reroutes_used increment in
    # multi-critic mode (the C parallel critics suppress their own
    # delta — see critic_node). Adds 1 if the consolidated decision
    # is needs_more_research, 0 if ready. The route_after_critic
    # conditional edge then reads the additive total to enforce the
    # effort tier's reroute cap.
    rerouted_delta = 1 if decision == "needs_more_research" else 0
    turn = RoleTurn(
        role="meta_critic", round=this_round, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    if recorder is not None:
        try:
            recorder.record_node(
                role="meta_critic", kind="node_exit",
                round=this_round, duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("meta_critic", round=this_round,
                    duration_ms=int(dt * 1000), ok=True)
    return {
        # Synthesizer reads ``critique`` + ``critic_decision``. In
        # multi-critic mode the C parallel critics deliberately don't
        # write these; meta-critic is the sole writer.
        "critique": text,
        "critic_decision": decision,
        "critic_reroutes_used": rerouted_delta,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def synthesizer_node(state: dict, *, chat_client, model: str,
                     think: Any = True,
                     self_critic: bool = False,
                     recorder=None,
                     prior_messages: Optional[list[dict]] = None,
                     fallback_models: Optional[list[str]] = None) -> dict:
    _emit_started("synthesizer", round=1, model=model)
    if recorder is not None:
        try:
            recorder.record_node(role="synthesizer", kind="node_enter")
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    # Phase 5: same prior-thread-reuse pattern as researcher. The
    # parent's synthesizer thread is [system, user, assistant]; we
    # append the follow-up question and any new researcher findings
    # for THIS follow-up turn so the synthesizer sees a clean
    # multi-turn chat. Falls back to build_synthesizer_messages when
    # prior_messages is None (fresh consultation or v1.0 parent).
    #
    # Contract with the follow-up runner: when prior_messages is
    # set, the runner does NOT pre-seed ``state["research"]`` with
    # the parent's research — those rounds are already in
    # prior_messages. So everything in ``state.get("research")``
    # here is THIS follow-up turn's evidence delta.
    if prior_messages is not None:
        follow_up_q = (state.get("question") or "").strip() or "Continue."
        new_research = [
            r for r in (state.get("research") or [])
            if isinstance(r, str) and r.strip()
        ]
        user_parts = ["FOLLOW-UP QUESTION:\n" + follow_up_q]
        for i, rep in enumerate(new_research, start=1):
            user_parts.append(
                f"\nNEW RESEARCHER REPORT (this follow-up, round {i}):"
                f"\n{rep.strip()}",
            )
        if state.get("critique"):
            user_parts.append(
                f"\nCRITIC'S VERDICT:\n{state['critique'].strip()}"
            )
        user_parts.append(
            "\nWrite the answer to the follow-up question. Reuse "
            "the prior reasoning above where it still applies; only "
            "rewrite what the follow-up actually changes."
        )
        msgs = list(prior_messages) + [{
            "role": "user", "content": "\n".join(user_parts),
        }]
    else:
        # M5: surface injected docs targeted at the synthesizer or
        # "any". The synthesizer is the place a user-injected
        # constraint ("call out GDPR risk", "lead with the bottom
        # line") most often needs to land — it shapes the final
        # answer the user sees.
        extra_ctx_syn = _additional_context_for(state, "synthesizer")
        msgs = build_synthesizer_messages(
            state["question"], state.get("plan", ""),
            state.get("research") or [],
            state.get("critique"),
            self_critic=self_critic,
            additional_context=extra_ctx_syn,
            # M10: surface coder lane outputs when the coder role
            # contributed to this consultation. The block is omitted
            # when the channel is empty / absent.
            coder_artifacts=state.get("coder_artifacts") or [],
        )
    # 2026-05-07: serial fallback chain. The synthesizer always tries
    # ``model`` first (with its own ChatClient retry budget — ~15 min
    # at the v1.1 defaults). On Exception, walks ``fallback_models``
    # in order. Same chat_client, same prior_messages — only the
    # ``model`` field of the payload changes. First successful model
    # wins; if every model fails, fall through to the degraded-answer
    # path. Each attempt logs an llm_call event with the actual model
    # used so the audit trail in transcript.db is accurate.
    models_to_try: list[str] = [model] + list(fallback_models or [])
    t0 = time.monotonic()
    text: Optional[str] = None
    pt = ct = 0
    last_exc: Optional[Exception] = None
    used_model: str = model
    for attempt_idx, try_model in enumerate(models_to_try):
        try:
            text, pt, ct = _single_shot(
                chat_client, try_model, msgs, think=think,
                recorder=recorder, role="synthesizer", round=1,
            )
            used_model = try_model
            if attempt_idx > 0:
                log.warning(
                    "synthesizer fell back from %s to %s on attempt %d/%d",
                    model, try_model,
                    attempt_idx + 1, len(models_to_try),
                )
            break
        except Exception as e:
            last_exc = e
            if attempt_idx + 1 < len(models_to_try):
                log.warning(
                    "synthesizer model %s failed (%s); "
                    "trying fallback model %s",
                    try_model, e, models_to_try[attempt_idx + 1],
                )
                continue
            # Last fallback also failed — fall through to degraded.
            log.exception(
                "synthesizer_node failed on every model "
                "(primary=%s, fallbacks=%s): %s",
                model, list(fallback_models or []), e,
            )
    if text is None:
        _emit_finished(
            "synthesizer", round=1,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False,
            error=f"{type(last_exc).__name__}: {last_exc}" if last_exc
                  else "synthesizer failed",
        )
        # Synthesizer failure is terminal — propagate as an error
        # but try to surface the researcher + critic work that DID
        # complete as a "degraded answer" so the user gets the raw
        # findings instead of a bare "(consultation incomplete)".
        err_text = f"(synthesizer failed: {last_exc})"
        degraded = _compose_degraded_answer(state, error=str(last_exc))
        return {
            "error": f"synthesizer failed: {last_exc}",
            "_role_failed": "synthesizer",
            "final_answer": degraded,
            "turns": [RoleTurn(
                role="synthesizer", round=1, content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    turn = RoleTurn(
        role="synthesizer", round=1, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    if recorder is not None:
        try:
            recorder.record_node(
                role="synthesizer", kind="node_exit",
                duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("synthesizer", round=1,
                    duration_ms=int(dt * 1000), ok=True)
    return {
        "final_answer": text,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


# ----------------------- initial state ---------------------------- #

def initial_state(*, question: str, cwd: str, models: dict[str, str],
                  topology: str, effort: str) -> dict:
    return {
        "question": question,
        "cwd": cwd,
        "models": models,
        "topology": topology,
        "effort": effort,
        "plan": "",
        "research": [],
        "critique": None,
        "critic_decision": None,
        "final_answer": "",
        "turns": [],
        "research_rounds_used": 0,
        "critic_reroutes_used": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "retries_by_role": {},
    }
