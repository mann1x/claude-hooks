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

from claude_hooks import capped_thinking, token_calib, truncation
from consultants.engine import budget, retrospective
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


# ---------- M0: self-confidence extraction ------------------------ #
# The synthesizer and critic each end their output with a
# ``CONFIDENCE: <0.0-1.0>`` self-rating line. We pull it out of the
# raw text (so it never leaks into the user-facing answer or the
# critique the synthesizer reads), append it to the ``confidence``
# state channel, and emit a ConfidenceUpdate event. This is what makes
# ``interrupt_on_low_confidence`` + the xauto escalator + the M2
# adversary-checkpoint gate actually fire — before this the channel
# was fully plumbed but never written.

_CONFIDENCE_RX = re.compile(
    r"(?im)^[ \t>*-]*CONFIDENCE[ \t]*[:=][ \t]*([01](?:\.\d+)?|0?\.\d+)\b[ \t]*$",
)


def _extract_confidence(text: str) -> tuple[str, Optional[float]]:
    """Pull a trailing ``CONFIDENCE: <0.0-1.0>`` self-rating out of a
    node's output.

    Returns ``(text_without_the_line, score)`` or ``(text, None)`` when
    the line is absent or malformed. The score is clamped to ``[0, 1]``.
    The matched line is stripped so the rating never reaches the user.
    The LAST match wins (the rating is emitted at the very end).
    """
    if not text:
        return text, None
    last: Optional[re.Match] = None
    for last in _CONFIDENCE_RX.finditer(text):
        pass
    if last is None:
        return text, None
    try:
        score = float(last.group(1))
    except (TypeError, ValueError):  # pragma: no cover - regex guards this
        return text, None
    score = max(0.0, min(1.0, score))
    stripped = (text[: last.start()] + text[last.end():]).rstrip()
    return stripped, score


def _emit_confidence(score: Optional[float], *, source: str,
                     state: dict) -> None:
    """Best-effort emit of a :class:`ConfidenceUpdate`. No-op on None.

    Never raises — telemetry must not break a node. The ``target`` is
    the live ``runtime_control.confidence_target`` so a consumer can
    render "0.62 / 0.70 — below threshold".
    """
    if score is None:
        return
    try:
        from consultants.engine.events import ConfidenceUpdate, emit
        from consultants.engine.control import (
            runtime_get, DEFAULT_CONFIDENCE_TARGET,
        )
        target = runtime_get(state, "confidence_target",
                             DEFAULT_CONFIDENCE_TARGET)
        emit(ConfidenceUpdate(
            score=float(score), source=source,
            target=float(target) if target is not None else None,
        ))
    except Exception:  # pragma: no cover - telemetry must never raise
        log.debug("emit ConfidenceUpdate raised; ignored", exc_info=True)


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
    "another full council round.\n\n"
    "CITATION INTEGRITY (load-bearing). Every `path:line` you emit "
    "MUST come from a real tool result you saw this turn — not "
    "from inference, not from a plausible-sounding filename. A "
    "downstream linter marks unverified cites inline. Three "
    "failure modes to avoid:\n"
    "  (1) citing a sibling file you never actually read;\n"
    "  (2) writing a line number from memory instead of grep output;\n"
    "  (3) writing a numbered SOURCE LISTING (lines of code with "
    "prepended line numbers) for a file you did not read — a long "
    "block of plausible-looking imports / fields / methods that "
    "reads like a verbatim quote but isn't grounded in any "
    "tool_result.\n"
    "If tool_results don't cover a file, say so — 'evidence "
    "insufficient: did not read X' is a valid report. If you "
    "know the function name but not its verified line, cite "
    "`path: <function_name>` and leave the line-precision gap "
    "visible. NEVER fabricate path, line, or source-listing "
    "content."
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
    "extra round costs another full agent loop.\n\n"
    "FINAL LINE (load-bearing). End your output with a line "
    "`CONFIDENCE: <0.0-1.0>` — your confidence that the assembled "
    "evidence is sufficient AND correct for the synthesizer (1.0 = "
    "certain; <0.7 = shaky / contested / thin). The engine consumes "
    "this line and strips it; it is not shown to the user."
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
    "for more research.\n\n"
    "FINAL LINE (load-bearing). End your output with a line "
    "`CONFIDENCE: <0.0-1.0>` — the consolidated confidence that the "
    "evidence is sufficient AND correct (factor in critic "
    "disagreement: wide disagreement lowers it). The engine consumes "
    "this line and strips it; it is not shown to the user."
)

# M4: the dynamic critic dial. ``runtime_critic_strictness`` selects a
# directive appended to the critic / meta-critic user message.
# ``normal`` is ABSENT on purpose — it contributes nothing, so the
# default-config prompt is byte-identical to v1 (cohort-2 parity).
_CRITIC_STRICTNESS_DIRECTIVE: dict[str, str] = {
    "lax": (
        "STRICTNESS: lax. Bias hard toward `ready`. Request another "
        "research round ONLY for a missing fact that would change the "
        "answer's bottom line; tolerate thin-but-sufficient evidence."
    ),
    "strict": (
        "STRICTNESS: strict. Hold the evidence to a high bar: demand a "
        "`path:line` or named source for every load-bearing claim. If a "
        "key fact rests on a single unverified assertion, that is a "
        "concrete gap worth another round."
    ),
    "adversarial": (
        "STRICTNESS: adversarial. Actively try to BREAK the evidence — "
        "hunt for the unstated assumption, the edge case the research "
        "skipped, the citation that doesn't actually say what it's "
        "used for, the claim that's true in general but false here. "
        "Still request research only for a concrete, nameable gap, but "
        "look harder than usual for one."
    ),
}


def _append_critic_dial(parts: list[str], *, strictness: str,
                        adversarial_focus: str) -> None:
    """Append the M4 strictness directive + any injected adversarial
    focus to a critic / meta-critic message body. No-op for the default
    (``normal`` strictness, empty focus) so the v1 prompt is preserved."""
    directive = _CRITIC_STRICTNESS_DIRECTIVE.get(strictness)
    if directive:
        parts.append("\n" + directive)
    if adversarial_focus and adversarial_focus.strip():
        parts.append(
            "\nADVERSARIAL FOCUS (attack this specifically):\n"
            + adversarial_focus.strip())


def _read_critic_dial(state: dict) -> tuple[str, str]:
    """Read the live critic dial — ``(strictness, adversarial_focus)`` —
    from ``state['runtime_control']`` for the critic / meta-critic
    nodes. Lazy-imports ``control`` (langgraph-free) so the parser-only
    import surface of this module stays clean. Defaults to
    ``("normal", "")`` when runtime_control is absent (legacy v1 path)
    so the prompt is byte-identical (cohort-2 parity)."""
    try:
        from consultants.engine import control
        return (control.runtime_critic_strictness(state),
                control.runtime_adversarial_focus(state))
    except Exception:  # pragma: no cover — defensive
        return ("normal", "")


