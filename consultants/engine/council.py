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
    # low: planner -> 1 researcher round -> synthesizer (no critic)
    "low":    EffortCaps(1, 0,  6, 4),
    # medium: full council, 1 round, ≤1 reroute
    "medium": EffortCaps(1, 1, 10, 6),
    # high: full council, ≤3 rounds, ≤2 reroutes
    "high":   EffortCaps(3, 2, 16, 10),
    # max: large but bounded — token spend caps in practice
    "max":    EffortCaps(8, 5, 35, 20),
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
                               critique: Optional[str]) -> list[dict]:
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
    return [
        {"role": "system", "content": SYNTHESIZER_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


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

def planner_node(state: dict, *, chat_client, model: str) -> dict:
    t0 = time.monotonic()
    msgs = build_planner_messages(state["question"])
    try:
        plan, pt, ct = _single_shot(chat_client, model, msgs)
    except Exception as e:
        log.exception("planner_node failed: %s", e)
        return {"error": f"planner failed: {e}",
                "_role_failed": "planner"}
    dt = time.monotonic() - t0
    turn = RoleTurn(
        role="planner", round=1, content=plan,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    return {
        "plan": plan,
        "turns": (state.get("turns") or []) + [turn],
        "total_prompt_tokens": (state.get("total_prompt_tokens") or 0) + pt,
        "total_completion_tokens":
            (state.get("total_completion_tokens") or 0) + ct,
    }


def researcher_node(state: dict, *,
                    chat_client,
                    tool_executor,
                    tool_specs: list[dict],
                    grounding_msgs: list[dict],
                    model: str,
                    cwd: str,
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
            think=True,
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

    # Walk the run_loop's transcript-equivalent to summarize tool usage.
    # run_loop returns only the final response, so we don't have the
    # full tool trace here — that's fine for the summary; the full
    # transcript can be added in v1.1 by piping run_loop's seen_calls
    # back out. For now, RoleTurn just records the text + tokens.
    turn = RoleTurn(
        role="researcher", round=this_round, content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    return {
        "research": prior_rounds + [text],
        "research_rounds_used": this_round,
        "turns": (state.get("turns") or []) + [turn],
        "total_prompt_tokens": (state.get("total_prompt_tokens") or 0) + pt,
        "total_completion_tokens":
            (state.get("total_completion_tokens") or 0) + ct,
    }


def critic_node(state: dict, *, chat_client, model: str) -> dict:
    msgs = build_critic_messages(
        state["question"], state["plan"], state.get("research") or [],
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(chat_client, model, msgs)
    except Exception as e:
        log.exception("critic_node failed: %s", e)
        # On critic failure default to "ready" so the council still
        # produces an answer rather than stalling forever.
        return {"error": f"critic failed: {e}",
                "_role_failed": "critic",
                "critic_decision": "ready"}
    dt = time.monotonic() - t0
    decision = parse_critic_decision(text)
    reroutes_used = int(state.get("critic_reroutes_used") or 0)
    if decision == "needs_more_research":
        reroutes_used += 1
    rounds_used = int(state.get("research_rounds_used") or 0)
    turn = RoleTurn(
        role="critic", round=max(rounds_used, 1), content=text,
        prompt_tokens=pt, completion_tokens=ct, duration_seconds=dt,
    )
    return {
        "critique": text,
        "critic_decision": decision,
        "critic_reroutes_used": reroutes_used,
        "turns": (state.get("turns") or []) + [turn],
        "total_prompt_tokens": (state.get("total_prompt_tokens") or 0) + pt,
        "total_completion_tokens":
            (state.get("total_completion_tokens") or 0) + ct,
    }


def synthesizer_node(state: dict, *, chat_client, model: str) -> dict:
    msgs = build_synthesizer_messages(
        state["question"], state.get("plan", ""),
        state.get("research") or [],
        state.get("critique"),
    )
    t0 = time.monotonic()
    try:
        text, pt, ct = _single_shot(chat_client, model, msgs)
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
        "turns": (state.get("turns") or []) + [turn],
        "total_prompt_tokens": (state.get("total_prompt_tokens") or 0) + pt,
        "total_completion_tokens":
            (state.get("total_completion_tokens") or 0) + ct,
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
