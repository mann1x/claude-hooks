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
    # Trace 2026-05-07 (csl-...-b9d0) showed the researcher hitting
    # 10 iters with diminishing returns past iter 6 (output 92, 237,
    # 317, 293, 251 tokens iters 5-9 vs 830 closing tokens at iter 10).
    # Tightened caps below shave 2-3 iters off typical runs without
    # affecting answer quality. force_answer_after kicks in one
    # iteration before max so the researcher always has a clean
    # closing-summary turn with tools stripped.
    #
    # low: planner -> ≤4 researcher iters -> synthesizer (no critic)
    "low":    EffortCaps(1, 0,  4, 3),
    # medium: full council, 1 round, ≤1 reroute, ≤6 iters
    "medium": EffortCaps(1, 1,  6, 5),
    # high: full council, ≤3 rounds, ≤2 reroutes, ≤10 iters
    "high":   EffortCaps(3, 2, 10,  8),
    # max: large but bounded — token spend caps in practice
    "max":    EffortCaps(8, 5, 25, 18),
}


def caps_for(effort: str) -> EffortCaps:
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
    "distinct sections."
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
    "distinct sections."
)


def build_planner_messages(question: str) -> list[dict]:
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": question.strip()},
    ]


def build_researcher_messages(question: str, plan: str,
                              prior_rounds: list[str],
                              grounding_msgs: list[dict]) -> list[dict]:
    """Researcher's conversation seed.

    Grounding (anchor files + structure map + addendum) goes first, so
    it sits at the start of context regardless of multi-round growth.
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
    msgs.append({"role": "user", "content": "\n".join(user_parts)})
    return msgs


def build_critic_messages(question: str, plan: str,
                          research_rounds: list[str]) -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    return [
        {"role": "system", "content": CRITIC_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_synthesizer_messages(question: str, plan: str,
                               research_rounds: list[str],
                               critique: Optional[str],
                               *, self_critic: bool = False) -> list[dict]:
    parts = [
        f"USER QUESTION:\n{question.strip()}",
        f"\nPLANNER'S PLAN:\n{plan.strip()}",
    ]
    for i, r in enumerate(research_rounds, start=1):
        parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
    if critique:
        parts.append(f"\nCRITIC'S VERDICT:\n{critique.strip()}")
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
    edge calls this."""
    decision = state.get("critic_decision") or "ready"
    if decision == "ready":
        return ROUTE_SYNTHESIZER
    rounds_used = int(state.get("research_rounds_used") or 1)
    reroutes_used = int(state.get("critic_reroutes_used") or 0)
    effort = str(state.get("effort") or "medium")
    caps = caps_for(effort)
    if reroutes_used >= caps.critic_reroutes_max:
        log.info(
            "route_after_critic: reroute cap hit (%d >= %d); -> synthesizer",
            reroutes_used, caps.critic_reroutes_max,
        )
        return ROUTE_SYNTHESIZER
    if rounds_used >= caps.researcher_rounds_max:
        log.info(
            "route_after_critic: researcher round cap hit (%d >= %d); "
            "-> synthesizer",
            rounds_used, caps.researcher_rounds_max,
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
                 *, think: Any = True) -> tuple[str, int, int]:
    """Run a one-call chat (no tools, no loop). Returns
    (text, prompt_tokens, completion_tokens)."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
    }
    response = chat_client.chat(payload)
    return (_extract_text(response), *_usage_from(response))


# ----------------------- node implementations -------------------- #
# Each node mutates only its own slice of state. The LangGraph
# Reducer pattern would let us return partial updates that are
# merged into state; we follow the same convention here so graph.py
# can plug these in directly.

def planner_node(state: dict, *, chat_client, model: str,
                 think: Any = True) -> dict:
    t0 = time.monotonic()
    msgs = build_planner_messages(state["question"])
    try:
        plan, pt, ct = _single_shot(chat_client, model, msgs, think=think)
    except Exception as e:
        log.exception("planner_node failed: %s", e)
        return {"error": f"planner failed: {e}",
                "_role_failed": "planner"}
    dt = time.monotonic() - t0
    turn = RoleTurn(
        role="planner", round=1, content=plan,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    plan_items = parse_plan_items(plan)
    # Delta-only return — additive reducers in CouncilState merge
    # ``turns``, ``total_*_tokens``, ``research_rounds_used`` across
    # parallel fan-out lanes.
    return {
        "plan": plan,
        "plan_items": plan_items,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def researcher_node(state: dict, *,
                    chat_client,
                    tool_executor,
                    tool_specs: list[dict],
                    grounding_msgs: list[dict],
                    model: str,
                    cwd: str,
                    think: Any = True,
                    loop_runner=None) -> dict:
    """Researcher uses agent_loop.runner.run_loop for a tool sub-loop.

    ``loop_runner`` defaults to ``claude_hooks.agent_loop.runner.run_loop``
    but is injectable for tests. We import lazily to keep the module
    importable in environments where claude_hooks isn't on the path
    (although in practice it always is — this is just defensive).
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

    rounds_used = int(state.get("research_rounds_used") or 0)
    this_round = rounds_used + 1
    prior_rounds: list[str] = list(state.get("research") or [])

    # Fan-out path: when invoked via Send with a ``plan_item``, work
    # only on that sub-question. The single-researcher path (no
    # plan_item) — used for critic re-routes and unfanned topologies
    # — uses the full plan + prior research rounds for context.
    plan_item = state.get("plan_item")
    if plan_item:
        focused_plan = (
            f"Sub-research lane {state.get('lane_idx', 0) + 1}: "
            f"{plan_item}"
        )
        msgs = build_researcher_messages(
            state["question"], focused_plan, [], grounding_msgs,
        )
    else:
        msgs = build_researcher_messages(
            state["question"], state["plan"], prior_rounds, grounding_msgs,
        )
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

    t0 = time.monotonic()
    try:
        final = loop_runner(
            payload, cwd,
            config=cfg,
            tool_specs=tool_specs,
            chat_fn=chat_client.chat,
            tool_executor=tool_executor,
        )
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        return {"error": f"researcher failed: {e}",
                "_role_failed": "researcher"}
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
            follow_up = chat_client.chat({
                "model": model,
                "messages": summary_msgs,
                "stream": False,
                "think": think,
            })
            text2 = _extract_text(follow_up)
            pt2, ct2 = _usage_from(follow_up)
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
    return {
        "research": [text],
        "research_rounds_used": 1,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def critic_node(state: dict, *, chat_client, model: str,
                think: Any = True) -> dict:
    msgs = build_critic_messages(
        state["question"], state["plan"], state.get("research") or [],
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(chat_client, model, msgs, think=think)
    except Exception as e:
        log.exception("critic_node failed: %s", e)
        # On critic failure default to "ready" so the council still
        # produces an answer rather than stalling forever.
        return {"error": f"critic failed: {e}",
                "_role_failed": "critic",
                "critic_decision": "ready"}
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
    return {
        "critique": text,
        "critic_decision": decision,
        "critic_reroutes_used": rerouted_delta,
        "turns": [turn],
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
    }


def synthesizer_node(state: dict, *, chat_client, model: str,
                     think: Any = True,
                     self_critic: bool = False) -> dict:
    msgs = build_synthesizer_messages(
        state["question"], state.get("plan", ""),
        state.get("research") or [],
        state.get("critique"),
        self_critic=self_critic,
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(chat_client, model, msgs, think=think)
    except Exception as e:
        log.exception("synthesizer_node failed: %s", e)
        # Synthesizer failure is terminal — propagate as an error
        # but produce a placeholder final_answer so storage still
        # writes something readable.
        return {
            "error": f"synthesizer failed: {e}",
            "_role_failed": "synthesizer",
            "final_answer": (
                f"(consultation incomplete: synthesizer error: {e})"
            ),
        }
    dt = time.monotonic() - t0
    turn = RoleTurn(
        role="synthesizer", round=1, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
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
