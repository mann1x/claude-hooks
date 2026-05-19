"""``tool_executor`` node — runs one ``ToolPlanItem`` per Send lane.

When ``cfg.roles.tool_executor.enabled = True``, the researcher
emits a semantic ``tool_plan`` block instead of running the tool
subloop inline; the graph dispatcher emits one ``Send`` per plan
item, each landing here. This node delegates the *mechanics* of
tool calls to a specialist model (default ``gemma4:31b-cloud``)
while the researcher's frontier model owns the *semantic* planning
the council values it for.

Rationale recap (from the plan):

- Frontier models (kimi-k2.6, glm-5.1, deepseek-v4) sometimes
  bungle multi-tool sequences (wrong args, redundant calls, drop
  results on the floor). gemma4-31b is comparatively rock-solid at
  the call mechanics in our trace data.
- Per-lane parallelism: when the planner emits 6 plan items, the
  researcher emits a tool_plan of 6 items in one call; the graph
  fans those out into 6 tool_executor lanes that run concurrently
  rather than the researcher running 6 tools sequentially in its
  own loop.
- Bounded blast radius: each lane is one intent, one model call
  (which may chain multiple tools internally). A lane crash
  tombstones its own result; sibling lanes proceed.

The node:

1. Reads ``state["tool_plan_item"]`` (Send-injected per-lane).
2. Calls ``loop_runner`` (defaults to
   ``claude_hooks.agent_loop.runner.run_loop``) with the
   specialist model + full tool stack + the plan item's intent
   embedded as the user task.
3. Records every iteration's LLM call + every tool call to the
   recorder, tagged ``role="tool_executor"`` (vs the researcher's
   ``role="researcher"`` — the audit trail in transcript.db
   distinguishes who ran which tools).
4. Emits ``NodeStarted`` / ``NodeFinished`` events for the SSE
   bridge.
5. Returns ``{"tool_results": [ToolResult(...)]}`` — the additive
   reducer merges across sibling lanes back to state.

On error: writes a ``ToolResult`` with ``error="…"`` rather than
re-raising. The researcher's round-2 prompt sees the tombstone
and re-plans around the gap. Same tombstone shape pattern as the
researcher node's lane-failure path.

Pure-Python; ``run_loop`` and the recorder are duck-typed so unit
tests pass stubs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from consultants.engine.state_v2 import ToolPlanItem, ToolResult

log = logging.getLogger("consultants.engine.tool_executor")


# ============================================================== #
# Prompt builder — kept short; the specialist model just needs
# the intent + the project sandbox seed.
# ============================================================== #

TOOL_EXECUTOR_SYSTEM = (
    "ROLE: tool_executor. You execute one specific intent from the "
    "researcher's tool_plan by calling the project's tools. The "
    "researcher decides WHAT to investigate; you decide HOW. Use "
    "the smallest sequence of tool calls that satisfies the intent. "
    "Avoid duplicate calls — if you already have the file's content "
    "from a prior read_file, don't re-read it. When the intent is "
    "satisfied, produce a SHORT final message (5-15 lines) that "
    "summarizes the evidence you gathered, with `path:line` "
    "citations for any code findings. The researcher will weave "
    "your output into the round-2 report — do NOT write the "
    "research report yourself."
)


def build_tool_executor_messages(item: ToolPlanItem,
                                  grounding_msgs: list[dict],
                                  *,
                                  question: str = "") -> list[dict]:
    """Build the conversation seed for one tool_executor lane.

    Layout: grounding first (anchor files + structure map), then
    the system prompt, then a user message that names the intent +
    the researcher's ``why`` reason + the parent question for
    context. Mirrors ``build_researcher_messages`` to keep audit
    trails comparable.
    """
    msgs: list[dict] = list(grounding_msgs or [])
    msgs.append({"role": "system", "content": TOOL_EXECUTOR_SYSTEM})
    parts = []
    if question:
        parts.append(f"PARENT QUESTION (context only):\n{question.strip()}")
    parts.append(f"\nINTENT TO EXECUTE:\n{item.intent.strip()}")
    if item.why and item.why.strip():
        parts.append(f"\nWHY (researcher's reason):\n{item.why.strip()}")
    if item.suggested_tools:
        parts.append(
            "\nSUGGESTED TOOLS (advisory; pick what fits): "
            + ", ".join(item.suggested_tools)
        )
    msgs.append({"role": "user", "content": "\n".join(parts)})
    return msgs


# ============================================================== #
# Event helpers — same defensive-no-op pattern as council._emit_*
# ============================================================== #

def _emit_started(role: str, *, round: int, lane_idx: Optional[int],
                  model: Optional[str]) -> None:
    try:
        from consultants.engine.events import NodeStarted, emit
        emit(NodeStarted(role=role, round=round, lane_idx=lane_idx,
                          model=model))
    except Exception:  # pragma: no cover
        log.exception("emit NodeStarted raised; ignored")


def _emit_finished(role: str, *, round: int, lane_idx: Optional[int],
                   duration_ms: int, ok: bool,
                   error: Optional[str] = None) -> None:
    try:
        from consultants.engine.events import NodeFinished, emit
        emit(NodeFinished(role=role, round=round, lane_idx=lane_idx,
                           duration_ms=duration_ms,
                           ok=ok, error=error))
    except Exception:  # pragma: no cover
        log.exception("emit NodeFinished raised; ignored")


def _emit_tool_call(role: str, *, round: int, lane_idx: Optional[int],
                    tool: str, args_preview: str, output_preview: str,
                    duration_ms: int,
                    error: Optional[str] = None) -> None:
    try:
        from consultants.engine.events import ToolCall, emit
        emit(ToolCall(role=role, round=round, lane_idx=lane_idx,
                       tool=tool, args_preview=args_preview[:200],
                       output_preview=output_preview[:200],
                       duration_ms=duration_ms, error=error))
    except Exception:  # pragma: no cover
        log.exception("emit ToolCall raised; ignored")


# ============================================================== #
# Node
# ============================================================== #

def tool_executor_node(state: dict,
                       *,
                       chat_client,
                       tool_executor,
                       tool_specs: list[dict],
                       grounding_msgs: list[dict],
                       model: str,
                       cwd: str,
                       think: Any = False,
                       loop_runner=None,
                       recorder=None) -> dict:
    """Execute one ``ToolPlanItem`` from the per-lane state slice.

    Returns a state delta merging into ``tool_results``. Never
    raises — failures land as a tombstone ``ToolResult`` with the
    ``error`` field populated.

    Parameters mirror ``researcher_node`` so the runner can wire
    the same deps (chat_client per role, tool_executor callable,
    tool_specs, grounding_msgs, recorder) without divergent
    plumbing. ``loop_runner`` defaults to
    ``claude_hooks.agent_loop.runner.run_loop`` (lazy-imported so
    the module loads in envs where claude_hooks isn't on the path).
    """
    item: Optional[ToolPlanItem] = state.get("tool_plan_item")
    lane_idx = state.get("lane_idx")
    # #103 proper composition: per-lane Send carries the researcher
    # lane that emitted the plan item. Stamped on every emitted
    # ToolResult so the round-filter can match results back to the
    # parent researcher lane at REPORT-mode re-entry. ``None`` on
    # the single-researcher path (base tiers + xtier <FANOUT_MIN
    # short-circuit).
    parent_lane_idx = state.get("parent_lane_idx")
    parent_round = (
        int(item.parent_round) if item is not None else 1
    )
    t0 = time.monotonic()

    # Defensive: a Send without a plan item is a graph-wiring bug;
    # surface it as an explicit tombstone rather than crashing.
    if item is None or not isinstance(item, ToolPlanItem) \
            or not (item.intent or "").strip():
        _emit_started("tool_executor", round=parent_round,
                       lane_idx=lane_idx, model=model)
        _emit_finished(
            "tool_executor", round=parent_round, lane_idx=lane_idx,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error="missing tool_plan_item",
        )
        return {
            "tool_results": [ToolResult(
                intent="(missing)",
                content="",
                error="tool_executor lane invoked without a "
                       "tool_plan_item on per-lane state",
                lane_idx=lane_idx,
                parent_lane_idx=parent_lane_idx,
                parent_round=parent_round,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )],
        }

    _emit_started("tool_executor", round=parent_round,
                   lane_idx=lane_idx, model=model)
    if recorder is not None:
        try:
            recorder.record_node(
                role="tool_executor", kind="node_enter",
                round=parent_round, lane_idx=lane_idx,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")

    # Lazy import keeps the module importable in envs without
    # claude_hooks (tests pass a stub loop_runner so this is
    # skipped). Same pattern as researcher_node.
    if loop_runner is None:
        try:
            from claude_hooks.agent_loop.runner import run_loop  # lazy
            loop_runner = run_loop
        except Exception:  # pragma: no cover — defensive
            log.exception("could not import run_loop; aborting lane")
            dt_ms = int((time.monotonic() - t0) * 1000)
            _emit_finished(
                "tool_executor", round=parent_round,
                lane_idx=lane_idx,
                duration_ms=dt_ms, ok=False,
                error="run_loop import failed",
            )
            return {
                "tool_results": [ToolResult(
                    intent=item.intent, content="",
                    error="agent_loop.runner.run_loop not available",
                    lane_idx=lane_idx,
                    parent_lane_idx=parent_lane_idx,
                    parent_round=parent_round,
                    duration_ms=dt_ms,
                )],
            }

    try:
        from claude_hooks.agent_loop.runner import LoopConfig
    except Exception:  # pragma: no cover
        LoopConfig = None  # type: ignore[assignment]

    msgs = build_tool_executor_messages(
        item, grounding_msgs, question=state.get("question") or "",
    )
    payload = {"model": model, "messages": msgs, "stream": False}

    cfg = None
    if LoopConfig is not None:
        cfg = LoopConfig(
            # Tool-call lanes are mechanical, not exploratory —
            # cap iterations tight. The default is what
            # researcher_node uses at effort=medium; M11c will
            # tune this per-model.
            max_iterations=6,
            force_answer_after=5,
            tools_available=True,
            think=think,
            force_first_tool_call=False,
        )

    # Recorder callbacks — exactly the researcher pattern, but
    # role="tool_executor". The post-mortem audit can now SQL by
    # role to see who called which tools.
    on_iter_cb = None
    on_tool_cb = None
    tools_called: list[str] = []
    if recorder is not None:
        def _on_iter(_idx: int, req: dict, resp: dict, dt_ms: int) -> None:
            pt, ct = _usage_from(resp)
            recorder.record_llm(
                role="tool_executor", round=parent_round,
                lane_idx=lane_idx, model=model,
                request=req, response=resp,
                prompt_tokens=pt, completion_tokens=ct,
                duration_ms=dt_ms,
            )

        def _on_tool(name: str, args: str, output: str,
                     dt_ms: int, err: Optional[str]) -> None:
            recorder.record_tool(
                role="tool_executor", round=parent_round,
                lane_idx=lane_idx, tool=name, args=args,
                output=output, duration_ms=dt_ms, error=err,
            )
            tools_called.append(name)
            _emit_tool_call(
                "tool_executor", round=parent_round,
                lane_idx=lane_idx, tool=name,
                args_preview=args, output_preview=output,
                duration_ms=dt_ms, error=err,
            )
        on_iter_cb = _on_iter
        on_tool_cb = _on_tool
    else:
        def _on_tool_norec(name: str, args: str, output: str,
                           dt_ms: int, err: Optional[str]) -> None:
            tools_called.append(name)
            _emit_tool_call(
                "tool_executor", round=parent_round,
                lane_idx=lane_idx, tool=name,
                args_preview=args, output_preview=output,
                duration_ms=dt_ms, error=err,
            )
        on_tool_cb = _on_tool_norec

    # Match ``run_loop``'s real signature: positional payload + cwd,
    # then kw-only ``chat_fn`` (NOT chat_client) and ``on_iter``
    # (NOT on_iteration). The wrapper ``chat_fn`` delegates to the
    # role's ChatClient, mirroring how researcher_node bridges the
    # two (and giving us a clean hook point if M3-style stall
    # protection lands here too).
    def _chat_fn(_payload: dict) -> dict:
        return chat_client.chat(_payload)

    try:
        result = loop_runner(
            payload,
            cwd,
            config=cfg,
            tool_specs=tool_specs,
            chat_fn=_chat_fn,
            tool_executor=tool_executor,
            on_iter=on_iter_cb,
            on_tool=on_tool_cb,
        )
    except Exception as e:
        log.exception("tool_executor lane %s failed: %s",
                       lane_idx, e)
        dt_ms = int((time.monotonic() - t0) * 1000)
        _emit_finished(
            "tool_executor", round=parent_round,
            lane_idx=lane_idx, duration_ms=dt_ms,
            ok=False, error=f"{type(e).__name__}: {e}",
        )
        return {
            "tool_results": [ToolResult(
                intent=item.intent, content="",
                tools_called=list(tools_called),
                error=f"{type(e).__name__}: {e}",
                lane_idx=lane_idx,
                parent_lane_idx=parent_lane_idx,
                parent_round=parent_round,
                duration_ms=dt_ms,
            )],
        }

    final_text = _extract_final_content(result)
    dt_ms = int((time.monotonic() - t0) * 1000)
    if recorder is not None:
        try:
            recorder.record_node(
                role="tool_executor", kind="node_exit",
                round=parent_round, lane_idx=lane_idx,
                duration_ms=dt_ms,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    _emit_finished("tool_executor", round=parent_round,
                    lane_idx=lane_idx,
                    duration_ms=dt_ms, ok=True)

    return {
        "tool_results": [ToolResult(
            intent=item.intent,
            content=final_text,
            transcript_summary=_summarize_tools(tools_called),
            tools_called=list(tools_called),
            lane_idx=lane_idx,
            parent_lane_idx=parent_lane_idx,
            parent_round=parent_round,
            duration_ms=dt_ms,
        )],
    }


# ============================================================== #
# Internal helpers
# ============================================================== #

def _usage_from(resp: dict) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from a chat
    response shape. Robust against either OpenAI's
    ``usage.prompt_tokens`` or Ollama's
    ``prompt_eval_count`` field name.
    """
    if not isinstance(resp, dict):
        return (0, 0)
    usage = resp.get("usage") or {}
    pt = (
        usage.get("prompt_tokens")
        or resp.get("prompt_eval_count")
        or 0
    )
    ct = (
        usage.get("completion_tokens")
        or resp.get("eval_count")
        or 0
    )
    try:
        return (int(pt), int(ct))
    except (TypeError, ValueError):
        return (0, 0)


def _extract_final_content(result: Any) -> str:
    """Pull the final assistant text out of ``run_loop``'s return.

    ``run_loop`` returns a dict shaped like::

        {"final": {"choices": [{"message": {"content": "..."}}]}, ...}

    Tolerant of partial shapes — if the structure is unfamiliar we
    return the stringified result so the researcher's prompt at
    least sees *something* rather than empty.
    """
    if isinstance(result, dict):
        final = result.get("final") or result
        if isinstance(final, dict):
            choices = final.get("choices") or []
            if choices and isinstance(choices, list):
                msg = choices[0].get("message") if isinstance(
                    choices[0], dict) else None
                if isinstance(msg, dict):
                    content = msg.get("content") or ""
                    if isinstance(content, str):
                        return content
        # Some run_loop variants surface "content" directly on the
        # top-level result.
        content = final.get("content") if isinstance(final, dict) else None
        if isinstance(content, str):
            return content
    if isinstance(result, str):
        return result
    return str(result) if result is not None else ""


def _summarize_tools(names: list[str]) -> str:
    """Compact one-liner of which tools ran, in order, with counts
    for duplicates. ``["read_file", "grep", "read_file"]`` →
    ``"read_file ×2, grep ×1"``. Used by the researcher's round-2
    prompt to gauge whether the lane chained tools sensibly.
    """
    if not names:
        return "(no tools called)"
    counts: dict[str, int] = {}
    order: list[str] = []
    for n in names:
        if n not in counts:
            order.append(n)
        counts[n] = counts.get(n, 0) + 1
    return ", ".join(f"{n} ×{counts[n]}" for n in order)


# ============================================================== #
# Tool-plan parser (extracts ``tool_plan`` from researcher output)
# ============================================================== #

TOOL_PLAN_FENCE_RE = None  # Lazy-compiled in _import_re

def _import_re():
    """Cache the regex import so the module loads without ``re``
    pulled into the namespace at top level (cheap micro-opt to
    keep the importer cold-start tight)."""
    global TOOL_PLAN_FENCE_RE
    import re
    if TOOL_PLAN_FENCE_RE is None:
        TOOL_PLAN_FENCE_RE = re.compile(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            re.DOTALL | re.IGNORECASE,
        )
    return re, TOOL_PLAN_FENCE_RE


def parse_tool_plan(text: str, *,
                    parent_round: int = 1,
                    parent_lane_idx: Optional[int] = None,
                    ) -> list[ToolPlanItem]:
    """Extract ``ToolPlanItem`` entries from the researcher's
    PLAN-mode response.

    Accepts three input shapes (tolerance is the contract — models
    don't all fence JSON the same way):

    1. **Fenced JSON**: ``\\`\\`\\`json\\n{"tool_plan": [...]}\\n\\`\\`\\``.
       Extract via regex.
    2. **Bare JSON object**: response starts with ``{`` — parse the
       whole thing.
    3. **Bare JSON array**: response is ``[{...}, {...}]`` — wrap.

    The JSON shape we accept::

        {"tool_plan": [
            {"intent": "...", "why": "...",
             "suggested_tools": ["grep"]},
            ...
        ]}

    OR a bare list of those objects (the wrapper is convenience).

    Items missing ``intent`` are skipped silently — they're a sign
    the model malformed one row, not the whole plan. Empty input
    returns an empty list (caller decides how to handle).

    ``parent_round`` stamps every emitted item; the graph wires this
    from the researcher's ``this_round`` so the M6 round-filter
    works downstream.

    ``parent_lane_idx`` is the #103 proper-composition stamp: the
    researcher_node passes its own ``state["lane_idx"]`` so each
    emitted item records WHICH researcher lane produced it. The
    downstream round-filter ``tool_results_for_round(state, round,
    parent_lane_idx=...)`` reads this field to keep x-tier sibling
    lanes from cross-pollinating each other's REPORT-mode prompts.
    ``None`` on the single-researcher path (base tiers).
    """
    import json

    if not text or not isinstance(text, str):
        return []
    body = text.strip()
    if not body:
        return []

    # Try fenced first.
    re_mod, fence_re = _import_re()
    candidates: list[str] = []
    for m in fence_re.finditer(body):
        candidates.append(m.group(1))
    # If no fence match, try the bare body.
    if not candidates:
        candidates.append(body)

    for raw in candidates:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        items: list = []
        if isinstance(data, dict) and "tool_plan" in data:
            maybe = data["tool_plan"]
            if isinstance(maybe, list):
                items = maybe
        elif isinstance(data, list):
            items = data
        else:
            continue
        out: list[ToolPlanItem] = []
        for idx, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                continue
            intent = (raw_item.get("intent") or "").strip()
            if not intent:
                continue
            why = (raw_item.get("why") or "").strip()
            suggested = raw_item.get("suggested_tools") or []
            if not isinstance(suggested, list):
                suggested = []
            else:
                suggested = [
                    str(s) for s in suggested if isinstance(s, str)
                ]
            out.append(ToolPlanItem(
                intent=intent, why=why,
                lane_idx=idx,
                parent_lane_idx=parent_lane_idx,
                parent_round=int(parent_round),
                suggested_tools=suggested,
            ))
        if out:
            return out
    return []


# ============================================================== #
# Researcher PLAN-mode prompt fragment
# ============================================================== #

RESEARCHER_PLAN_MODE_BLOCK = (
    "\n\nTOOL-EXECUTOR MODE — DO NOT CALL TOOLS YOURSELF. Instead, "
    "decompose the plan above into a short list of focused tool-use "
    "intents that a specialist model will execute in parallel. "
    "Output ONLY a JSON object on a single fenced ```json block at "
    "the end of your response, shaped:\n\n"
    "```json\n"
    "{\"tool_plan\": ["
    "{\"intent\": \"<one-sentence what to find/check>\", "
    "\"why\": \"<one-line reason>\", "
    "\"suggested_tools\": [\"<tool>\", ...]}"
    "]}\n"
    "```\n\n"
    "Rules: 1-6 items per plan; each intent must be self-contained "
    "(the executor will not see your other intents); prefer "
    "specifics ('find function handle_payment in /api/...') over "
    "vagueness ('look around'). Available tools: survey_project, "
    "list_files, read_file, glob, grep, recall_memory. Suggested-"
    "tools is advisory only."
)


def build_tool_plan_user_appendix(prior_results: list[ToolResult]) -> str:
    """Build the 'PRIOR TOOL RESULTS' block the researcher's
    REPORT-mode prompt appends after the v1 user message.

    Renders each ToolResult as: intent + tools_called summary +
    truncated content. Tombstones (error set) are rendered in a
    compact "(intent: '...' FAILED: error)" form so the model
    knows the gap exists and re-plans around it.

    M14 follow-up (2026-05-18): closes with an explicit "write a
    research report NOW, do NOT emit another tool_plan" instruction
    so the model doesn't re-enter PLAN mode when peer findings or
    other prompt context could make re-planning seem like the
    right move. Originally the appendix dumped the tool results
    and stopped — the absence of an instruction was load-bearing
    in M13 (the model defaulted to free-text reports) but broke
    when the M14 default-on store surfaced peer findings.
    """
    if not prior_results:
        return ""
    lines = ["PRIOR TOOL RESULTS (from the tool_executor lanes):"]
    for i, r in enumerate(prior_results, start=1):
        if r.error:
            lines.append(
                f"\n{i}. (intent: {r.intent!r} FAILED: {r.error})"
            )
            continue
        # Truncate content to keep round-2 prompt bounded; the
        # full text is in the recorder for post-mortems.
        snippet = (r.content or "").strip()
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "... [truncated]"
        lines.append(
            f"\n{i}. INTENT: {r.intent}\n"
            f"   TOOLS: {r.transcript_summary or '(none)'}\n"
            f"   EVIDENCE:\n{snippet}"
        )
    lines.append(
        "\n\nREPORT NOW. The tool_executor lanes have run the calls "
        "you planned — synthesize the evidence above into a "
        "research report. Do NOT emit another "
        "``{\"tool_plan\": [...]}`` block. Do NOT request more "
        "tool calls. Write a plain-prose finding citing the "
        "evidence (path:line where applicable). If evidence is "
        "insufficient, say so explicitly — the critic decides "
        "whether more research rounds are warranted.\n\n"
        "DO NOT FABRICATE SOURCE LISTINGS. The EVIDENCE blocks "
        "above are the ONLY file content you may quote, paraphrase, "
        "or cite. Do NOT emit a numbered code block (e.g. lines like "
        "``21  import foo``) for any file unless that exact block "
        "is in an EVIDENCE block. Do NOT invent fields, function "
        "names, table names, constants, or import lines that are "
        "not in the evidence — even if they sound plausible. Do "
        "NOT cite ``path:line`` for a file absent from EVIDENCE. "
        "Ground every claim in the evidence above or report "
        "'evidence insufficient'."
    )
    return "\n".join(lines)


__all__ = [
    "RESEARCHER_PLAN_MODE_BLOCK",
    "TOOL_EXECUTOR_SYSTEM",
    "build_tool_executor_messages",
    "build_tool_plan_user_appendix",
    "parse_tool_plan",
    "tool_executor_node",
]
