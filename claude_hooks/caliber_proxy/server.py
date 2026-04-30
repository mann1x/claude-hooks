"""OpenAI-compatible HTTP server that augments Ollama with project
grounding. Runs the tool-call agent loop locally (read_file, grep, glob,
list_files) so gemma4-98e (or any tool-capable Ollama model) can
cite real `path:line` references in caliber's init / regenerate output.

Architecture:

::

    caliber ──POST /v1/chat/completions──► this proxy
                                              │
                                              ├─ prepend grounding system + anchors
                                              ├─ inject tool specs
                                              ▼
                                           Ollama
                                              │
                                              ▼
                              if finish_reason == "tool_calls":
                                  execute tools locally, loop
                              else:
                                  mirror back to caliber

The proxy binds to ``127.0.0.1:38090`` by default (local-only; another
host can't see the project files anyway). Change via
``CALIBER_GROUNDING_HOST`` / ``CALIBER_GROUNDING_PORT``.

Exit codes:
    0 on clean SIGTERM / SIGINT; non-zero on bind / config failure.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from claude_hooks.caliber_proxy import ollama, prompt, recall, tools

log = logging.getLogger("claude_hooks.caliber_proxy.server")

# Max number of tool-call rounds before we give up and let the model
# produce whatever it has. User-configured at 35 (decision #3).
DEFAULT_MAX_ITERATIONS = 35
# After this many tool rounds we strip ``tools`` / ``tool_choice`` from
# the payload so the model is forced to finalise its answer instead of
# looping on tool calls. Some templates (gemma4-98e) otherwise keep
# emitting partial JSON fragments as content and never commit to a
# complete response. Set to 0 to keep tools available for every round.
DEFAULT_FORCE_ANSWER_AFTER = 5
# Cap how many tool_calls we will execute from a single assistant turn.
# Models with a looping failure mode (gemma4-98e has been observed
# emitting 800+ calls in one response) otherwise bloat context into
# uselessness. The remaining calls are silently dropped — the model
# sees the first N results and can request more next round.
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 8
# How often to emit an SSE keep-alive comment while the agent loop is
# running. The OpenAI Node SDK (used by caliber) wraps undici, whose
# bodyTimeout default is 5 minutes — if no bytes arrive on the response
# stream in that window, the request is aborted with "Request timed
# out." Caliber treats that as a fatal error and surfaces the audit
# response as null. For 140k-token audit prompts on local Ollama,
# gemma can take 4-6 minutes; the proxy used to buffer the entire
# response until the agent loop returned, which busted the bodyTimeout.
# Sending a ``: heartbeat`` comment every 20s resets the timer without
# polluting caliber's stream-parser (it only inspects ``data:`` lines).
DEFAULT_SSE_HEARTBEAT_SECONDS = 20.0
# Force the first iteration of the agent loop to use ``tool_choice``
# "required" so the model physically cannot return without invoking a
# tool. Gemma4-98e ignores even strongly-worded "MANDATORY tool use"
# language in the system prompt and returns ungrounded JSON anyway,
# tanking caliber's grounding score. Forcing the first call eliminates
# the "skipped survey, hallucinated paths" failure mode. Drops back to
# "auto" once the model has called at least one tool, so subsequent
# iterations remain free to finalise the answer.
DEFAULT_FORCE_FIRST_TOOL_CALL = True
# Pre-inject a synthetic ``survey_project`` tool call + result before the
# model's first turn. Off by default: empirically the shortcut backfires
# during the long audit/CLAUDE.md generation phase. The model sees a
# completed tool result and treats the work as done, returning a short
# "status: ready" summary instead of generating the rich rubric-targeted
# JSON caliber expects (observed at 561 chars vs ~4-5 KB on a clean
# force_first run). Skill-generation phases are short enough that the
# shortcut is harmless, but caliber doesn't separate the phases on the
# wire so we can't enable selectively. Kept opt-in via env for further
# experimentation. When on, also marks ``has_called_tool=True`` so the
# force_first retry path stays dormant — preseed and force_first are
# alternate strategies for the same problem (gemma skipping tools).
DEFAULT_PRESEED_SURVEY = False
PRESEED_SURVEY_TOOL_CALL_ID = "preseed_survey_0"


def _max_iterations() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_MAX_ITER",
                                  str(DEFAULT_MAX_ITERATIONS)))
    except ValueError:
        return DEFAULT_MAX_ITERATIONS


def _num_ctx_override() -> Optional[int]:
    """Optional context-window cap for the upstream chat model.

    Caliber's grounding prompts can be 30-50k tokens; running a 256k-ctx
    Gemma4 variant means Ollama allocates KV cache for 256k whether
    caliber needs it or not (Ollama's KV cache is statically sized at
    model-load). On a 24 GB 3090 a 256k-ctx 19.9B Q6_K model claims
    ~22 GB, leaving no room for the embedding model to coexist. Cap
    via CALIBER_GROUNDING_NUM_CTX to free VRAM. Default unset = no
    override (whatever the model's tag specifies).
    """
    v = os.environ.get("CALIBER_GROUNDING_NUM_CTX", "").strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def _think_setting() -> Any:
    """Map ``CALIBER_GROUNDING_THINK`` to the ``think`` field Ollama
    accepts on /api/chat (and ``reasoning_effort`` on /v1 OpenAI-compat).

    Accepts: false (default), true, low, medium, high.

    Gemma4 has a well-known overthinking failure mode — left unconstrained
    it burns the whole context on `"Wait, let me re-read..."` loops and
    never produces the final answer. Caliber's task is structured
    output, not puzzle-solving, so we default to ``false`` (no thinking).
    """
    v = os.environ.get("CALIBER_GROUNDING_THINK", "medium").strip().lower()
    if v in ("", "false", "0", "no", "off"):
        return False
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("low", "medium", "high"):
        return v
    return False


def _model_override() -> Optional[str]:
    """If set, every upstream request uses this model regardless of the
    one the client sent. Caliber filters OpenAI model names through a
    hard allowlist (gpt-4*, o3-*) — this lets us accept any placeholder
    name from the client and always route to our real Ollama tag.
    """
    v = os.environ.get("CALIBER_GROUNDING_MODEL_OVERRIDE")
    return v.strip() if v and v.strip() else None


def _max_tool_calls_per_turn() -> int:
    try:
        return int(os.environ.get(
            "CALIBER_GROUNDING_MAX_TOOL_CALLS_PER_TURN",
            str(DEFAULT_MAX_TOOL_CALLS_PER_TURN),
        ))
    except ValueError:
        return DEFAULT_MAX_TOOL_CALLS_PER_TURN


def _force_answer_after() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_FORCE_ANSWER_AFTER",
                                  str(DEFAULT_FORCE_ANSWER_AFTER)))
    except ValueError:
        return DEFAULT_FORCE_ANSWER_AFTER


def _force_first_tool_call() -> bool:
    """When True (default), iteration 0 of the agent loop pins
    ``tool_choice`` to ``"required"`` so the model must invoke a tool
    before it can answer. Once the model has called at least one tool
    this turn, the proxy drops back to ``"auto"`` for subsequent
    iterations. Override via CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL=0
    if a model template doesn't honour OpenAI-style tool_choice values
    or you specifically want answer-without-tool turns to be allowed.
    """
    raw = os.environ.get("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL")
    if raw is None:
        return DEFAULT_FORCE_FIRST_TOOL_CALL
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _preseed_survey_enabled() -> bool:
    """When True (default), the agent loop pre-injects a synthetic
    ``survey_project`` assistant turn + tool result before the model's
    first iteration. Override via CALIBER_GROUNDING_PRESEED_SURVEY=0
    to fall back to the model calling it itself.
    """
    raw = os.environ.get("CALIBER_GROUNDING_PRESEED_SURVEY")
    if raw is None:
        return DEFAULT_PRESEED_SURVEY
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _build_preseed_survey_pair(cwd: str) -> Optional[tuple[list[dict], str]]:
    """Build the synthetic [assistant(tool_call), tool(result)] pair for
    a ``survey_project({})`` invocation. Returns ``(messages, content)``
    or None if the survey can't be produced (logged + treated as
    "preseed disabled this run"). The caller uses ``content`` to
    pre-populate the dedup table so a model retry of the same call is
    caught as a duplicate.
    """
    try:
        content = tools.execute("survey_project", "{}", cwd)
    except Exception as e:
        log.warning("preseed_survey: build failed: %s", e)
        return None
    if not content:
        return None
    assistant_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": PRESEED_SURVEY_TOOL_CALL_ID,
            "type": "function",
            "function": {"name": "survey_project", "arguments": "{}"},
        }],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": PRESEED_SURVEY_TOOL_CALL_ID,
        "name": "survey_project",
        "content": content,
    }
    return [assistant_msg, tool_msg], content


def _sse_heartbeat_seconds() -> float:
    raw = os.environ.get("CALIBER_GROUNDING_SSE_HEARTBEAT_SECONDS")
    if not raw:
        return DEFAULT_SSE_HEARTBEAT_SECONDS
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_SSE_HEARTBEAT_SECONDS
    return v if v > 0 else DEFAULT_SSE_HEARTBEAT_SECONDS


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _tools_enabled() -> bool:
    """When False, the proxy does NOT inject tool specs. Useful for models
    whose tool-use template is weak on multi-turn (e.g. custom Gemma
    variants without proper role-tool template support). Pre-stuffing
    becomes the only grounding mechanism in that mode.
    """
    return _env_flag("CALIBER_GROUNDING_TOOLS", default=True)


def _extended_sources_enabled() -> bool:
    """When True, pre-stuffing expands to include a curated subset of
    project source files (capped at 200 KB). Recommended when
    ``CALIBER_GROUNDING_TOOLS=0`` so the model still has deep context."""
    return _env_flag("CALIBER_GROUNDING_EXTENDED", default=False)


def _cwd_for_request() -> str:
    """The proxy grounds against the directory it was launched in. If
    caliber is invoked from /srv/.../claude-hooks, the proxy must have
    been started in that same directory (via caliber-smart or manually).
    We *don't* accept a cwd override from the request body — that's a
    trivial prompt-injection vector to pivot into another project.
    """
    return os.environ.get("CALIBER_GROUNDING_CWD", os.getcwd())


# -- Agent loop ------------------------------------------------------- #
def _merge_tools(existing: Optional[list[dict]]) -> list[dict]:
    """Combine caliber's tool list (usually none) with ours. Caller tools
    win by name on collision — we stay out of the way if caliber ever
    starts sending its own."""
    our_specs = tools.openai_tool_specs()
    if not existing:
        return our_specs
    existing_names = {
        t.get("function", {}).get("name") for t in existing
        if isinstance(t, dict)
    }
    return list(existing) + [
        s for s in our_specs
        if s["function"]["name"] not in existing_names
    ]


def _inject_grounding(messages: list[dict], cwd: str,
                      tools_available: bool) -> list[dict]:
    """Prepend grounding system messages. We place them at the very front
    so they precede caliber's own system prompt — Ollama treats multiple
    system messages as additive, and the addendum is a hard constraint
    we want evaluated before caliber's task instructions.

    When the proxy is running without tool injection
    (``CALIBER_GROUNDING_TOOLS=0``), the addendum switches to a no-tools
    variant that tells the model all grounding is pre-loaded, and
    extended source pre-stuffing is forced on so there's more material
    for the model to ground against.
    """
    # Force extended sources whenever tools are off — the model has no
    # other way to reach source code beyond what we pre-stuff.
    extended = _extended_sources_enabled() or not tools_available
    grounding = prompt.build_grounding_messages(
        cwd,
        extended_sources=extended,
        tools_available=tools_available,
    )
    # Cross-provider recall: prepend a ``## Recalled memory`` system
    # message based on the latest user prompt. Mirrors the deterministic
    # recall claude-hooks runs inside Claude Code's UserPromptSubmit
    # hook. Empty list when disabled or when no hits come back.
    recall_msgs: list[dict] = []
    try:
        query = recall.latest_user_text(list(messages))
        if query:
            recall_msgs = recall.build_prepend_messages(query)
    except Exception as e:
        log.warning("recall: prepend failed: %s", e)
    return recall_msgs + grounding + list(messages)


def _execute_tool_calls(tool_calls: list[dict], cwd: str,
                         seen: dict[str, str]) -> list[dict]:
    """Run each tool and return the resulting ``role: tool`` messages.

    Duplicate (name+args) calls within a single agent loop return a
    short stub pointing the model back at the prior result. Models
    (notably gemma4-98e) will otherwise loop on the same tool call
    dozens of times, exploding context and wall time.
    """
    results = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_str = fn.get("arguments") or "{}"
        # Tool calls can come as dicts OR stringified JSON — handle both.
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
            output = tools.execute(name, args_str, cwd)
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


def run_agent_loop(payload: dict, cwd: str,
                   max_iterations: Optional[int] = None) -> dict:
    """Drive the tool-use loop until the model stops calling tools or the
    iteration cap is hit. Returns the final Ollama chat-completion JSON.
    """
    if max_iterations is None:
        max_iterations = _max_iterations()

    tools_available = _tools_enabled()
    force_after = _force_answer_after() if tools_available else 0
    override = _model_override()
    if override:
        payload = dict(payload)
        payload["model"] = override
    # Constrain thinking budget. Ollama accepts both the native ``think``
    # field and the OpenAI-style ``reasoning_effort`` — set both so we
    # work against either endpoint path.
    think = _think_setting()
    if think is False:
        payload["think"] = False
        payload["reasoning_effort"] = "none"
    elif think is True:
        payload["think"] = True
    else:
        # "low" | "medium" | "high"
        payload["think"] = think
        payload["reasoning_effort"] = think
    # Optional context-window cap. Goes under ``options.num_ctx`` —
    # Ollama's OpenAI-compat path forwards the ``options`` block to its
    # native runner. Don't clobber if caliber already set num_ctx; only
    # cap if no value is present.
    num_ctx = _num_ctx_override()
    if num_ctx is not None:
        opts = dict(payload.get("options") or {})
        if "num_ctx" not in opts:
            opts["num_ctx"] = num_ctx
            payload["options"] = opts
    messages = _inject_grounding(
        payload.get("messages") or [], cwd, tools_available=tools_available,
    )
    payload = dict(payload)
    payload["messages"] = messages
    if tools_available:
        payload["tools"] = _merge_tools(payload.get("tools"))
    else:
        # No tool injection — pre-stuffing is the only grounding. Strip
        # any caliber-supplied tool list too so the model doesn't try to
        # call something the proxy can't service.
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
    # Force non-streaming inside the loop so we can inspect tool_calls
    # cleanly. We'll decide whether to stream the final turn back to
    # the caller separately.
    payload.pop("stream", None)

    final: dict[str, Any] = {}
    tools_stripped = False
    has_called_tool = False
    force_first = _force_first_tool_call() if tools_available else False
    force_first_retried = False
    seen_calls: dict[str, str] = {}
    # Pre-inject the survey tool call + result so the model starts iter 0
    # with the project map already in context. Keeps force_first dormant
    # (we mark has_called_tool=True), and pre-populates the dedup table
    # so a redundant model-side survey_project({}) is caught as a dup.
    if tools_available and _preseed_survey_enabled():
        preseed = _build_preseed_survey_pair(cwd)
        if preseed is not None:
            preseed_msgs, preseed_content = preseed
            payload["messages"] = list(payload["messages"]) + preseed_msgs
            seen_calls["survey_project|{}"] = preseed_content
            has_called_tool = True
            log.info(
                "preseed_survey: injected synthetic survey_project tool "
                "result (%d chars), force_first now dormant",
                len(preseed_content),
            )
    for i in range(max_iterations):
        # After N tool rounds, strip the tool list so the model stops
        # looping on tool calls and commits to the final prose+JSON
        # answer. Caliber's parser expects: STATUS lines, EXPLAIN:, JSON.
        # Some templates (gemma4-98e) otherwise emit empty ```  fences.
        if (tools_available and force_after > 0 and i >= force_after
                and not tools_stripped):
            log.info(
                "force-answer: stripping tools at iter %d (after=%d)",
                i, force_after,
            )
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            tools_stripped = True
        # Set tool_choice based on whether we still want to force a
        # tool call. force_first pins iter 0 to "required" until the
        # model has actually invoked a tool, then we drop to "auto".
        # Skipped entirely once tools have been stripped.
        if tools_available and not tools_stripped:
            if force_first and not has_called_tool:
                payload["tool_choice"] = "required"
            else:
                payload["tool_choice"] = "auto"
        final = ollama.chat_completions(payload)
        choices = final.get("choices") or []
        if not choices:
            log.warning("ollama returned empty choices on iter %d", i)
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
            # Force-first retry: Ollama's native /api/chat drops the
            # tool_choice="required" hint we set on the OpenAI payload
            # (Ollama has no equivalent), so we re-prompt the model
            # explicitly when it skipped tools. Caps at ONE retry per
            # turn to avoid an infinite loop when the model genuinely
            # cannot/won't use tools — better to ship ungrounded JSON
            # than to time out.
            if (force_first and not has_called_tool and tools_available
                    and not tools_stripped and not force_first_retried):
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
                        "content": (
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
                        ),
                    },
                ]
                continue
            break
        # Tool was invoked — drop ``force_first`` for the next iteration.
        has_called_tool = True
        # Guard against runaway tool_call bursts. gemma4-98e has been
        # observed emitting hundreds of IDENTICAL calls in one response.
        # First collapse by unique (name, arguments), then cap. This
        # preserves distinct calls when the model legitimately asks
        # for several at once.
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
        cap = _max_tool_calls_per_turn()
        if len(uniq) > cap:
            uniq = uniq[:cap]
        if len(uniq) != original_len:
            log.info(
                "tool_calls %d -> %d unique (cap=%d) on iter %d",
                original_len, len(uniq), cap, i,
            )
        tool_calls = uniq
        # Normalise the assistant message: when tool_calls are present,
        # drop any ``content`` — models sometimes emit template
        # fragments or partial JSON alongside the structured call, and
        # re-sending that garbage on the next turn derails generation.
        clean_msg = dict(msg)
        clean_msg["content"] = None
        clean_msg["tool_calls"] = tool_calls  # already capped above
        # Gemma4 docs: "In multi-turn conversations, the historical model
        # output should only include the final response. Thoughts from
        # previous model turns must not be added before the next user
        # turn begins." Strip thinking/reasoning fields before echoing.
        for k in ("thinking", "reasoning", "reasoning_content"):
            clean_msg.pop(k, None)
        payload["messages"] = list(payload["messages"]) + [clean_msg]
        payload["messages"].extend(
            _execute_tool_calls(tool_calls, cwd, seen_calls)
        )
    else:
        log.warning("agent loop hit max_iterations=%d", max_iterations)
    sanitize_assistant_json(final)
    return final


def sanitize_assistant_json(result: dict) -> None:
    """Strip trailing junk after the last balanced ``}`` in the assistant
    message's content, in-place.

    Caliber's stream-parser uses a single ``JSON.parse`` on the captured
    region; one extra ``}`` at the end raises ``Extra data`` and the
    whole AgentSetup is discarded. Gemma4-98e in particular has been
    observed emitting one stray closing brace right before its natural
    stop token (the Modelfile stop sequences match ``<turn|>`` and
    ``<|tool_response>``, neither of which fires on a stray ``}``).

    We don't try to repair structurally invalid JSON — only the case
    where the JSON is fine but trailed by extra closing braces and / or
    code-fence terminators. If we can't find a parse-able object, the
    content is left untouched so the caller still sees the model's raw
    output for debugging.
    """
    try:
        choices = result.get("choices") or []
        if not choices:
            return
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return
        cleaned = _strip_trailing_json_garbage(content)
        if cleaned is not None and cleaned != content:
            msg["content"] = cleaned
            choices[0]["message"] = msg
            log.info(
                "sanitized %d chars of trailing junk after JSON in assistant content",
                len(content) - len(cleaned),
            )
    except Exception as e:
        # Sanitiser failure must never break the response.
        log.warning("sanitize_assistant_json: %s", e)


def _strip_trailing_json_garbage(content: str) -> Optional[str]:
    """Return ``content`` with any post-JSON garbage removed, or None
    if no top-level JSON document is found. Pure function — does not
    mutate the input.

    Handles two response shapes:

    * Object: STATUS preamble + EXPLAIN: prose + ``{"key": ...}``.
      Caliber's stream-parser uses ``/(?:^|\\n)\\s*(?:```json\\s*\\n\\s*)?\\{(?=\\s*")/``
      to locate the JSON start; we mirror it so the sanitizer agrees
      with caliber on which character begins the document.
    * Array: ``[{...}, {...}]`` returned by file-scoring / dismissal
      phases. Inner-element ``{`` must NOT be mistaken for the
      document start (an earlier version of this function did exactly
      that and truncated 75-element scoring arrays to their first
      entry). We require ``[`` to be at start-of-line preceded only
      by whitespace + optional code fence.

    If the matched JSON region parses cleanly we drop everything after
    its closing brace / bracket. If parsing fails, return None and let
    the caller see the raw model output for debugging.
    """
    import re
    object_match = re.search(
        r"(?:^|\n)\s*(?:```json\s*\n\s*)?\{(?=\s*\")", content
    )
    array_match = re.search(
        r"(?:^|\n)\s*(?:```json\s*\n\s*)?\[(?=\s*[{\"\d\-tfn\[])",
        content,
    )

    candidate = None
    if object_match and array_match:
        candidate = (
            object_match if object_match.start() <= array_match.start()
            else array_match
        )
    elif object_match:
        candidate = object_match
    elif array_match:
        candidate = array_match
    if candidate is None:
        return None

    # Locate the actual opening character (skip past the leading
    # whitespace / optional ```json fence the regex consumed).
    start = -1
    for ch_target in ("{", "["):
        idx = content.find(ch_target, candidate.start(), candidate.end())
        if idx >= 0 and (start < 0 or idx < start):
            start = idx
    if start < 0:
        return None
    open_ch = content[start]
    close_ch = "}" if open_ch == "{" else "]"

    prefix = content[:start]
    body = content[start:]
    depth = 0
    in_str = False
    esc = False
    end = -1
    j = 0
    while j < len(body):
        ch = body[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
        else:
            if ch == "\"":
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = j
                    break
        j += 1
    if end < 0:
        return None  # truncated; let caliber see the raw error
    json_text = body[: end + 1]
    try:
        json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return prefix + json_text


# -- HTTP server ------------------------------------------------------ #
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - - %s", self.address_string(), fmt % args)

    def _write_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _start_sse_stream(self) -> None:
        """Send the 200 OK + SSE headers immediately. Called BEFORE the
        agent loop runs (when ``stream: true``) so undici's headersTimeout
        is satisfied within seconds and we can interleave keep-alive
        comments while the agent loop blocks on Ollama.

        We send ``Connection: close`` (not the SSE-default keep-alive)
        because BaseHTTPRequestHandler runs HTTP/1.0 and will close the
        socket as soon as the handler returns regardless of header
        intent. With ``keep-alive``, undici (used by caliber's OpenAI
        Node SDK) waits for more data on the closed socket and then
        errors out ~5 min later with "terminated" / "other side
        closed", which caliber's stream-parser misclassifies as a
        transient error and retries the entire generation. Saying
        ``close`` up front aligns the client's expectation with the
        actual transport behaviour and lets the SDK exit cleanly the
        moment ``data: [DONE]`` arrives.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse_body(self, result: dict, lock: threading.Lock) -> None:
        """Write the actual chat.completion.chunk events for a completed
        agent-loop result. Headers must already have been sent via
        ``_start_sse_stream``. ``lock`` serialises writes against any
        heartbeat thread still running on this socket.
        """
        try:
            choice = (result.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            role = msg.get("role") or "assistant"
            finish = choice.get("finish_reason") or "stop"
            tool_calls = msg.get("tool_calls")
            usage = result.get("usage")
            model = result.get("model") or "caliber-grounding-proxy"
            created = result.get("created") or int(time.time())
            cid = result.get("id") or f"chatcmpl-{created}"

            def emit(delta: dict, finish_reason=None) -> None:
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }],
                }
                with lock:
                    self.wfile.write(b"data: ")
                    self.wfile.write(json.dumps(chunk).encode("utf-8"))
                    self.wfile.write(b"\n\n")
                    self.wfile.flush()

            emit({"role": role})
            if tool_calls:
                emit({"tool_calls": tool_calls})
            if content:
                step = 1024
                for i in range(0, len(content), step):
                    emit({"content": content[i:i + step]})
            final_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": finish}],
            }
            if usage is not None:
                final_chunk["usage"] = usage
            with lock:
                self.wfile.write(b"data: ")
                self.wfile.write(json.dumps(final_chunk).encode("utf-8"))
                self.wfile.write(b"\n\ndata: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _write_sse_error(self, message: str, lock: threading.Lock) -> None:
        """Write a single error chunk + [DONE] when the agent loop fails
        AFTER headers have already been sent. We can't change the HTTP
        status at that point, so deliver the error in the SSE stream so
        the SDK at least sees a terminal event instead of hanging.
        """
        try:
            created = int(time.time())
            cid = f"chatcmpl-{created}"
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "caliber-grounding-proxy",
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[caliber-grounding-proxy error] {message}"},
                    "finish_reason": "error",
                }],
            }
            with lock:
                self.wfile.write(b"data: ")
                self.wfile.write(json.dumps(chunk).encode("utf-8"))
                self.wfile.write(b"\n\ndata: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler convention
        if self.path == "/health":
            self._write_json(200, {"ok": True, "service": "caliber-grounding-proxy"})
            return
        if self.path == "/v1/models":
            # Advertise one caliber-allowlisted name so its model-recovery
            # UI has something to pick, plus the real Ollama tag we route
            # to. The override (if set) wins internally anyway.
            models = [
                {"id": "gpt-4o", "object": "model"},
                {"id": "gpt-4o-mini", "object": "model"},
            ]
            ov = _model_override()
            if ov:
                models.append({"id": ov, "object": "model"})
            self._write_json(200, {"object": "list", "data": models})
            return
        self._write_json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._write_json(404, {"error": {"message": "not found"}})
            return
        clen = int(self.headers.get("Content-Length") or 0)
        try:
            body = self.rfile.read(clen) if clen > 0 else b""
            payload = json.loads(body) if body else {}
        except (ValueError, OSError) as e:
            self._write_json(400, {"error": {"message": f"bad request: {e}"}})
            return

        cwd = _cwd_for_request()
        stream_requested = bool(payload.get("stream"))
        # Optional dump of the full agent-loop result for debugging
        # truncation / JSON parse failures downstream. Set
        # CALIBER_GROUNDING_RESULT_DUMP_DIR to capture one file per request.
        dump_dir = os.environ.get("CALIBER_GROUNDING_RESULT_DUMP_DIR")

        if stream_requested:
            # When the caller asks for SSE, send 200 + headers NOW and
            # emit periodic keep-alive comments while the agent loop
            # runs. Otherwise undici's bodyTimeout (5min default, used
            # by the OpenAI Node SDK that caliber wraps) aborts the
            # request before the loop returns on long audit prompts.
            try:
                self._start_sse_stream()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Caller hung up before we could even respond; nothing
                # to do — agent loop work would be wasted.
                return
            write_lock = threading.Lock()
            stop_hb = threading.Event()
            interval = _sse_heartbeat_seconds()

            def _heartbeat() -> None:
                # Wait first, then emit; first heartbeat fires after one
                # interval so a fast loop doesn't pollute the stream.
                while not stop_hb.wait(interval):
                    try:
                        with write_lock:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        stop_hb.set()
                        return

            hb_thread = threading.Thread(
                target=_heartbeat, name="sse-heartbeat", daemon=True,
            )
            hb_thread.start()
            try:
                try:
                    result = run_agent_loop(payload, cwd)
                except ollama.UpstreamError as e:
                    log.warning("upstream error %d: %s", e.status,
                                str(e.body)[:200])
                    self._write_sse_error(
                        f"upstream {e.status}: {str(e.body)[:200]}",
                        write_lock,
                    )
                    return
                except Exception as e:
                    log.exception("agent loop failed")
                    self._write_sse_error(
                        f"agent loop failed: {e}", write_lock,
                    )
                    return
            finally:
                stop_hb.set()
                hb_thread.join(timeout=2.0)

            if dump_dir:
                try:
                    os.makedirs(dump_dir, exist_ok=True)
                    fname = os.path.join(
                        dump_dir, f"result-{int(time.time() * 1000)}.json",
                    )
                    with open(fname, "w", encoding="utf-8") as f:
                        json.dump({"stream": True, "result": result},
                                  f, indent=2, ensure_ascii=False)
                except OSError:
                    pass
            try:
                recall.maybe_store_assistant_turn(payload, result)
            except Exception as e:
                log.warning("recall: store-back failed: %s", e)
            self._write_sse_body(result, write_lock)
            return

        # Non-streaming path: classic request/response with a real
        # status code on failure.
        try:
            result = run_agent_loop(payload, cwd)
        except ollama.UpstreamError as e:
            log.warning("upstream error %d: %s", e.status,
                        str(e.body)[:200])
            self._write_json(e.status,
                             e.body if isinstance(e.body, dict)
                             else {"error": {"message": str(e.body)}})
            return
        except Exception as e:
            log.exception("agent loop failed")
            self._write_json(502, {"error": {
                "type": "proxy_error",
                "message": f"grounding proxy failed: {e}",
            }})
            return
        if dump_dir:
            try:
                os.makedirs(dump_dir, exist_ok=True)
                fname = os.path.join(
                    dump_dir, f"result-{int(time.time() * 1000)}.json",
                )
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump({"stream": False, "result": result},
                              f, indent=2, ensure_ascii=False)
            except OSError:
                pass
        try:
            recall.maybe_store_assistant_turn(payload, result)
        except Exception as e:
            log.warning("recall: store-back failed: %s", e)
        self._write_json(200, result)


def build_server(host: Optional[str] = None,
                 port: Optional[int] = None,
                 ) -> ThreadingHTTPServer:
    host = host or os.environ.get("CALIBER_GROUNDING_HOST", "127.0.0.1")
    port = port or int(os.environ.get("CALIBER_GROUNDING_PORT", "38090"))
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def run() -> int:
    logging.basicConfig(
        level=os.environ.get("CALIBER_GROUNDING_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server = build_server()
    except OSError as e:
        print(f"caliber-grounding-proxy: bind failed: {e}", file=sys.stderr)
        return 1
    host, port = server.server_address
    print(
        f"caliber-grounding-proxy listening on http://{host}:{port}/v1 -> "
        f"{ollama.default_upstream()}",
        file=sys.stderr,
    )
    print(
        f"  cwd: {_cwd_for_request()}",
        file=sys.stderr,
    )
    print(
        f"  point caliber at it via: OPENAI_BASE_URL=http://{host}:{port}/v1 "
        f"OPENAI_API_KEY=ollama",
        file=sys.stderr,
    )
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    # Pre-warm the embedder(s) in a background thread so cold-loading
    # qwen3-embedding doesn't show up as a 90-180s stall on caliber's
    # very first recall. Failure is logged and ignored — the proxy
    # serves traffic regardless. Skip with CALIBER_GROUNDING_PREHEAT=0.
    threading.Thread(
        target=lambda: recall.preheat_embedder(),
        name="recall-preheat",
        daemon=True,
    ).start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            stop.wait(timeout=1.0)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        ollama.close()
    return 0