def build_meta_critic_messages(
    question: str, plan: str,
    research_rounds: list[str],
    critic_verdicts: list[str],
    *,
    strictness: str = "normal",
    adversarial_focus: str = "",
) -> list[dict]:
    """Build the meta-critic's prompt. Critics are anonymized as
    ``Critic 1``, ``Critic 2``, ... in the order ``critic_verdicts``
    arrives — the recorder's per-row ``model`` column is the audit
    map back to which Ollama tag produced which verdict.

    Identity is anonymized to avoid biasing the meta-critic toward
    a model it 'knows' performs better. The recorder is the source
    of truth for who-said-what.

    ``strictness`` + ``adversarial_focus`` (M4) thread the same dynamic
    critic dial used by ``build_critic_messages`` so the x-tier
    meta-critic doesn't silently degrade to the default when an
    operator sharpens the live dial. Default (normal, no focus) →
    byte-identical to v1 (cohort-2 parity).
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
    # M4: dynamic critic dial — appended before the closing
    # instruction so the directive reads as additional guidance, not a
    # trailing afterthought. No-op for the default (parity).
    _append_critic_dial(parts, strictness=strictness,
                        adversarial_focus=adversarial_focus)
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
    "EXHAUSTIVE ENUMERATION. For list-shaped questions (edge "
    "cases, failure modes, gotchas, options, alternatives): if the "
    "researchers identified N items, cover all N. Walk the reports "
    "item by item before emitting your stop token. Mark a sub-case "
    "explicitly rather than dropping it.\n\n"
    "If any researcher report is a failure tombstone — a string that "
    "begins with `(researcher lane failed:` or `(planner failed:` — "
    "do NOT silently synthesize over the gap. Either name the "
    "specific plan item or sub-question that could not be verified "
    "(\"could not verify <X> because <lane> failed: <error>\"), or, "
    "when the surviving evidence is too thin to answer at all, "
    "state that plainly and stop. The tombstone is the system's "
    "signal that a lane crashed; never treat it as evidence.\n\n"
    "CITATION INTEGRITY (load-bearing). Every `path:line` in your "
    "answer MUST appear verbatim in a researcher report or tool "
    "result. A downstream linter checks every cite against the "
    "filesystem and tags fabrications inline as "
    "`path:line [unverified — …]`. Do NOT swap a researcher's "
    "`path` for a sibling that sounds related; do NOT 'sharpen' a "
    "line number — relay the exact cite or omit the line. If "
    "researchers disagree, name both as `path:lineA / lineB "
    "(researchers disagree)`.\n\n"
    "FINAL LINE (load-bearing). After the answer, output a line "
    "`CONFIDENCE: <0.0-1.0>` — your own confidence that the answer is "
    "correct and complete (1.0 = certain; lower it for thin evidence, "
    "unresolved disagreement, or a failed lane). The engine strips "
    "this line; the user never sees it."
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
    "EXHAUSTIVE ENUMERATION. For list-shaped questions: cover "
    "every item the researchers reported. Walk the reports item "
    "by item before stopping.\n\n"
    "If any researcher report is a failure tombstone — a string that "
    "begins with `(researcher lane failed:` or `(planner failed:` — "
    "do NOT silently synthesize over the gap. Either name the "
    "specific plan item or sub-question that could not be verified "
    "(\"could not verify <X> because <lane> failed: <error>\"), or, "
    "when the surviving evidence is too thin to answer at all, "
    "state that plainly and stop. The tombstone is the system's "
    "signal that a lane crashed; never treat it as evidence.\n\n"
    "CITATION INTEGRITY (load-bearing). Every `path:line` you put "
    "in the final answer MUST appear verbatim in at least one "
    "researcher report or tool result you were given. Do NOT "
    "introduce new path:line citations no researcher emitted — a "
    "downstream linter scans the answer against the actual "
    "filesystem and will mark every fabrication inline. Do NOT "
    "swap a researcher's `path` for a sibling 'sounds-related' "
    "filename. Do NOT replace a researcher's line number with one "
    "that 'looks more precise'. Relay exactly, or omit the line.\n\n"
    "FINAL LINE (load-bearing). After the answer, output a line "
    "`CONFIDENCE: <0.0-1.0>` — your own confidence that the answer is "
    "correct and complete, after the self-critique above (1.0 = "
    "certain). The engine strips this line; the user never sees it."
)


# ----------------------- adversary (M3) --------------------------- #
# Opt-in post-synthesis refuter. A SINGLETON node that runs once after
# the synthesizer (synthesizer → adversary → END) — no per-lane Send,
# so Phase 9/10 x-tier fanout upstream is untouched. Default OFF.

ADVERSARY_SYSTEM = _role_prompt(
    "ROLE: adversary. You are the council's red team. A final answer "
    "has ALREADY been written by the synthesizer. Your ONLY job is to "
    "REFUTE it — surface claims the evidence does not support: "
    "hallucinated facts, fabricated or mis-attributed `path:line` "
    "citations, overstated certainty, logical leaps, and edge cases "
    "the answer glosses over.\n\n"
    "You do NOT rewrite the answer. You do NOT ask for more research "
    "(the research phase is over). You do NOT praise or summarize. You "
    "surface ONLY what is wrong, unsupported, or overclaimed.\n\n"
    "OUTPUT (load-bearing). Emit EXACTLY one block, nothing else:\n"
    "  REFUTATION: none\n"
    "when every material claim is backed by a researcher report or "
    "tool result, OR\n"
    "  REFUTATION:\n"
    "  - <the claim, quoted briefly> — <why the evidence doesn't "
    "support it>\n"
    "  - <next issue>\n"
    "one bullet per real problem. Quote the claim and name the gap. "
    "If you genuinely cannot find a real problem, emit "
    "`REFUTATION: none` — do NOT invent issues to look busy, and do "
    "NOT flag a claim merely because it lacks a citation when the "
    "evidence plainly supports it (unless STRICTNESS says otherwise)."
)

# M1 ``adversary_strictness`` tunes how aggressively the refuter fires.
_ADVERSARY_STRICTNESS_DIRECTIVE = {
    "soft": (
        "STRICTNESS: soft. Flag ONLY clear hallucinations and "
        "fabricated / mis-attributed facts and citations. Let "
        "reasonable inferences and minor hedging pass."
    ),
    "normal": (
        "STRICTNESS: normal. Flag hallucinations and fabricated "
        "citations, AND any materially unsupported claim or "
        "overstated certainty."
    ),
    "strict": (
        "STRICTNESS: strict. Challenge EVERY factual assertion not "
        "directly backed by a researcher report or tool result. Demand "
        "a citation for each; treat an uncited factual claim as "
        "unsupported until the evidence shows otherwise."
    ),
}


def build_adversary_messages(question: str, final_answer: str, plan: str,
                             research_rounds: list[str], *,
                             strictness: str = "normal") -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{(plan or '').strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        if isinstance(r, str) and r.strip():
            parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    parts.append(f"\nFINAL ANSWER TO REFUTE:\n{final_answer.strip()}")
    parts.append(
        "\n" + _ADVERSARY_STRICTNESS_DIRECTIVE.get(
            strictness, _ADVERSARY_STRICTNESS_DIRECTIVE["normal"]))
    parts.append("\nNow emit your REFUTATION block — nothing else.")
    return [
        {"role": "system", "content": ADVERSARY_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


_REFUTATION_RX = re.compile(r"(?is)\bREFUTATION\s*[:=]\s*(.*)$")


def parse_adversary_refutation(text: str) -> tuple[str, str]:
    """Parse the adversary's output into ``(decision, body)``.

    ``decision`` is ``"none"`` when the refuter cleared the answer,
    ``"issues"`` when it raised problems. ``body`` is the refutation
    text (empty for ``none``). Tolerant: a ``REFUTATION:`` header is
    preferred; absent one, a non-trivial reply is treated as issues."""
    m = _REFUTATION_RX.search(text or "")
    if not m:
        body = (text or "").strip()
        return ("issues", body) if body else ("none", "")
    body = m.group(1).strip()
    low = body.lower()
    if (not body
            or low.startswith("none")
            or low in ("no issues", "no refutation", "n/a", "-")):
        return "none", ""
    return "issues", body


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
                          additional_context=None,
                          strictness: str = "normal",
                          adversarial_focus: str = "") -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    extra = _additional_context_block(additional_context)
    if extra:
        parts.append("\n" + extra)
    # M4: dynamic critic dial. No-op for the default (normal, no focus)
    # so the prompt is byte-identical to v1 (cohort-2 parity).
    _append_critic_dial(parts, strictness=strictness,
                        adversarial_focus=adversarial_focus)
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

def _raw_text(response: dict) -> str:
    """The assistant text exactly as returned, whitespace intact.
    Tolerates the Ollama-native shape in case a caller passes an
    un-translated response."""
    if "choices" in response:
        choices = response.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return msg.get("content") or ""
        return ""
    msg = response.get("message") or {}
    return msg.get("content") or ""


def _extract_text(response: dict) -> str:
    """Pull the assistant text out of a chat-completion response."""
    return _raw_text(response).strip()


def _usage_from(response: dict) -> tuple[int, int]:
    u = response.get("usage") or {}
    return (
        int(u.get("prompt_tokens") or u.get("prompt_eval_count") or 0),
        int(u.get("completion_tokens") or u.get("eval_count") or 0),
    )


#: How many times a cut-off answer may be continued. Each continuation
#: is a fresh call whose prompt carries the partial answer, so the cost
#: is real; two is enough to recover a synthesizer that overran its
#: budget without letting a model that cannot stop bill indefinitely.
MAX_CONTINUATIONS = 2

_CONTINUE_INSTRUCTION = (
    "Your previous message was cut off by the output limit before you "
    "finished. Continue from exactly where it stopped — resume "
    "mid-sentence if that is where it ended. Do not repeat any text you "
    "already produced, do not restate the question, and do not open with "
    "a preamble; the two halves will be concatenated verbatim."
)


def _observe_usage(messages: list[dict], response: dict,
                   prompt_tokens: int, completion_tokens: int,
                   model: str) -> None:
    """Feed a real provider count back into the token ratios.

    Two separate calibrations, because prompt and reasoning do not
    tokenize alike and averaging them is what makes an estimate wrong in
    the direction that lets a request be built too large:

    * the **prompt** ratio, from the serialized request against
      ``prompt_eval_count``, with the reasoning characters charged at
      their own rate first so the two halves don't both account for the
      same characters;
    * the **reasoning** ratio, but only from a turn that was mostly
      reasoning — a turn that was mostly a tool call would teach it
      about JSON.

    Never raises: calibration is an optimisation, and a run that dies
    because it tried to get smarter about tokens is a worse outcome than
    one that stays on the conservative default.
    """
    try:
        chars, reasoning_chars = token_calib.measure_request_chars(messages)
        token_calib.observe_request_tokens(
            chars, prompt_tokens, reasoning_chars, model)
        if completion_tokens > 0:
            msg = ((response.get("choices") or [{}])[0].get("message") or {})
            thinking = capped_thinking.thinking_text(msg)
            produced = capped_thinking.produced_characters(msg)
            # "Mostly reasoning" — the caller's judgement, per
            # observe_thinking_tokens' contract.
            if thinking and produced and len(thinking) / produced >= 0.6:
                token_calib.observe_thinking_tokens(
                    len(thinking), completion_tokens, model)
    except Exception:  # pragma: no cover — never sink a call
        log.debug("token calibration failed; staying on defaults",
                  exc_info=True)


def _plan_and_compact(chat_client, model: str, messages: list[dict], *,
                      role: Optional[str] = None
                      ) -> tuple[list[dict], "budget.Budget"]:
    """Size the next call, compacting the history first if it no longer
    leaves room to answer.

    Re-planned after compacting rather than assumed: the whole point of
    dropping messages is that the budget changes, and a plan computed
    against the pre-compaction history would hand back the number the
    compaction was supposed to fix.

    The cap that wins is recorded on the process-wide calibration state
    so a later reader — the truncation verdict, a post-mortem — can ask
    what limited the reply rather than inferring it.
    """
    plan = budget.plan(chat_client, model, messages)
    if plan.needs_compaction:
        log.warning("role=%s: %s — compacting history", role, plan.detail)
        keep = plan.trigger_tokens or max(
            0, (plan.context_length or 0)
            - budget.MAX_OUTPUT_TOKENS - budget.WINDOW_MARGIN_TOKENS)
        previous = retrospective.previous_digest(messages)

        def _marker(dropped_msgs: list[dict], generation: int) -> dict:
            """Two passes over the span being dropped, then the message
            that replaces it.

            Runs inside compaction rather than after it because this is
            the only moment the discarded turns still exist: once
            ``compact_messages`` returns they are gone, and a summary
            written from what survived would describe the wrong thing.
            """
            digest = retrospective.write(
                dropped_msgs, chat_client=chat_client, model=model,
                target_tokens=keep, generation=generation,
                previous=previous)
            if digest.empty:
                log.warning(
                    "role=%s: compaction digest came back empty (%s); the "
                    "elided span leaves only the marker",
                    role, digest.stand_down or "both phases silent")
                return None
            return retrospective.render_marker(digest, len(dropped_msgs))

        result = budget.compact_messages(
            messages, keep_tokens=keep, context_length=plan.context_length,
            model=model, make_marker=_marker)
        if result.changed:
            messages = result.messages
            plan = budget.plan(chat_client, model, messages)
    token_calib.note_output_cap(plan.cap_report(), model)
    return messages, plan


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

    Three things happen around the call itself (2026-08-12):

    * the model's real context window is probed and an explicit
      ``num_predict`` is sent, so the output ceiling is ours rather
      than an unseen provider default;
    * a history that no longer leaves room to answer is compacted;
    * a completion the backend cut short is **continued** rather than
      returned as-is. Continuation is the only response that preserves
      the work: the alternative for a synthesizer that spent 43
      minutes of council output is to hand back a third of an answer
      or to throw all of it away.

    Token counts returned are summed across continuations, so a
    caller's accounting stays true.
    """
    convo, plan = _plan_and_compact(chat_client, model, list(messages),
                                    role=role)

    text_parts: list[str] = []
    total_pt = total_ct = 0

    for attempt in range(MAX_CONTINUATIONS + 1):
        payload = {
            "model": model,
            "messages": convo,
            "stream": False,
            "think": think,
            "options": {"num_predict": plan.output_tokens},
        }
        t0 = time.monotonic()
        try:
            response = chat_client.chat(payload)
        except Exception as exc:
            if recorder is not None and role is not None:
                try:
                    recorder.record_llm(
                        role=role, round=round, lane_idx=lane_idx,
                        model=model, request=payload, response=None,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:  # pragma: no cover — must not mask
                    log.exception("recorder.record_llm raised; ignored")
            raise
        dt_ms = int((time.monotonic() - t0) * 1000)
        pt, ct = _usage_from(response)
        total_pt += pt
        total_ct += ct
        # Calibrate on what this request actually cost. The estimate that
        # sized the budget was a guess about how this content tokenizes;
        # the provider just answered the question. Cheap, and it is the
        # difference between a ratio that drifts 1.7x high all session and
        # one that converges after the first call.
        _observe_usage(convo, response, pt, ct, model)
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

        # Raw, unstripped: a continuation resumes mid-word, so stripping
        # each part before joining would fuse the last word of one onto
        # the first of the next. The join is stripped once, at the end.
        text_parts.append(_raw_text(response))

        cut = truncation.classify(response, role=role or "", model=model)
        if cut is None:
            break
        if attempt == MAX_CONTINUATIONS:
            log.error(
                "role=%s: still truncated after %d continuation(s) — "
                "returning the partial answer; the run will be marked "
                "failed. %s", role, MAX_CONTINUATIONS, cut.detail,
            )
            break
        log.warning(
            "role=%s: %s — continuing (%d/%d)",
            role, cut.detail, attempt + 1, MAX_CONTINUATIONS,
        )
        # The partial becomes context for its own continuation. Joining
        # without a separator is deliberate: the model was told to
        # resume mid-sentence, so any inserted whitespace would land in
        # the middle of a word.
        convo = convo + [
            {"role": "assistant", "content": "".join(text_parts)},
            {"role": "user", "content": _CONTINUE_INSTRUCTION},
        ]
        # What capped the turn decides what happens next.
        #
        # A **window-bound** cap is compaction's to fix: the history has
        # grown past the point where a full answer fits beside it, and
        # the continuation needs that room back. A cap that came from our
        # own request or the model is not — shrinking the history cannot
        # raise it, so compacting there would spend the transcript to
        # leave the continuation facing the same ceiling with less of the
        # work it was doing. The fork measured exactly that: a
        # 48,508-token request compacted against a 110,000-token window
        # when the cap that ended the turn was the caller's own 32,000.
        #
        # Note what is deliberately *not* done here. The fork gives a
        # truncated turn **less** to spend on its retry, because it
        # discards the half-written reply and regenerates — a smaller
        # budget is what makes the second attempt fit. This is a
        # continuation, not a regeneration: the partial is kept and the
        # model writes only what is left, so cutting the budget each time
        # would make each continuation shorter than the last and turn a
        # bounded recovery into a guaranteed failure.
        convo, plan = _plan_and_compact(chat_client, model, convo, role=role)

    return ("".join(text_parts).strip(), total_pt, total_ct)


# ====================================================================== #
# M-B: uniform role tool access
# ====================================================================== #
# Before M-B the researcher was the only default role that could call a
# tool. planner / critic / meta_critic / synthesizer / adversary each ran
# through ``_single_shot`` — one call, no tools — so they could reason
# about the researcher's text but never check it.
#
# That is why the CitationLinter had to exist. The 2026-05-18 forensic
# (csl-2026-05-18-1031-9e3b) traced a fabricated filename to a researcher
# lane with zero tool calls; the critic could not verify it because the
# critic had no way to look. A critic that can ``read_file`` checks the
# claim directly instead of inheriting it.
#
# Cost is the reason this is opt-in rather than simply switched on. A
# single-shot role costs exactly one LLM call; a tooled role costs one
# per iteration, and critic fans out per lane at the x-tiers, so the
# multiplier is roles × lanes × iterations. The caps below are therefore
# deliberately tighter than the researcher's: these roles are meant to
# *check* a handful of specific claims, not to conduct research. If a
# critic needs eight tool calls to form a verdict, the plan was wrong.
ROLE_TOOL_MAX_ITERATIONS = 4
ROLE_TOOL_MAX_CALLS_PER_TURN = 4
#: Strip tools after this many iterations so a role always produces text.
ROLE_TOOL_FORCE_ANSWER_AFTER = 3


# ---------------------------------------------------------------------- #
# The tool addendum — why wiring alone was not enough
# ---------------------------------------------------------------------- #
# The first live M-B bench (2026-08-01, gemma4:31b-cloud, 18 tooled
# trials) recorded **zero tool calls**. The surface was live — 11 specs
# in every payload, the registry answering correctly when called
# directly — and the model declined it every single time.
#
# The cause is in the prompts. Each role's system prompt predates the
# tool surface and describes a job that does not involve looking at
# anything. CRITIC_SYSTEM is the sharpest case: it frames the job as a
# routing decision ("is more research needed?") and then says "Default
# to ready unless you can name a concrete missing fact… each extra
# round costs another full agent loop". A model reading that has been
# told, in effect, that investigating is expensive and the safe answer
# is yes. ADVERSARY_SYSTEM is the most ironic: it is explicitly asked to
# catch "fabricated or mis-attributed `path:line` citations" while
# having no way to check one, so it can only judge whether a claim was
# *reported*, never whether the report was *true*.
#
# So the addendum is not decoration around the plumbing; it is the half
# that makes the plumbing reachable. It is appended to the system
# message only when a role is actually handed tools, which keeps the
# default-config prompt byte-identical (cohort-2 parity) and means a
# role can never be told about a tool it cannot call.

#: What each role should *do* with a tool, in its own terms. Generic
#: encouragement ("you may use tools") loses to a role prompt that
#: already told the model not to bother, so each entry names the
#: specific move and, where the base prompt pushes the other way,
#: overrides it explicitly.
_ROLE_TOOL_DIRECTIVE: dict[str, str] = {
    "planner": (
        "You may call these tools yourself before writing the plan. Use "
        "them sparingly and only to make steps concrete: confirm a file "
        "or symbol you are about to point the researcher at actually "
        "exists, rather than inferring it from a plausible name. A plan "
        "step aimed at a file that is not there costs a full research "
        "round to discover."
    ),
    "critic": (
        "You may call these tools to CHECK the researcher's claims, not "
        "merely to judge whether more research is wanted. This changes "
        "what counts as a concrete missing fact: a load-bearing "
        "`path:line` you looked up and could NOT confirm is concrete — "
        "name it. Verifying a citation costs one cheap tool call, not "
        "another research round, so the 'each extra round is expensive' "
        "caution above does not apply to checking. Spot-check the cites "
        "the answer will rest on; do not re-verify everything.\n"
        "CHECK EQUALITY, NOT EXISTENCE. Finding the symbol is not "
        "confirming the claim. When a report asserts a VALUE "
        "(`MAX_ATTEMPTS = 5`), a LINE (`foo.py:12`), a SIGNATURE, or a "
        "RETURN, read it and compare the two. `grep` returning a hit "
        "tells you the name exists and nothing more — a wrong constant "
        "and a wrong line number both survive a grep that 'succeeds'.\n"
        "NEVER SILENTLY CORRECT. If what you read differs from what a "
        "report claimed, that discrepancy IS your finding — do not "
        "quietly use the right value and call the report accurate. "
        "Report it, attributed to the report it came from by the "
        "`RESEARCHER REPORT (round N)` header above it, in a block "
        "after your justification:\n"
        "  CORRECTIONS:\n"
        "  - report N: claimed <X> — actual <Y> (`path:line`)\n"
        "Omit the block entirely when you corrected nothing; do not "
        "write `CORRECTIONS: none`.\n"
        "A correction is NOT grounds for `needs_more_research`. You "
        "already have the right answer — emitting `ready` WITH a "
        "CORRECTIONS block is the cheap outcome and the expected one. "
        "Re-route only when a fact is missing that you could not "
        "resolve yourself."
    ),
    "meta_critic": (
        "You may call these tools to settle a disagreement between "
        "critics rather than picking a side on plausibility. When "
        "critics disagree about a fact in the code, look it up — a "
        "verified answer beats a weighed one.\n"
        "CARRY EVERY CORRECTION FORWARD. Your consolidated verdict "
        "REPLACES the individual critics' — anything you drop is lost "
        "before the synthesizer sees it. If any critic emitted a "
        "`CORRECTIONS:` block, merge them all into one block in your "
        "own output, keeping the `report N: claimed <X> — actual <Y>` "
        "attribution. Dedupe identical corrections; when two critics "
        "correct the same fact differently, verify it yourself and "
        "emit the checked value."
    ),
    "synthesizer": (
        "You may call these tools to verify a citation before relaying "
        "it. Cheapest use: when you are about to emit a `path:line` "
        "that only one researcher reported, confirm it. Do not conduct "
        "new research — the research phase is over.\n"
        "HONOUR THE CRITIC'S CORRECTIONS. When the critic's verdict "
        "carries a `CORRECTIONS:` block, each line supersedes the "
        "researcher report it names: relay the corrected value or "
        "line, never the superseded one, and do not average or hedge "
        "between them. The critic read the file; the report did not. "
        "You need not re-verify a corrected fact, and you need not "
        "mention that a correction happened — the user wants the "
        "answer, not the council's process."
    ),
    "adversary": (
        "You may call these tools to CHECK the citations you are asked "
        "to refute. Until now you could only judge whether a claim was "
        "reported by a researcher; you can now judge whether the report "
        "was true. Look up the answer's load-bearing `path:line` cites: "
        "a cite that does not resolve, or resolves to something other "
        "than what the answer claims, is exactly the fabricated or "
        "mis-attributed citation this role exists to surface. A claim "
        "you verified and found correct is NOT a refutation — do not "
        "flag it.\n"
        "CHECK EQUALITY, NOT EXISTENCE, and never silently correct. "
        "A cite that resolves to a real file at a real line can still "
        "be wrong about the value, the line, or the signature — "
        "compare what the answer says against what you read. If they "
        "differ, that IS the refutation; quote both. Finding the right "
        "value and letting the wrong one stand is the failure this "
        "role exists to prevent."
    ),
}


def _tool_names(tool_specs: Optional[list[dict]]) -> tuple[str, ...]:
    """Names from OpenAI-shape specs, order preserved, dupes dropped."""
    out: list[str] = []
    for spec in tool_specs or []:
        try:
            name = ((spec.get("function") or {}).get("name")
                    or spec.get("name") or "")
        except AttributeError:  # pragma: no cover — malformed spec
            continue
        if name and name not in out:
            out.append(str(name))
    return tuple(out)


def build_tool_addendum(role: Optional[str],
                        tool_specs: Optional[list[dict]]) -> str:
    """The block appended to a role's system prompt when it has tools.

    Returns ``""`` when the role has no tools or none is known for the
    role, so callers can append unconditionally. The tool list is
    derived from the specs actually in the payload rather than written
    out in prose — a hardcoded list silently goes stale the moment a
    provider is enabled, which is how the researcher ended up being
    told about six tools while eleven were on offer.
    """
    names = _tool_names(tool_specs)
    directive = _ROLE_TOOL_DIRECTIVE.get(role or "")
    if not names or not directive:
        return ""
    return ("\n\nTOOLS AVAILABLE TO YOU: " + ", ".join(names) + ".\n"
            + directive
            + "\nCite what you verify as `path:line`. If a tool call "
            "fails or returns nothing, say so rather than assuming the "
            "claim is false — absence of a result is not evidence.")


def _builtin_tool_names() -> tuple[str, ...]:
    """The six tools RESEARCHER_SYSTEM and the tool-plan prompt spell
    out in prose. Read from the provider rather than re-typed, because
    a second hardcoded list is how the first one went stale."""
    try:
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        return _tool_names(openai_tool_specs())
    except Exception:  # pragma: no cover — package always present
        log.exception("could not resolve builtin tool names")
        return ()


def build_extra_tools_note(tool_specs: Optional[list[dict]]) -> str:
    """Announce tools the prose enumeration does not mention.

    ``RESEARCHER_SYSTEM`` and the tool-plan prompt name their six tools
    inline, and a model generally works from that list rather than from
    the schema array. So enabling a provider — git, later MCP or shell —
    puts tools in the payload that the role has effectively been told do
    not exist. Rather than rewriting the prose (which would change the
    default prompt byte-for-byte and invalidate the M11c corpus), name
    only the *difference*, and only when there is one.
    """
    extras = [n for n in _tool_names(tool_specs)
              if n not in _builtin_tool_names()]
    if not extras:
        return ""
    return ("\n\nALSO AVAILABLE (beyond the tools listed above): "
            + ", ".join(extras) + ". Same citation rules apply.")


def _with_extra_tools_note(messages: list[dict],
                           tool_specs: Optional[list[dict]]) -> list[dict]:
    """Copy of ``messages`` with :func:`build_extra_tools_note` appended
    to the LAST system turn. Byte-identical when there are no extras."""
    note = build_extra_tools_note(tool_specs)
    if not note:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + note
            break
    return out


def _with_tool_addendum(messages: list[dict], role: Optional[str],
                        tool_specs: Optional[list[dict]]) -> list[dict]:
    """Copy ``messages`` with the addendum appended to the system turn.

    Never mutates the caller's list: the same message list is reused
    across x-tier lanes, and appending in place would compound the
    addendum once per lane.
    """
    add = build_tool_addendum(role, tool_specs)
    if not add:
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + add
            return out
    # No system turn (shouldn't happen for these roles) — prepend one
    # rather than dropping the directive on the floor.
    return [{"role": "system", "content": add.lstrip("\n")}] + out


def _role_turn(chat_client, model: str, messages: list[dict],
               *, think: Any = True,
               recorder=None, role: Optional[str] = None,
               round: int = 1,
               lane_idx: Optional[int] = None,
               tool_specs: Optional[list[dict]] = None,
               tool_executor=None,
               cwd: str = "",
               loop_runner=None) -> tuple[str, int, int]:
    """One role turn, returning ``(text, prompt_tokens, completion_tokens)``.

    Runs a tool loop when ``tool_specs`` **and** ``tool_executor`` are
    both supplied; otherwise delegates to :func:`_single_shot`
    unchanged. The default path is therefore byte-identical to pre-M-B
    behaviour — the loop is reachable only when the graph deliberately
    hands a role its tools.

    Failures fall back to a single shot rather than propagating. A role
    that cannot run its tool loop should still deliver its verdict:
    losing the critic entirely because the loop misbehaved is strictly
    worse than a critic that reasons without having looked.
    """
    if not tool_specs or tool_executor is None:
        return _single_shot(
            chat_client, model, messages, think=think,
            recorder=recorder, role=role, round=round, lane_idx=lane_idx,
        )

    if loop_runner is None:
        try:
            from claude_hooks.agent_loop.runner import run_loop
            loop_runner = run_loop
        except Exception:  # pragma: no cover — claude_hooks always present
            log.exception("agent_loop unavailable; %s falls back to "
                          "a single shot", role)
            return _single_shot(
                chat_client, model, messages, think=think,
                recorder=recorder, role=role, round=round,
                lane_idx=lane_idx,
            )

    on_iter_cb = on_tool_cb = None
    if recorder is not None and role is not None:
        def _on_iter(_idx: int, req: dict, resp: dict, dt_ms: int) -> None:
            pt_l, ct_l = _usage_from(resp)
            try:
                recorder.record_llm(
                    role=role, round=round, lane_idx=lane_idx, model=model,
                    request=req, response=resp, prompt_tokens=pt_l,
                    completion_tokens=ct_l, duration_ms=dt_ms,
                )
            except Exception:  # pragma: no cover
                log.exception("recorder.record_llm raised; ignored")

        def _on_tool(name: str, args: str, output: str,
                     dt_ms: int, err: Optional[str]) -> None:
            try:
                recorder.record_tool(
                    role=role, round=round, lane_idx=lane_idx, tool=name,
                    args=args, output=output, duration_ms=dt_ms, error=err,
                )
            except Exception:  # pragma: no cover
                log.exception("recorder.record_tool raised; ignored")

        on_iter_cb, on_tool_cb = _on_iter, _on_tool

    # Applied here, past every fallback branch: a role that ends up in
    # ``_single_shot`` must never see a directive about tools it will
    # not be offered.
    payload = {"model": model,
               "messages": _with_tool_addendum(messages, role, tool_specs),
               "stream": False, "think": think}
    try:
        from claude_hooks.agent_loop.runner import LoopConfig
        cfg = LoopConfig(
            max_iterations=ROLE_TOOL_MAX_ITERATIONS,
            max_tool_calls_per_turn=ROLE_TOOL_MAX_CALLS_PER_TURN,
            force_answer_after=ROLE_TOOL_FORCE_ANSWER_AFTER,
            tools_available=True,
            think=think,
            # These roles are handed a full brief and must be free to
            # answer immediately. Forcing a first tool call would make
            # a critic that has nothing to verify burn a call proving it.
            force_first_tool_call=False,
        )
        loop_kwargs = dict(config=cfg, tool_specs=tool_specs,
                           chat_fn=chat_client.chat,
                           tool_executor=tool_executor)
        if on_iter_cb is not None:
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
        log.warning("%s tool loop failed (%s); falling back to a single "
                    "shot", role, e)
        return _single_shot(
            chat_client, model, messages, think=think,
            recorder=recorder, role=role, round=round, lane_idx=lane_idx,
        )

    text = _extract_text(final)
    pt, ct = _usage_from(final)
    if not text.strip():
        # Same failure the researcher hit in the 2026-05-07 audit: the
        # loop can exhaust its iterations mid-tool-call and return no
        # prose. A role that returns "" is a silent hole downstream, so
        # spend one tool-free call to get its actual answer.
        log.warning("%s: empty text after tool loop; forcing a "
                    "tool-free summary call", role)
        return _single_shot(
            chat_client, model, messages, think=think,
            recorder=recorder, role=role, round=round, lane_idx=lane_idx,
        )
    return (text, pt, ct)


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
                 coder_enabled: bool = False,
                 tool_specs: Optional[list[dict]] = None,
                 tool_executor=None, cwd: str = "") -> dict:
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
        plan, pt, ct = _role_turn(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="planner", round=1,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
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

    # 2026-05-18 (#204): citation lint on researcher REPORT output.
    # The 2026-05-18 csl-2026-05-18-1031-9e3b forensic proved the
    # worst-case fabrication (an entirely fake filename
    # ``consultants/engine/store_sql.py``) originated in a researcher
    # lane (glm-5.1:cloud, lane 5) with ZERO tool calls — pure
    # hallucination. The bad cite then flowed unchallenged through
    # peer_findings into the critic and synthesizer. The synthesizer-
    # side linter (shipped in commit ``159d353``) caught it at the
    # end, but the cite still contaminated every intermediate step.
    #
    # Wiring the linter at the researcher boundary fixes that: the
    # annotated form (``store_sql.py [unverified — file not found]``)
    # is what flows into peer_findings, the critic, and the
    # synthesizer's input — so every downstream role sees the verdict
    # alongside the claim instead of being silently misinformed. The
    # synthesizer-side lint stays as belt-and-suspenders for cites
    # the synthesizer itself introduces or transforms.
    def _lint_research_text(text_in: str) -> str:
        if not isinstance(text_in, str) or not text_in.strip():
            return text_in
        try:
            from consultants.engine.citation_linter import lint_answer
            roots: list[str] = []
            if isinstance(cwd, str) and cwd:
                roots.append(cwd)
            for r in (state.get("extra_roots") or ()):
                if isinstance(r, str) and r:
                    roots.append(r)
            if not roots:
                return text_in
            linted_text, issues = lint_answer(
                text_in, allowed_roots=roots,
            )
            if issues:
                log.info(
                    "researcher citation lint sid=%s lane=%s round=%s: "
                    "%d fabrication(s) annotated; %s",
                    sid, lane_idx, this_round, len(issues),
                    "; ".join(
                        f"{i.original_match} ({i.reason})"
                        for i in issues
                    ),
                )
                from consultants.engine.citation_linter import (
                    root_misconfiguration_hint,
                )
                hint = root_misconfiguration_hint(
                    text_in, issues, roots,
                )
                if hint:
                    log.warning(
                        "researcher citation lint sid=%s lane=%s "
                        "round=%s: %s", sid, lane_idx, this_round, hint,
                    )
            return linted_text
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "citation_linter raised in researcher lane %s; "
                "using unlinted text", lane_idx,
            )
            return text_in
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
        # M14 follow-up (2026-05-18): rebuild ``msgs`` without
        # ``peer_findings`` in REPORT mode. When the M8 store is
        # default-on (M14), sibling lanes' findings get recalled
        # into ``peer_findings_block`` and surfaced as a "## Peer
        # findings (recalled from earlier lanes)" block in the
        # researcher's user message. In PLAN mode this is helpful
        # (lets the lane dedup against what other lanes already
        # tackled). But in REPORT mode it competes with the PRIOR
        # TOOL RESULTS appendix below — the appendix carries no
        # explicit "write a report now" instruction (see
        # ``build_tool_plan_user_appendix``), so the model is left
        # inferring. With peer findings present, gemini-3-flash-
        # preview re-emits another ``tool_plan`` JSON ("let me
        # verify what the peers found") instead of synthesizing
        # the tool results into a report. The M13 smoke worked
        # because the store was disabled and peer_findings was
        # always None. The 2026-05-18 first real M14 consult
        # (csl-2026-05-18-0937-4f4f, 737 s, refusal answer)
        # caught this regression.
        if report_mode:
            extra_ctx_res_local = _additional_context_for(
                state, "researcher",
            )
            if plan_item:
                focused_plan_local = (
                    f"Sub-research lane "
                    f"{state.get('lane_idx', 0) + 1}: "
                    f"{plan_item}"
                )
                msgs = build_researcher_messages(
                    state["question"], focused_plan_local, [],
                    grounding_msgs,
                    additional_context=extra_ctx_res_local,
                    peer_findings=None,
                )
            else:
                msgs = build_researcher_messages(
                    state["question"], state["plan"], prior_rounds,
                    grounding_msgs,
                    additional_context=extra_ctx_res_local,
                    peer_findings=None,
                )
        # Append the mode-specific appendix to the user message.
        # Both append to msgs[-1] (the v1 user message) so the
        # researcher's existing context (plan, prior rounds,
        # additional_context) stays intact.
        appendix = (
            build_tool_plan_user_appendix(prior_for_round)
            if report_mode else RESEARCHER_PLAN_MODE_BLOCK
        )
        if not report_mode:
            # PLAN mode ends with an "Available tools:" enumeration that
            # feeds ``suggested_tools``. Left stale, a provider-supplied
            # tool can never be suggested, so the executor never sees an
            # intent shaped for it. Empty on the default surface.
            appendix = appendix + build_extra_tools_note(tool_specs)
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
            # The turn record keeps the RAW model output so the
            # transcript stays a faithful "what the model said"
            # forensic — citation annotation flows only into the
            # downstream-visible ``research`` field below (see #204
            # docstring above).
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
            # #204: annotate fabricated cites before they flow to
            # peer_findings + the store + the synthesizer's input.
            linted_text = _lint_research_text(text)
            _record_finding_to_store(linted_text)
            return {
                "research": [linted_text],
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
            # #204: same downstream-annotation pattern as REPORT mode.
            linted_text = _lint_research_text(text)
            _record_finding_to_store(linted_text)
            return {
                "research": [linted_text],
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
        # RESEARCHER_SYSTEM enumerates its six tools in prose; anything
        # a provider adds beyond those has to be announced or the role
        # never learns it exists. No-op on the default surface.
        "messages": _with_extra_tools_note(msgs, tool_specs),
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
            # Via _single_shot rather than chat_client.chat directly:
            # this call *is* the lane's report when it fires, so it
            # needs the same output budget, history compaction,
            # continuation-on-truncation and recording as any other
            # role's call. It used to be the one hand-rolled backend
            # call in the engine, and a hand-rolled call is exactly
            # where a fix like this gets forgotten.
            text2, pt2, ct2 = _single_shot(
                chat_client, model, summary_msgs, think=think,
                recorder=recorder, role="researcher",
                round=this_round, lane_idx=lane_idx,
            )
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
    # #204: v1 inline-loop researcher exit — same lint pattern as the
    # M6 REPORT and PLAN-empty branches.
    linted_text = _lint_research_text(text)
    _record_finding_to_store(linted_text)
    return {
        "research": [linted_text],
        "research_rounds_used": 1,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def critic_node(state: dict, *, chat_client, model: str,
                think: Any = True, recorder=None,
                tool_specs: Optional[list[dict]] = None,
                tool_executor=None, cwd: str = "") -> dict:
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
    # M4: live critic dial (strictness + injected adversarial focus).
    _crit_strict, _crit_focus = _read_critic_dial(state)
    msgs = build_critic_messages(
        state["question"], state["plan"], state.get("research") or [],
        additional_context=extra_ctx_critic,
        strictness=_crit_strict, adversarial_focus=_crit_focus,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _role_turn(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="critic",
            round=max(rounds_used_pre, 1),
            lane_idx=lane_idx,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
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
    # M0: strip the critic's CONFIDENCE line out of ``text`` so it's
    # absent from the critique the synthesizer reads + the turn
    # transcript. Emit + append only on the single-critic path below;
    # in fanned mode the meta-critic owns the consolidated value.
    text, _critic_conf = _extract_confidence(text)
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
    _emit_confidence(_critic_conf, source="critic", state=state)
    out = {
        "critique": text,
        "critic_decision": decision,
        "critic_reroutes_used": rerouted_delta,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }
    if _critic_conf is not None:
        out["confidence"] = [_critic_conf]
    return out


def meta_critic_node(state: dict, *, chat_client, model: str,
                     think: Any = True, recorder=None,
                     tool_specs: Optional[list[dict]] = None,
                     tool_executor=None, cwd: str = "") -> dict:
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

    # M4: thread the same live critic dial into the meta-critic so the
    # x-tier consolidation honors a sharpened strictness / focus too.
    _mc_strict, _mc_focus = _read_critic_dial(state)
    msgs = build_meta_critic_messages(
        state["question"], state.get("plan", ""),
        state.get("research") or [],
        critic_verdicts,
        strictness=_mc_strict, adversarial_focus=_mc_focus,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _role_turn(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="meta_critic",
            round=this_round,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
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
    # M0: strip + emit the consolidated confidence (fanned-critic path).
    text, _meta_conf = _extract_confidence(text)
    _emit_confidence(_meta_conf, source="critic", state=state)
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
    out = {
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
    if _meta_conf is not None:
        out["confidence"] = [_meta_conf]
    return out


def synthesizer_node(state: dict, *, chat_client, model: str,
                     think: Any = True,
                     self_critic: bool = False,
                     recorder=None,
                     prior_messages: Optional[list[dict]] = None,
                     fallback_models: Optional[list[str]] = None,
                     tool_specs: Optional[list[dict]] = None,
                     tool_executor=None, cwd: str = "") -> dict:
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
    for attempt_idx, try_model in enumerate(models_to_try):
        try:
            text, pt, ct = _role_turn(
                chat_client, try_model, msgs, think=think,
                recorder=recorder, role="synthesizer", round=1,
                tool_specs=tool_specs, tool_executor=tool_executor,
                cwd=cwd,
            )
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
    # M0: pull the synthesizer's self-confidence out of ``text`` BEFORE
    # the citation lint reassigns it and before the user-facing return,
    # so the CONFIDENCE line never reaches the answer.
    text, _self_conf = _extract_confidence(text)
    _emit_confidence(_self_conf, source="synthesizer", state=state)
    # 2026-05-18: post-synthesis citation lint. The 2026-05-18 first
    # M14 consult caught two fabrication classes in the synthesizer's
    # output — an entirely fake filename relayed forward from a
    # researcher hallucination, plus several wrong-line cites within
    # real files. The linter scans path:line patterns against the
    # session's allowed_roots, annotates fabrications inline as
    # ``path:line [unverified — …]``. Symbol-semantic verification
    # (right function at right line) is out of scope — needs an AST
    # pass; that's a follow-up if the inline annotation isn't enough.
    # The linter is non-blocking: a clean answer survives unchanged.
    try:
        from consultants.engine.citation_linter import lint_answer
        roots: list[str] = []
        cwd = state.get("cwd")
        if isinstance(cwd, str) and cwd:
            roots.append(cwd)
        for r in (state.get("extra_roots") or ()):
            if isinstance(r, str) and r:
                roots.append(r)
        if roots:
            linted_text, issues = lint_answer(text, allowed_roots=roots)
            if issues:
                log.info(
                    "synthesizer citation lint: %d fabrication(s) "
                    "annotated; %s",
                    len(issues),
                    "; ".join(
                        f"{i.original_match} ({i.reason})"
                        for i in issues
                    ),
                )
                from consultants.engine.citation_linter import (
                    root_misconfiguration_hint,
                )
                hint = root_misconfiguration_hint(text, issues, roots)
                if hint:
                    log.warning("synthesizer citation lint: %s", hint)
                text = linted_text
    except Exception:  # pragma: no cover - defensive
        log.exception(
            "citation_linter raised; using unlinted synthesizer output"
        )
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
    out = {
        "final_answer": text,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }
    if _self_conf is not None:
        out["confidence"] = [_self_conf]
    return out


def adversary_node(state: dict, *, chat_client, model: str,
                   think: Any = True, strictness: str = "normal",
                   recorder=None,
                   tool_specs: Optional[list[dict]] = None,
                   tool_executor=None, cwd: str = "") -> dict:
    """M3: post-synthesis red team. Reads the synthesizer's
    ``final_answer`` and the upstream evidence, emits a ``REFUTATION``
    block, and — when it finds real problems — annotates the
    user-facing answer inline AND records the raw refutation in
    ``final_answer_refutation`` / ``adversary_decision``. A singleton
    node (runs once, no Send), so x-tier fanout upstream is untouched.

    Non-fatal everywhere: an empty / failed synthesis is skipped
    (nothing to refute), and an adversary LLM failure leaves the
    synthesizer's answer standing unrefuted (``adversary_decision`` =
    ``error``)."""
    final_answer = (state.get("final_answer") or "").strip()
    # Nothing to refute: the synthesizer produced no answer or failed
    # (degraded path). Leave the degraded answer untouched.
    if not final_answer or state.get("_role_failed") == "synthesizer":
        return {}
    _emit_started("adversary", round=1, model=model)
    if recorder is not None:
        try:
            recorder.record_node(role="adversary", kind="node_enter")
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    msgs = build_adversary_messages(
        state.get("question") or "", final_answer,
        state.get("plan", ""), state.get("research") or [],
        strictness=strictness,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _role_turn(
            chat_client, model, msgs, think=think,
            recorder=recorder, role="adversary", round=1,
            tool_specs=tool_specs, tool_executor=tool_executor, cwd=cwd,
        )
    except Exception as e:
        log.exception("adversary_node failed: %s", e)
        _emit_finished(
            "adversary", round=1,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        # Adversary failure is non-fatal — the answer stands unrefuted.
        return {
            "adversary_decision": "error",
            "turns": [RoleTurn(
                role="adversary", round=1,
                content=f"(adversary failed: {e})",
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
    dt = time.monotonic() - t0
    decision, body = parse_adversary_refutation(text)
    turn = RoleTurn(
        role="adversary", round=1, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    if recorder is not None:
        try:
            recorder.record_node(
                role="adversary", kind="node_exit",
                duration_ms=int(dt * 1000),
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("adversary", round=1,
                    duration_ms=int(dt * 1000), ok=True)
    out: dict = {
        "adversary_decision": decision,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }
    if decision == "issues" and body:
        # Surface the red team's findings to the user inline AND keep
        # the raw refutation for the transcript / the skill's review
        # loop. The synthesizer's mechanical citation-linter annotations
        # (path:line checks) compose with this semantic pass.
        out["final_answer_refutation"] = body
        out["final_answer"] = (
            final_answer
            + "\n\n---\n**⚠️ Adversarial review:**\n" + body
        )
    return out


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
