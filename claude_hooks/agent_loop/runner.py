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

from claude_hooks import capped_thinking, reasoning_replay, truncation
from claude_hooks import reasoning_loop_guard as rlg
from claude_hooks import reasoning_repetition
from claude_hooks import loop_detection

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

    # Reasoning a budget cut short is condensed into a note rather than
    # dropped, so the next iteration continues instead of re-deriving.
    # False restores the pre-fix behavior of discarding it.
    capped_thinking_enabled: bool = True
    #: Fallback budget when the request carries no ``num_predict`` of its
    #: own. Usually unnecessary — the request's own budget is exact.
    thinking_budget_tokens: Optional[int] = None
    #: The model's declared out-of-budget message, from ``/api/show``.
    #: When present it is direct evidence and needs no threshold.
    thinking_budget_message: Optional[str] = None

    # A final answer the backend cut short is continued rather than
    # returned half-written (2026-08-12). Each continuation is one more
    # call, so this is bounded; 0 restores the pre-fix behavior of
    # accepting whatever arrived. Only the *answer* turn is continued —
    # a truncated tool-call turn is discarded by the existing arg-parse
    # guard, which is the correct handling for half-written arguments.
    max_answer_continuations: int = 2

    # Past-turn reasoning on the wire (plan C4): "none" drops it (the
    # default and the pre-C4 behaviour), "all" keeps the field, "inline"
    # puts it in the content as <think>…</think>. Callers resolve "all"
    # against the server first (claude_hooks.reasoning_replay.resolve).
    reasoning_replay: str = "none"

    # Called with the payload before every chat_fn call in the loop, and
    # may rewrite it in place (compact ``messages``, size
    # ``options.num_predict``). The history grows by a tool result per
    # iteration, so a budget computed once before the loop is wrong by
    # the second call. Exceptions are logged and the call goes ahead
    # unprepared. None = no preparation (caliber / advisor behavior).
    prepare_payload: Optional[Callable[[dict], None]] = None

    # Called with (payload, response) after every conversation call the
    # loop makes — each iteration and each answer continuation — with the
    # payload exactly as it was sent. For calibration: the provider's
    # prompt count belongs to that payload, and only to that one.
    # Auxiliary calls (the capped-thinking condenser) are not reported,
    # so they cannot overwrite the conversation's anchor. Exceptions are
    # logged and ignored. None = no callback (caliber / advisor).
    on_response: Optional[Callable[[dict, dict], None]] = None

    # Result tools: calls the loop answers itself instead of executing.
    # A result call that is the turn's only call ends the loop; its
    # arguments travel back as ``final["_result_call"]`` and are never
    # run. Beside other calls it is not taken: the others run and the
    # result call is answered with ``result_not_alone_message``. Arguments
    # that don't parse as JSON are answered with ``result_invalid_message``
    # once. When the loop strips tools to force an answer, the result
    # tools stay, so the forced answer can still be a record. Both
    # messages take ``{name}`` (and the second ``{what}``). Empty = none
    # (caliber / advisor).
    result_tools: tuple[str, ...] = ()
    result_not_alone_message: str = (
        "Not taken yet: call {name} on its own, after you have read what "
        "your other calls returned.")
    result_invalid_message: str = (
        "Your {name} call was {what}. Call {name} again, smaller: shorter "
        "fields and fewer items.")

    # Plan D1: the reasoning-loop guard is always on. A turn whose
    # reasoning collapsed into repetition is cut (the stream readers stop
    # reading; a reply that was not streamed is scanned after the fact),
    # its partial content kept and the loop continued with a note; this
    # many cut turns in a row end the loop with one forced tool-less
    # report. ``on_guard`` gets one event dict per trip and per stop —
    # the quantity the guard fired on included. Exceptions are ignored.
    reasoning_loop_max_trips: int = rlg.DEFAULT.max_consecutive_trips
    # Plan D10/D14 (always on): consecutive failed tool calls that end the
    # loop, and tools whose same call rightly repeats (status / await).
    mistake_limit: int = loop_detection.MISTAKE_LIMIT
    polling_tools: tuple[str, ...] = ()
    on_guard: Optional[Callable[[dict], None]] = None


