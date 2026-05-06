"""Tool-use agent loop runner.

A single ``run_loop`` function drives multi-round chat with a tool-capable
upstream model (Ollama, OpenAI, anything that returns OpenAI-style
``choices[*].message.tool_calls``). The runner is transport-agnostic: the
caller passes a ``chat_fn(payload) -> response_dict`` and a
``tool_executor(name, args_json_str, cwd) -> output_str`` so this module
has no hardcoded HTTP client or tool registry.

The runner deliberately preserves all caliber-grounding-proxy quirks
(force-first-tool-call, force-answer-after, preseed survey, gemma-style
tool burst dedup + cap) because both consumers want them — caliber for
gemma4-98e + the advisor when the configured advisor model is small or
sloppy. Quirks are gated by ``LoopConfig`` flags so a clean caller can
opt out.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("claude_hooks.agent_loop")


DEFAULT_MAX_ITERATIONS = 35
DEFAULT_FORCE_ANSWER_AFTER = 5
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 8
DEFAULT_FORCE_FIRST_RETRY_MESSAGE = (
    "You skipped tool use on your last turn. Re-do "
    "your previous turn with tools: call "
    "`survey_project` (no arguments) first, then any "
    "of `read_file`, `grep`, `glob`, `list_files` you "
    "need to verify references. THEN, on the same "
    "task, emit the EXACT response format the "
    "original instruction asked for (the structured "
    "JSON object with all required fields — not a "
    "summary, not a question, not a status update). "
    "Do not ask what to do next; complete the "
    "original task with the tool grounding you "
    "skipped."
)


@dataclass
class LoopConfig:
    """All behavior knobs for ``run_loop``. Defaults preserve the
    caliber-grounding-proxy behavior; pass a tighter config for cleaner
    callers (e.g. /get-advice)."""

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    force_answer_after: int = DEFAULT_FORCE_ANSWER_AFTER  # 0 = never strip
    max_tool_calls_per_turn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN

    tools_available: bool = True

    think: Any = False  # False | True | "low" | "medium" | "high"
    num_ctx_override: Optional[int] = None
    model_override: Optional[str] = None

    force_first_tool_call: bool = True
    force_first_retry_enabled: bool = True
    force_first_retry_message: str = DEFAULT_FORCE_FIRST_RETRY_MESSAGE


# Type aliases.
ToolExecutor = Callable[[str, str, str], str]
ChatFn = Callable[[dict], dict]
# A preseed builder returns either:
#   None  — no preseed
#   ([assistant_msg, tool_msg], dedup_key, dedup_content)
PreseedBuilder = Callable[[str], Optional[tuple[list[dict], str, str]]]


def merge_tools(existing: Optional[list[dict]],
                our_specs: list[dict]) -> list[dict]:
    """Combine caller-supplied tool list with the runner's own.
    Caller tools win on name collision so the runner stays out of the
    way if the caller already injected the same names."""
    if not existing:
        return list(our_specs)
    existing_names = {
        t.get("function", {}).get("name") for t in existing
        if isinstance(t, dict)
    }
    return list(existing) + [
        s for s in our_specs
        if s["function"]["name"] not in existing_names
    ]


def execute_tool_calls(tool_calls: list[dict], cwd: str,
                       seen: dict[str, str],
                       tool_executor: ToolExecutor) -> list[dict]:
    """Run each tool and return ``role: tool`` messages.

    Duplicate (name+args) calls within a single agent loop return a
    short stub pointing the model back at the prior result. Models
    (notably gemma4-98e) will otherwise loop on the same tool call
    dozens of times, exploding context and wall time.
    """
    results: list[dict] = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_str = fn.get("arguments") or "{}"
        if isinstance(args_str, dict):
            args_str = json.dumps(args_str)
        key = f"{name}|{args_str}"
        if key in seen:
            output = (
                f"(duplicate: you already called {name}({args_str[:80]}). "
                "Use that prior result and continue. Do not repeat this call.)"
            )
            log.info("tool %s(%s) -> DEDUP stub", name, args_str[:80])
        else:
            t0 = time.monotonic()
            output = tool_executor(name, args_str, cwd)
            dt_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "tool %s(%s) -> %d chars in %d ms",
                name, args_str[:80], len(output), dt_ms,
            )
            seen[key] = output
        results.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "name": name,
            "content": output,
        })
    return results


def run_loop(
    payload: dict,
    cwd: str,
    *,
    config: LoopConfig,
    tool_specs: list[dict],
    chat_fn: ChatFn,
    tool_executor: ToolExecutor,
    preseed_builder: Optional[PreseedBuilder] = None,
) -> dict:
    """Drive the tool-use loop until the model stops calling tools or
    the iteration cap is hit. Returns the final upstream response dict.

    The caller is responsible for prepending grounding system messages
    onto ``payload["messages"]`` BEFORE calling this. The runner only
    handles the loop, not grounding assembly.
    """
    payload = dict(payload)

    if config.model_override:
        payload["model"] = config.model_override

    # Map ``think`` to both ``think`` (Ollama native) and
    # ``reasoning_effort`` (OpenAI-compat) so we work against either path.
    think = config.think
    if think is False:
        payload["think"] = False
        payload["reasoning_effort"] = "none"
    elif think is True:
        payload["think"] = True
    else:
        payload["think"] = think
        payload["reasoning_effort"] = think

    # Optional context-window cap. Goes under ``options.num_ctx``. Don't
    # clobber if the caller already set num_ctx; only cap when absent.
    if config.num_ctx_override is not None:
        opts = dict(payload.get("options") or {})
        if "num_ctx" not in opts:
            opts["num_ctx"] = config.num_ctx_override
            payload["options"] = opts

    if config.tools_available:
        payload["tools"] = merge_tools(payload.get("tools"), tool_specs)
    else:
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    # Force non-streaming inside the loop so we can inspect tool_calls
    # cleanly. The caller decides how to deliver the final turn.
    payload.pop("stream", None)

    final: dict[str, Any] = {}
    tools_stripped = False
    has_called_tool = False
    force_first = (config.force_first_tool_call
                   if config.tools_available else False)
    force_first_retried = False
    seen_calls: dict[str, str] = {}

    if config.tools_available and preseed_builder is not None:
        preseed = preseed_builder(cwd)
        if preseed is not None:
            preseed_msgs, dedup_key, dedup_content = preseed
            payload["messages"] = list(payload["messages"]) + preseed_msgs
            seen_calls[dedup_key] = dedup_content
            has_called_tool = True
            log.info(
                "preseed: injected %d synthetic message(s) (%d chars), "
                "force_first now dormant",
                len(preseed_msgs), len(dedup_content),
            )

    force_after = (config.force_answer_after
                   if config.tools_available else 0)
    cap = config.max_tool_calls_per_turn

    for i in range(config.max_iterations):
        if (config.tools_available and force_after > 0 and i >= force_after
                and not tools_stripped):
            log.info(
                "force-answer: stripping tools at iter %d (after=%d)",
                i, force_after,
            )
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            tools_stripped = True
        if config.tools_available and not tools_stripped:
            if force_first and not has_called_tool:
                payload["tool_choice"] = "required"
            else:
                payload["tool_choice"] = "auto"
        final = chat_fn(payload)
        choices = final.get("choices") or []
        if not choices:
            log.warning("upstream returned empty choices on iter %d", i)
            break
        choice = choices[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        finish_reason = choice.get("finish_reason")
        log.debug(
            "iter %d: finish=%s, tool_calls=%d, content_len=%d",
            i, finish_reason, len(tool_calls),
            len((msg.get("content") or "")),
        )
        if finish_reason != "tool_calls" or not tool_calls:
            if (force_first and not has_called_tool
                    and config.tools_available
                    and not tools_stripped
                    and not force_first_retried
                    and config.force_first_retry_enabled):
                log.info(
                    "force_first: iter %d returned no tool_calls; "
                    "injecting corrective user message and retrying",
                    i,
                )
                force_first_retried = True
                prior_content = msg.get("content") or ""
                payload = dict(payload)
                payload["messages"] = list(payload["messages"]) + [
                    {"role": "assistant", "content": prior_content},
                    {
                        "role": "user",
                        "content": config.force_first_retry_message,
                    },
                ]
                continue
            break
        has_called_tool = True

        # Collapse identical tool_calls within one assistant turn, then
        # cap. Preserves distinct calls; suppresses gemma-style bursts.
        original_len = len(tool_calls)
        uniq: list[dict] = []
        seen_sigs: set[str] = set()
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            if isinstance(args, dict):
                args = json.dumps(args, sort_keys=True)
            sig = f"{fn.get('name','')}|{args}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            uniq.append(tc)
        if len(uniq) > cap:
            uniq = uniq[:cap]
        if len(uniq) != original_len:
            log.info(
                "tool_calls %d -> %d unique (cap=%d) on iter %d",
                original_len, len(uniq), cap, i,
            )
        tool_calls = uniq

        clean_msg = dict(msg)
        clean_msg["content"] = None
        clean_msg["tool_calls"] = tool_calls
        for k in ("thinking", "reasoning", "reasoning_content"):
            clean_msg.pop(k, None)
        payload["messages"] = list(payload["messages"]) + [clean_msg]
        payload["messages"].extend(
            execute_tool_calls(tool_calls, cwd, seen_calls, tool_executor)
        )
    else:
        log.warning("agent loop hit max_iterations=%d", config.max_iterations)
    return final