def _json_args(tc: dict) -> bool:
    args = (tc.get("function") or {}).get("arguments")
    if isinstance(args, dict):
        return True
    try:
        return isinstance(json.loads(args or ""), dict)
    except (TypeError, json.JSONDecodeError):
        return False


# Type aliases.
ToolExecutor = Callable[[str, str, str], str]
ChatFn = Callable[[dict], dict]
# A preseed builder returns either:
#   None  — no preseed
#   ([assistant_msg, tool_msg], dedup_key, dedup_content)
PreseedBuilder = Callable[[str], Optional[tuple[list[dict], str, str]]]

# Optional observability callbacks. Both default to None — caliber +
# advisor pass nothing; consultants' MessageRecorder hooks them up.
#
# OnIter fires AFTER each chat_fn(payload) returns. The callback gets
# the iteration index, the request payload that was sent (mutations
# from earlier iterations are reflected), the response dict, and the
# elapsed wall-clock in ms.
#
# OnTool fires once per tool invocation in execute_tool_calls.
# duration_ms is 0 for dedup stubs (no actual tool work happened).
# error is None on success, or the exception string when the
# tool_executor raised.
OnIter = Callable[[int, dict, dict, int], None]
OnTool = Callable[[str, str, str, int, Optional[str]], None]


CONTINUE_ANSWER_INSTRUCTION = (
    "Your previous message was cut off by the output limit before you "
    "finished. Continue from exactly where it stopped — resume "
    "mid-sentence if that is where it ended. Do not repeat any text you "
    "already produced and do not open with a preamble; the two halves "
    "will be concatenated verbatim."
)


class _ChatFnClient:
    """Adapts a bare ``chat_fn`` to the ``.chat(payload)`` shape the
    condenser expects. The condensation runs against the same endpoint
    and model as the turn it is condensing, which is the only pairing
    that needs no configuration to be correct."""

    def __init__(self, chat_fn: "ChatFn") -> None:
        self._chat_fn = chat_fn

    def chat(self, payload: dict) -> dict:
        return self._chat_fn(payload)


def _capped_thinking_note(msg: dict, response: dict, payload: dict,
                          config: "LoopConfig", chat_fn: "ChatFn", *,
                          iteration: int) -> Optional[str]:
    """A note replacing the reasoning of a turn the budget cut short.

    ``None`` — the common case — means the turn reasoned within its
    budget and its thinking is simply dropped, as before.

    The budget compared against is **our own** ``num_predict``. Ollama
    counts thinking tokens toward it, so a role that reasons at length
    can spend the whole allowance before writing any content. That risk
    is created by sending an explicit budget at all, which this repo
    started doing on 2026-08-12 — before that the ceiling was an unseen
    provider default and equally uncontrollable, just invisible. Knowing
    the number is what makes the detection exact rather than a guess.
    """
    if getattr(config, "capped_thinking_enabled", True) is not True:
        return None
    # The real thinking allowance first (plan B3): what the server says it
    # enforced, or the budget the request stated. Only without either is
    # the whole output cap the bound — thinking counts toward it.
    timings = response.get("timings") or {}
    budget = timings.get("think_budget_tokens")
    if not (isinstance(budget, int) and budget > 0):
        budget = (payload.get("options") or {}).get("num_predict")
    completion_tokens = int(
        (response.get("usage") or {}).get("completion_tokens") or 0)
    capped, stand_down = capped_thinking.detect(
        msg,
        budget_tokens=budget or getattr(config, "thinking_budget_tokens", None),
        budget_message=getattr(config, "thinking_budget_message", None),
        completion_tokens=completion_tokens,
    )
    if capped is None:
        if stand_down not in ("turn-did-not-reason", "reasoning-within-budget"):
            log.debug("capped-thinking stood down on iter %d: %s",
                      iteration, stand_down)
        return None

    log.info(
        "capped thinking on iter %d (%s): %d reasoning tok of a %s budget "
        "— condensing rather than discarding",
        iteration, capped.evidence, capped.thinking_tokens, budget,
    )
    note, reason = capped_thinking.condense(
        capped,
        chat_client=_ChatFnClient(chat_fn),
        model=payload.get("model") or "",
        tool_outcomes=[
            {"name": (tc.get("function") or {}).get("name"),
             "input": (tc.get("function") or {}).get("arguments"),
             "result": "(pending — results follow this message)"}
            for tc in (msg.get("tool_calls") or [])
        ],
    )
    if note is None:
        log.info("capped-thinking produced no usable note (%s); the turn "
                 "will re-derive", reason)
        return None
    return capped_thinking.as_thinking_block(note)


def _notify_response(config: "LoopConfig", payload: dict,
                     response: dict) -> None:
    callback = getattr(config, "on_response", None)
    if callback is None:
        return
    try:
        callback(payload, response)
    except Exception:
        log.exception("on_response callback raised; ignored")


def _answer_text(response: dict) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def _continue_truncated_answer(final: dict, payload: dict, chat_fn: ChatFn,
                               config: "LoopConfig",
                               on_iter: "Optional[OnIter]",
                               iter_idx: int) -> dict:
    """Finish an answer the backend cut mid-generation.

    Returns a response dict whose text is the concatenation of the
    partial and its continuations, with usage summed. When nothing was
    truncated — the overwhelming majority of turns — this returns
    ``final`` unchanged and costs one dict inspection.

    Only the answer turn reaches here. A truncated *tool-call* turn is
    a different problem (half-written arguments) already handled by the
    arg-parse guard in ``execute_tool_calls``, and continuing one would
    mean asking a model to resume a JSON literal.
    """
    budget = getattr(config, "max_answer_continuations", 0) or 0
    if budget <= 0:
        return final

    parts = [_answer_text(final)]
    total_prompt = int((final.get("usage") or {}).get("prompt_tokens") or 0)
    total_completion = int(
        (final.get("usage") or {}).get("completion_tokens") or 0)
    messages = list(payload.get("messages") or [])
    result = final

    for n in range(budget):
        cut = truncation.classify(result)
        if cut is None:
            break
        log.warning(
            "agent loop: answer truncated on iter %d (%s) — "
            "continuing (%d/%d)", iter_idx, cut.detail, n + 1, budget,
        )
        # The discarded-turn path (plan C4): a cut answer is usually a turn
        # whose reasoning ran to its budget, and the continuation would
        # otherwise reason the same ground again from nothing.
        cut_msg = ((result.get("choices") or [{}])[0].get("message") or {})
        note = _capped_thinking_note(cut_msg, result, payload, config, chat_fn,
                                     iteration=iter_idx)
        instruction = CONTINUE_ANSWER_INSTRUCTION
        if note:
            instruction = (CONTINUE_ANSWER_INSTRUCTION + "\n\n"
                           + capped_thinking.as_discarded_turn_note(note))
        messages = messages + [
            {"role": "assistant", "content": "".join(parts)},
            {"role": "user", "content": instruction, "_harness": True},
        ]
        cont_payload = dict(payload)
        cont_payload["messages"] = messages
        # Tools stay off for a continuation: the model already decided
        # it was answering, and re-offering tools invites it to restart
        # the investigation instead of finishing the sentence.
        cont_payload.pop("tools", None)
        cont_payload.pop("tool_choice", None)
        t0 = time.monotonic()
        result = chat_fn(cont_payload)
        _notify_response(config, cont_payload, result)
        if on_iter is not None:
            try:
                on_iter(iter_idx, cont_payload, result,
                        int((time.monotonic() - t0) * 1000))
            except Exception:
                log.exception("on_iter callback raised; ignored")
        parts.append(_answer_text(result))
        usage = result.get("usage") or {}
        total_prompt += int(usage.get("prompt_tokens") or 0)
        total_completion += int(usage.get("completion_tokens") or 0)
    else:
        if truncation.classify(result) is not None:
            log.error(
                "agent loop: answer still truncated after %d "
                "continuation(s); returning the partial", budget,
            )

    if len(parts) == 1:
        return final

    merged = dict(result)
    choices = [dict(c) for c in (merged.get("choices") or [{}])]
    msg = dict(choices[0].get("message") or {})
    msg["content"] = "".join(parts)
    choices[0]["message"] = msg
    merged["choices"] = choices
    merged["usage"] = {
        **(merged.get("usage") or {}),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
    }
    return merged


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


#: Every this-many repeats of a stubbed call, the real result is served
#: again (the fork's ``unchanged-reads.ts``): a model that keeps asking
#: may no longer have the copy the stub points at.
RESERVE_EVERY = 4

#: Calls that change something: after one, earlier results may be stale,
#: so nothing is answered from memory any more.
_WRITE_PREFIXES = ("write_", "edit_", "apply_", "run_", "delete_", "create_",
                   "move_", "patch")


def is_write_tool(name: str) -> bool:
    return (name or "").lower().startswith(_WRITE_PREFIXES)


def dedup_key(name: str, args: Any) -> str:
    """``name|arguments`` with the arguments in canonical JSON, so a
    reordered or re-spaced repeat is still the same call."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return f"{name}|{args}"
    try:
        return f"{name}|{json.dumps(args, sort_keys=True, separators=(',', ':'))}"
    except (TypeError, ValueError):
        return f"{name}|{args}"


LOOP_GUARD_LAST_WARNING = (
    "This call was not run. This is your last warning: the next call the "
    "loop guard stops ends this task. Send something different, or give "
    "your answer with what you have.")

LOOP_GUARD_FINAL = (
    "[SYSTEM MESSAGE] The harness is ending this task: {why} Tools are off. "
    "Write your report now from what you have already established: what "
    "you found, with its evidence, and what is still open.")


class ToolGuards:
    """Plan D10/D14 state for one ``run_loop``: the loop tracker, the
    mistake limit, and what fired during the current batch of calls."""

    def __init__(self, config: "LoopConfig") -> None:
        self.config = config
        self.tracker = loop_detection.LoopTracker(
            polling=frozenset(getattr(config, "polling_tools", ()) or ())
            or loop_detection.POLLING_TOOL_NAMES)
        self.mistakes = loop_detection.MistakeLimit(
            limit=max(1, int(getattr(config, "mistake_limit",
                                     loop_detection.MISTAKE_LIMIT))))
        self.hard_hits = 0
        self.stop: Optional[dict] = None
        self.iteration = 0

    def _emit(self, event: dict) -> None:
        _emit_guard(self.config, {"iteration": self.iteration, **event})

    def hard(self, name: str, args: str, verdict) -> None:
        self.hard_hits += 1
        call = loop_detection.describe_call(name, args)
        action = "stop" if self.hard_hits >= 2 else "last_warning"
        self._emit({"guard": "loop_guard", "action": action,
                    "rule": verdict.rule, "count": verdict.count,
                    "call": call})
        if action == "stop" and self.stop is None:
            self.stop = {"stop_reason": "loop_guard", "call": call,
                         "why": f"{verdict.message} The call was {call}."}

    def outcome(self, name: str, args: str, answer: str, output: str,
                verdict) -> str:
        failed = loop_detection.is_failed_output(answer)
        self.tracker.note_outcome(not failed, answer)
        if self.mistakes.note(failed) and self.stop is None:
            n = self.mistakes.streak
            self._emit({"guard": "mistake_limit", "action": "stop",
                        "count": n, "call":
                        loop_detection.describe_call(name, args)})
            self.stop = {"stop_reason": "mistake_limit", "why": (
                f"{n} tool calls in a row have failed, the last one "
                f"{loop_detection.describe_call(name, args)}.")}
        if verdict.kind == "soft":
            self._emit({"guard": "loop_guard", "action": "warn",
                        "rule": verdict.rule, "count": verdict.count,
                        "call": loop_detection.describe_call(name, args)})
            return f"{output}\n\n[SYSTEM MESSAGE] {verdict.message}"
        return output


def execute_tool_calls(tool_calls: list[dict], cwd: str,
                       seen: dict[str, str],
                       tool_executor: ToolExecutor,
                       *,
                       on_tool: Optional[OnTool] = None,
                       repeats: Optional[dict[str, int]] = None,
                       guards: Optional["ToolGuards"] = None) -> list[dict]:
    """Run each tool and return ``role: tool`` messages.

    Duplicate (name+args) calls within a single agent loop return a
    short stub pointing the model back at the prior result. Models
    (notably gemma4-98e) will otherwise loop on the same tool call
    dozens of times, exploding context and wall time.

    Plan C5: the key is the canonical arguments; every
    :data:`RESERVE_EVERY`-th repeat (counted in ``repeats``) serves the
    real result again; and a call that writes clears ``seen``, because an
    earlier result may no longer be true. ``run_loop`` also clears it on
    compaction, when the result the stub points at was folded away.

    When ``on_tool`` is set, it fires once per tool call (real and
    deduped) with ``(name, args_str, output, duration_ms, error)``.
    Callback exceptions are swallowed so a misbehaving recorder
    can't crash the loop.

    Plan D10/D14: with ``guards``, every call is inspected first. A soft
    verdict rides on the result; a hard one answers the call without
    running it. Each outcome then feeds the loop tracker and the mistake
    limit, and what fired is left in ``guards.hits`` for ``run_loop``.
    """
    results: list[dict] = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_str = fn.get("arguments") or "{}"
        if isinstance(args_str, dict):
            args_str = json.dumps(args_str)
        key = dedup_key(name, args_str)
        err: Optional[str] = None
        verdict = (guards.tracker.inspect(name, args_str) if guards is not None
                   else loop_detection.OK)
        if verdict.kind == "hard":
            guards.hard(name, args_str, verdict)
            output = (f"[SYSTEM MESSAGE] {verdict.message}\n\n"
                      f"{LOOP_GUARD_LAST_WARNING}")
            guards.tracker.pending = ""
            if on_tool is not None:
                try:
                    on_tool(name, args_str, output, 0, None)
                except Exception:
                    log.exception("on_tool callback raised; ignored")
            results.append({"role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": output})
            continue
        reserve = False
        if key in seen and repeats is not None:
            repeats[key] = repeats.get(key, 0) + 1
            reserve = repeats[key] % RESERVE_EVERY == 0
        if key in seen and not reserve:
            output = (
                f"(duplicate: you already called {name}({args_str[:80]}). "
                "Use that prior result and continue. Do not repeat this call.)"
            )
            dt_ms = 0
            log.info("tool %s(%s) -> DEDUP stub", name, args_str[:80])
        else:
            t0 = time.monotonic()
            try:
                output = tool_executor(name, args_str, cwd)
            except Exception as exc:  # surface to recorder; re-raise
                dt_ms = int((time.monotonic() - t0) * 1000)
                err = f"{type(exc).__name__}: {exc}"
                if on_tool is not None:
                    try:
                        on_tool(name, args_str, "", dt_ms, err)
                    except Exception:
                        log.exception("on_tool callback raised; ignored")
                raise
            dt_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "tool %s(%s) -> %d chars in %d ms",
                name, args_str[:80], len(output), dt_ms,
            )
            seen[key] = output
            if is_write_tool(name):
                seen.clear()
                if repeats is not None:
                    repeats.clear()
        if guards is not None:
            # A stubbed repeat is the earlier answer again, failure included.
            answer = seen.get(key, output)
            output = guards.outcome(name, args_str, answer, output, verdict)
        if on_tool is not None:
            try:
                on_tool(name, args_str, output, dt_ms, err)
            except Exception:
                log.exception("on_tool callback raised; ignored")
        results.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "name": name,
            "content": output,
        })
    return results


REASONING_LOOP_NOTE = (
    "[SYSTEM MESSAGE] Your last turn was stopped by the harness: {diagnosis} "
    "That turn's reasoning was cut and is not carried into this request. "
    "Do not reason through the same ground again. Take the next concrete "
    "step instead: call a tool, or give your answer.")

REASONING_LOOP_FINAL = (
    "[SYSTEM MESSAGE] Your reasoning has looped {trips} turns in a row "
    "({diagnosis}), so the harness is ending this task. Tools are off. "
    "Write your report now from what you have already established: what "
    "you found, with its evidence, and what is still open. Keep any "
    "reasoning short.")


def reasoning_loop_verdict(response: dict) -> Optional[dict]:
    """The guard's verdict on one reply, or ``None``.

    A streamed reply the reader cut carries it already. A reply that was
    not streamed is scanned after the fact, and counts only when the
    reasoning ate the turn — no content and no tool call: there the
    billing already happened, but a loop that left nothing still has to
    end the lane rather than be asked again forever."""
    v = response.get("reasoning_loop")
    if isinstance(v, dict) and v:
        return v
    choices = response.get("choices") or [{}]
    choice = choices[0] or {}
    if (choice.get("done_reason") or response.get("done_reason")) == rlg.DONE_REASON:
        return {"kind": "unknown"}
    msg = choice.get("message") or {}
    if (msg.get("content") or "").strip() or msg.get("tool_calls"):
        return None
    found = rlg.scan(capped_thinking.thinking_text(msg))
    return found.public_dict() if found is not None else None


def _verdict_text(v: dict) -> str:
    if v.get("kind") not in ("low-uniqueness", "line-cycle", "phrase-cycle"):
        return "The model's reasoning collapsed into repetition."
    return rlg.describe(rlg.Verdict(
        kind=v["kind"], chars=int(v.get("chars") or 0),
        sample=str(v.get("sample") or ""), period=v.get("period"),
        cycles=v.get("cycles"), distinct=v.get("distinct")))


def _emit_guard(config: "LoopConfig", event: dict) -> None:
    cb = getattr(config, "on_guard", None)
    if cb is None:
        return
    try:
        cb(event)
    except Exception:
        log.exception("on_guard callback raised; ignored")


#: Reasoning handed to the condenser, at most (the part before the loop).
LOOP_NOTE_INPUT_CHARS = 20_000


def _before_loop(thinking: str, verdict: dict) -> str:
    """The reasoning up to the first occurrence of the repeating unit."""
    if not thinking:
        return ""
    unit = str(verdict.get("sample") or "").split(" / ")[0].strip()[:60]
    at = thinking.find(unit) if len(unit) >= 4 else -1
    head = thinking[:at] if at >= 0 else ""
    return head[-LOOP_NOTE_INPUT_CHARS:]


def _reasoning_loop_note(msg: dict, payload: dict, chat_fn: "ChatFn",
                         verdict: dict) -> Optional[str]:
    """What the cut reasoning had settled before it looped, condensed —
    or ``None``. The loop itself is not sent to the condenser: tens of
    thousands of repeated characters are nothing to summarise, and a
    condenser fed them tends to repeat them."""
    thinking = _before_loop(capped_thinking.thinking_text(msg), verdict)
    if len(thinking.strip()) < 200:
        return None
    capped = capped_thinking.CappedThinking(
        thinking=thinking, thinking_tokens=0, budget_tokens=None,
        evidence="reasoning-loop")
    note, reason = capped_thinking.condense(
        capped, chat_client=_ChatFnClient(chat_fn),
        model=payload.get("model") or "")
    if note is None:
        log.info("reasoning-loop: no usable condensed note (%s)", reason)
        return None
    return capped_thinking.as_discarded_turn_note(
        capped_thinking.as_thinking_block(note))


def _forced_report(payload: dict, history: list, instruction: str,
                   stop_reason: str, chat_fn: "ChatFn", config: "LoopConfig",
                   on_iter: Optional["OnIter"], iteration: int
                   ) -> tuple[dict, dict]:
    """The named terminal action (plan D): one tool-less, think-off report
    asked of the lane, marked ``_stop_reason`` / ``_failure_class``."""
    payload = dict(payload)
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    payload["think"] = False
    payload["reasoning_effort"] = "none"
    payload["messages"] = list(history) + [
        {"role": "user", "content": instruction, "_harness": True}]
    t0 = time.monotonic()
    final = chat_fn(payload)
    _notify_response(config, payload, final)
    if on_iter is not None:
        try:
            on_iter(iteration, payload, final,
                    int((time.monotonic() - t0) * 1000))
        except Exception:
            log.exception("on_iter callback raised; ignored")
    final = dict(final or {})
    final["_stop_reason"] = stop_reason
    final["_failure_class"] = stop_reason
    return payload, final


def run_loop(
    payload: dict,
    cwd: str,
    *,
    config: LoopConfig,
    tool_specs: list[dict],
    chat_fn: ChatFn,
    tool_executor: ToolExecutor,
    preseed_builder: Optional[PreseedBuilder] = None,
    on_iter: Optional[OnIter] = None,
    on_tool: Optional[OnTool] = None,
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
    result_invalid_sent = False
    seen_calls: dict[str, str] = {}
    repeats: dict[str, int] = {}
    compaction_gen = 0
    loop_trips = 0
    nudger = reasoning_repetition.RepetitionNudger()
    guards = ToolGuards(config)

    if config.tools_available and preseed_builder is not None:
        preseed = preseed_builder(cwd)
        if preseed is not None:
            preseed_msgs, preseed_key, dedup_content = preseed
            payload["messages"] = list(payload["messages"]) + preseed_msgs
            name, _, args = str(preseed_key).partition("|")
            seen_calls[dedup_key(name, args)] = dedup_content
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
            kept = [t for t in (payload.get("tools") or [])
                    if ((t.get("function") or {}).get("name")
                        in config.result_tools)]
            if kept:
                payload["tools"] = kept
                payload["tool_choice"] = "auto"
            else:
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
            tools_stripped = True
        if config.tools_available and not tools_stripped:
            if force_first and not has_called_tool:
                payload["tool_choice"] = "required"
            else:
                payload["tool_choice"] = "auto"
        if config.prepare_payload is not None:
            try:
                config.prepare_payload(payload)
            except Exception:
                log.exception("prepare_payload raised on iter %d; sending "
                              "the payload unprepared", i)
            gen = max((int(m.get("_compaction_generation") or 1)
                       for m in payload.get("messages") or []
                       if isinstance(m, dict) and m.get("_compaction_marker")),
                      default=0)
            if gen > compaction_gen:
                # The results the duplicate stub points at may be folded
                # away: answer repeats for real again (plan C5).
                compaction_gen = gen
                seen_calls.clear()
                repeats.clear()
        t_iter = time.monotonic()
        final = chat_fn(payload)
        iter_ms = int((time.monotonic() - t_iter) * 1000)
        _notify_response(config, payload, final)
        if on_iter is not None:
            try:
                on_iter(i, payload, final, iter_ms)
            except Exception:
                log.exception("on_iter callback raised; ignored")
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
        verdict = reasoning_loop_verdict(final)
        if verdict is None:
            loop_trips = 0
        else:
            # Plan D1. The turn is cut: its tool calls may be half-written
            # and its reasoning is the loop, so neither goes back. A
            # thinking-only re-ask (force_first, truncation continuation)
            # never applies to it.
            loop_trips += 1
            limit = max(1, int(config.reasoning_loop_max_trips))
            diagnosis = _verdict_text(verdict)
            event = {
                "guard": "reasoning_loop", "action": (
                    "stop" if loop_trips >= limit else "cut"),
                "iteration": i, "trips": loop_trips, "limit": limit,
                **verdict}
            # The realized sampler (plan A5): loops trace back to sampling.
            sampler = (final.get("timings") or {}).get("sampler")
            if sampler:
                event["sampler"] = sampler
            _emit_guard(config, event)
            log.warning("reasoning loop on iter %d (%d/%d): %s",
                        i, loop_trips, limit, diagnosis)
            partial = (msg.get("content") or "").strip()
            history = list(payload["messages"])
            if partial:
                history.append({"role": "assistant", "content": partial})
            if loop_trips >= limit:
                payload, final = _forced_report(
                    payload, history, REASONING_LOOP_FINAL.format(
                        trips=loop_trips, diagnosis=diagnosis),
                    "reasoning_loop", chat_fn, config, on_iter, i)
                final["_reasoning_loop"] = verdict
                break
            note = _reasoning_loop_note(msg, payload, chat_fn, verdict)
            text = REASONING_LOOP_NOTE.format(diagnosis=diagnosis)
            if note:
                text += "\n\n" + note
            payload = dict(payload)
            payload["messages"] = history + [
                {"role": "user", "content": text, "_harness": True}]
            continue
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
                retry = config.force_first_retry_message
                note = _capped_thinking_note(msg, final, payload, config,
                                             chat_fn, iteration=i)
                if note:
                    retry += ("\n\n"
                              + capped_thinking.as_discarded_turn_note(note))
                payload = dict(payload)
                payload["messages"] = list(payload["messages"]) + [
                    {"role": "assistant", "content": prior_content},
                    {"role": "user", "content": retry, "_harness": True},
                ]
                continue
            final = _continue_truncated_answer(
                final, payload, chat_fn, config, on_iter, i)
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

        results_called = [tc for tc in tool_calls
                          if ((tc.get("function") or {}).get("name")
                              in config.result_tools)]
        if results_called and len(tool_calls) == 1:
            tc = results_called[0]
            if _json_args(tc):
                final = dict(final)
                final["_result_call"] = {
                    "name": (tc.get("function") or {}).get("name"),
                    "arguments": (tc.get("function") or {}).get("arguments"),
                }
                break
            if not result_invalid_sent:
                result_invalid_sent = True
                cut = (choice.get("done_reason") or final.get("done_reason")) == "length"
                name = (tc.get("function") or {}).get("name") or ""
                payload["messages"] = list(payload["messages"]) + [
                    {"role": "assistant", "content": None, "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": tc.get("id") or "",
                     "name": name,
                     "content": config.result_invalid_message.format(
                         name=name, what="cut off at the output limit" if cut
                         else "not valid JSON")},
                ]
                continue
            break

        clean_msg = dict(msg)
        clean_msg["content"] = None
        clean_msg["tool_calls"] = tool_calls
        # The reasoning does not survive as-is: providers reject a
        # replayed thinking block, and seventeen thousand tokens of
        # interrupted rambling is not something a model continues from
        # anyway. But dropping it outright is what makes a turn that
        # reasoned to its budget start the same reasoning from scratch
        # on the next iteration — observed as the same ground covered a
        # dozen times, each pass ending at the same cap. When the cap
        # is what ended it, a short note of what it settled goes back in
        # its place; otherwise it goes, as before.
        note = _capped_thinking_note(
            msg, final, payload, config, chat_fn, iteration=i)
        if note:
            for k in capped_thinking.token_calib.REASONING_KEYS:
                clean_msg.pop(k, None)
            clean_msg["content"] = note
        else:
            clean_msg = reasoning_replay.apply(
                clean_msg, getattr(config, "reasoning_replay", "none"))
        payload["messages"] = list(payload["messages"]) + [clean_msg]
        runnable = [tc for tc in tool_calls if tc not in results_called]
        guards.iteration = i
        payload["messages"].extend(
            execute_tool_calls(
                runnable, cwd, seen_calls, tool_executor,
                on_tool=on_tool, repeats=repeats, guards=guards,
            )
        )
        for tc in results_called:
            name = (tc.get("function") or {}).get("name") or ""
            payload["messages"].append({
                "role": "tool", "tool_call_id": tc.get("id") or "",
                "name": name,
                "content": config.result_not_alone_message.format(name=name),
            })
        # Plan D1: a turn that re-derives the same conclusion paragraph
        # after paragraph while still calling tools reads as productive to
        # every turn-level check. One rationed nudge; it never cuts.
        if guards.stop is not None:
            # Plan D10/D14: the second hard loop verdict, or the mistake
            # limit. The batch's results stay in the history the report
            # is asked from.
            stop = guards.stop
            payload, final = _forced_report(
                payload, payload["messages"],
                LOOP_GUARD_FINAL.format(why=stop["why"]),
                stop["stop_reason"], chat_fn, config, on_iter, i)
            if stop.get("call"):
                final["_loop_call"] = stop["call"]
            break
        nudge = nudger.inspect(capped_thinking.thinking_text(msg), i)
        if nudge:
            payload["messages"].append(
                {"role": "user", "content": nudge, "_harness": True})
            _emit_guard(config, {"guard": "reasoning_repetition",
                                 "action": "nudge", "iteration": i,
                                 **nudger.last.public_dict()})
    else:
        log.warning("agent loop hit max_iterations=%d", config.max_iterations)
    # Attach the final conversation transcript to the response so
    # callers that need to follow up (e.g., the consultants
    # researcher fallback when the loop ends with empty text) can
    # build a fresh prompt that keeps tool results in context.
    # Existing callers (caliber, advisor) ignore the extra key.
    final = dict(final or {})
    final.setdefault("_loop_messages", list(payload.get("messages") or []))
    return final
