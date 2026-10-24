"""Compaction for council roles (plan Milestone C).

One pipeline, two flows (C2):

========  ==================================================================
reduced   present-tense replay writer → retrospective → one reviewer of the
          whole replay. The shipped default for every role and model.
full      writer with a ``<<<HALFWAY>>>`` marker → retrospective → critic of
          the first half → critic of the second half → synthesizer (joins
          the halves; the only call that revises the retrospective).
          Opt-in per role (``[role.X.compaction] flow = "full"``).
basic     no model call: the bare elision marker (the fork's "basic"
          floor). What a failed digest falls back to anyway.
========  ==================================================================

Hard rules (C1), enforced here rather than hoped for:

1. **The lane's own model.** :class:`Scope` carries the role's
   ``(chat_client, model)`` and nothing else; there is no parameter that
   names another model. Diversity is poison here.
2. **One serial connection.** Every step runs after the previous one on
   the same client. Parallel critics wait for PolyKV (Milestone F).
3. **Its own stall budget.** Calls are stall-protected with the model's
   tuned thresholds (operator overrides ``compaction_stall_threshold_s`` /
   ``compaction_hard_s`` on ``runtime_control``) and their events carry
   ``phase: "compaction"``. A stalled step counts as a failed attempt; it
   never takes the lane down. Refusals (429 / 503) are waited out: without
   bound for the writer, at most :data:`OPTIONAL_REFUSAL_WAITS` times for
   the others, on the fork's 2–30 s backoff.
4. **Thinking off** for every call (fork ``f53b7ab2e``: a thinking
   synthesizer spent its budget reasoning and returned nothing).

Target, trigger and rescue (C3): the digest aims at a quarter of the free
content (:func:`consultants.engine.budget.compaction_target_tokens`), not
at the trigger. A keep-tail result still over the trigger is re-run with
no tail and the smaller result kept (fork: 34,297 → 17,217 tokens on a
32k window). A fold that does not shrink the history is discarded. One
fold at a time per lane.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from claude_hooks import budget_projection, capped_thinking, token_calib
from consultants.engine import budget
from consultants.engine import compaction_prompts as P
from consultants.engine import retrospective, tool_ledger, unread_files

log = logging.getLogger("consultants.engine.compaction")

# --------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------- #

FLOWS = ("reduced", "full", "basic")
PLACEMENTS = ("system", "user")
REASONING_REPLAY = ("none", "all", "inline")


@dataclass(frozen=True)
class Settings:
    """Compaction settings for one (role, model): shipped defaults with
    ``[compaction]``, ``[role.X.compaction]`` and
    ``[role.X.models."<m>"]`` merged over them, plus the run's stall
    overrides. Built by :func:`settings_from`."""
    #: ``full`` by the owner's ruling, 2026-10-24: on compaction-v7-g2 it
    #: carried 62 of 62 rows only the digest held and fixed the bug, where
    #: ``reduced`` got the fix wrong.
    flow: str = "full"
    #: Role of the message that replaces the folded span. ``system`` is
    #: ours; ``user`` is the fork's. An A/B on the C bench decides the
    #: default (plan §11.2).
    placement: str = "system"
    #: The reduced flow's reviewer. Off leaves writer + retrospective.
    review: bool = True
    target_share: float = budget.COMPACTION_TARGET_SHARE
    writer_attempts: int = 3
    ledger: bool = True
    # C5 / C4 knobs live here too so one table configures compaction.
    tool_result_max_chars: int = 32_000
    file_result_max_chars: int = 50_000
    stale_read_share: float = 0.4
    reasoning_replay: str = "none"
    #: The writer and critics as the lane's next turn (plan F3): ``auto``
    #: under PolyKV pooled / assured only, ``on`` on any backend (a
    #: prompt-cache continuation), ``off`` the transcript as text.
    continuation: str = "auto"
    # From runtime_control, not config: whether stall protection applies
    # (a v2 run), and the operator's overrides.
    stall: bool = False
    stall_threshold_s: Optional[float] = None
    hard_cap_s: Optional[float] = None
    stall_retries: int = 1


DEFAULT_SETTINGS = Settings()

#: Config keys and their validators live in ``consultants.config``
#: (``COMPACTION_KEYS``); these are the names :func:`settings_from` reads.
CONFIG_KEYS = ("flow", "placement", "review", "target_share",
               "writer_attempts", "ledger", "tool_result_max_chars",
               "file_result_max_chars", "stale_read_share",
               "reasoning_replay", "continuation")


def settings_from(rc: Optional[dict], role: Optional[str],
                  model: str) -> Settings:
    """The settings for ``(role, model)`` from a run's
    ``runtime_control``: ``rc["compaction"]`` layers ``*`` → ``<role>`` →
    ``<role>@<model>``, then the stall overrides."""
    rc = rc or {}
    layers = rc.get("compaction") or {}
    merged: dict = {}
    for key in ("*", role or "", f"{role}@{model}"):
        for k, v in (layers.get(key) or {}).items():
            if k in CONFIG_KEYS and v is not None:
                merged[k] = v
    if rc:
        merged["stall"] = True
        if rc.get("compaction_stall_threshold_s"):
            merged["stall_threshold_s"] = float(rc["compaction_stall_threshold_s"])
        if rc.get("compaction_hard_s"):
            merged["hard_cap_s"] = float(rc["compaction_hard_s"])
        if rc.get("stall_retries"):
            merged["stall_retries"] = int(rc["stall_retries"])
    return replace(DEFAULT_SETTINGS, **merged) if merged else DEFAULT_SETTINGS


# --------------------------------------------------------------------- #
# Calls: the lane's own client, serial, stall-protected, recorded
# --------------------------------------------------------------------- #

#: Refusal waits for every call but the writer, and their backoff
#: (fork ``continuation-compaction.ts:114-126``).
OPTIONAL_REFUSAL_WAITS = 3
REFUSAL_BACKOFF_S = (2, 4, 8, 15, 30)

_sleep = time.sleep  # a seam for tests


@dataclass
class Scope:
    """Who is compacting. The only model a compaction can use is
    ``model`` on ``chat_client``: the lane's own (C1)."""
    chat_client: Any
    model: str
    role: str = ""
    recorder: Any = None
    round: int = 1
    lane_idx: Optional[int] = None
    settings: Settings = DEFAULT_SETTINGS
    #: Every call made, in order: ``(step, model)``. Read by tests and
    #: the compaction report.
    calls: list = field(default_factory=list)
    #: The lane's tool schemas, its wire options (``num_ctx``) and what it
    #: does to its messages before sending them (the ``# Output Budget``
    #: section): a continuation sends exactly what the lane would.
    tools: Optional[list] = None
    options: Optional[dict] = None
    wire: Optional[Callable[[list], list]] = None
    #: This compaction's continuation, when it runs as one.
    cont: Optional["Continuation"] = None
    _fn: Any = None
    _step: str = ""


@dataclass
class Continuation:
    """The writer and the critics as the lane's next turn (plan F3, fork
    ``continuation-compaction.ts``): the lane's own messages and tools
    with the instruction on the last of them, so the server shares the
    stream it already holds instead of prefilling the transcript again as
    text (:func:`with_instruction`)."""
    messages: list
    reason: str
    tools: Optional[list] = None
    options: dict = field(default_factory=dict)
    #: The writer wrote: the critics may follow it.
    writer_text: str = ""
    #: The engine session the writer went to, dropped by :func:`release`.
    session: Optional[str] = None
    released: bool = False


def with_instruction(messages: list, instruction: str) -> list:
    """``messages`` with ``instruction`` on the content of the last one
    when that is a tool result or a user turn, else as a harness turn.

    Never as a new user turn after the tool loop: a Qwen-style template
    renders every assistant turn after the last user query with its
    ``<think></think>`` block, so a new query re-renders the whole loop
    and the stream the lane's slot holds stops matching right after the
    question (eleven2go render, 2026-10-24). On the last tool result the
    lane's stream is a byte prefix of the request — the harness's own
    ``[SYSTEM MESSAGE]`` convention for notes riding on a tool result."""
    from claude_hooks.agent_loop.runner import HARNESS_TAG, harness_note
    out = list(messages)
    last = out[-1] if out else None
    if last is not None and last.get("role") in ("tool", "user") \
            and isinstance(last.get("content"), str):
        out[-1] = {**last, "content": (last["content"].rstrip() + "\n\n"
                                       + HARNESS_TAG + " " + instruction)}
        return out
    return out + [harness_note(instruction)]


@dataclass
class CallResult:
    text: str = ""
    completion_tokens: int = 0
    reasoning_chars: int = 0
    done_reason: str = ""
    error: str = ""
    tool_calls: list = field(default_factory=list)


def _is_refusal(exc: BaseException) -> bool:
    return (getattr(exc, "fault_class", "") == "refusal"
            or getattr(exc, "status", None) in (429, 503))


def _chat_fn(scope: Scope) -> Callable[[dict], dict]:
    if scope._fn is not None:
        return scope._fn
    client = scope.chat_client
    fn = client.chat
    s = scope.settings
    if s.stall and hasattr(client, "chat_streamed"):
        try:
            from consultants.engine.stall_chat import stall_protected_chat_fn_for
            from consultants.engine.stall_defaults import (
                HARD_CAP_REFERENCE_OUTPUT_TOKENS, runtime_stall_thresholds)
            tuned = runtime_stall_thresholds(scope.model)

            def sink(ev: dict) -> None:
                rec = scope.recorder
                if rec is None or not hasattr(rec, "record_event"):
                    return
                try:
                    rec.record_event(
                        kind=ev.get("kind") or "stall.event", role=scope.role,
                        round=scope.round, lane_idx=scope.lane_idx,
                        payload={**ev, "phase": "compaction",
                                 "step": scope._step})
                except Exception:  # pragma: no cover
                    log.exception("recorder.record_event raised; ignored")

            fn = stall_protected_chat_fn_for(
                client,
                stall_threshold_s=float(s.stall_threshold_s
                                        or tuned.stall_threshold_s),
                hard_cap_s=float(s.hard_cap_s or tuned.hard_cap_s),
                retries=int(s.stall_retries or 1), on_event=sink,
                hard_cap_reference_output=(
                    None if s.hard_cap_s else HARD_CAP_REFERENCE_OUTPUT_TOKENS))
        except Exception:  # pragma: no cover — plumbing never blocks a fold
            log.exception("compaction: stall wrapper failed; calling unwrapped")
            fn = client.chat
    scope._fn = fn
    return fn


def call(scope: Scope, step: str, system: str, user: str, max_tokens: int,
         *, refusal_waits: Optional[int] = OPTIONAL_REFUSAL_WAITS,
         cont: Optional[Continuation] = None) -> CallResult:
    """One compaction call on the lane's own model, thinking off.

    Never raises: a step that fails returns ``CallResult(error=...)`` and
    the pipeline keeps what it had. ``refusal_waits`` ``None`` waits a
    refusal out without bound (the writer).

    With ``cont`` the call continues the lane: its messages with ``system``
    and ``user`` as the instruction on the last of them
    (:func:`with_instruction`) — the lane's own system prompt stays in
    place, so the role travels in the instruction (fork
    ``continuation.write``) — with the lane's tools and options."""
    scope._step = step
    if cont is None:
        payload = {
            "model": scope.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {"num_predict": max(1, int(max_tokens))},
        }
    else:
        payload = {
            "model": scope.model,
            "messages": with_instruction(cont.messages, f"{system}\n\n{user}"),
            "stream": False,
            "think": False,
            "options": {**cont.options, "num_predict": max(1, int(max_tokens))},
        }
        if cont.tools:
            payload["tools"] = cont.tools
    fn = _chat_fn(scope)
    waits = 0
    answered = False
    while True:
        scope.calls.append((step, scope.model))
        t0 = time.monotonic()
        try:
            resp = fn(payload)
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            _record(scope, step, payload, None, ms, f"{type(exc).__name__}: {exc}")
            if _is_refusal(exc) and (refusal_waits is None
                                     or waits < refusal_waits):
                _sleep(REFUSAL_BACKOFF_S[min(waits, len(REFUSAL_BACKOFF_S) - 1)])
                waits += 1
                continue
            log.warning("compaction %s failed: %s", step, exc)
            return CallResult(error=f"{type(exc).__name__}: {exc}")
        ms = int((time.monotonic() - t0) * 1000)
        _record(scope, step, payload, resp, ms, None)
        r = _parse(resp)
        if cont is not None and not r.text and r.tool_calls and not answered:
            # The lane's tools stay on offer so its prefix still matches,
            # and Ollama has no tool_choice "none": a call is answered as
            # not run and the turn asked again, once, still a continuation
            # (live: three writer attempts spent on calls, run4).
            answered = True
            payload = {**payload, "messages": [
                *payload["messages"],
                {"role": "assistant", "content": "", "tool_calls": r.tool_calls},
                *({"role": "tool", "content": CONTINUATION_TOOLS_OFF,
                   **({"tool_call_id": tc["id"]} if tc.get("id") else {})}
                  for tc in r.tool_calls)]}
            continue
        return r


def _parse(resp: dict) -> CallResult:
    resp = resp or {}
    choices = resp.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else (
        resp.get("message") or {})
    text = str(msg.get("content") or "").strip()
    thinking = capped_thinking.thinking_text(msg) or ""
    usage = resp.get("usage") or {}
    ct = int(usage.get("completion_tokens") or resp.get("eval_count") or 0)
    done = str((choices[0].get("finish_reason") if choices else "")
               or resp.get("done_reason") or "")
    return CallResult(text=text, completion_tokens=ct,
                      reasoning_chars=len(thinking), done_reason=done,
                      tool_calls=list(msg.get("tool_calls") or []))


def _record(scope: Scope, step: str, payload: dict, resp: Optional[dict],
            ms: int, error: Optional[str]) -> None:
    rec = scope.recorder
    if rec is None or not hasattr(rec, "record_llm"):
        return
    usage = (resp or {}).get("usage") or {}
    try:
        rec.record_llm(
            role=f"{scope.role or 'role'}:compaction", round=scope.round,
            lane_idx=scope.lane_idx, model=scope.model,
            request={**payload, "compaction_step": step}, response=resp,
            prompt_tokens=int(usage.get("prompt_tokens") or 0) or None,
            completion_tokens=int(usage.get("completion_tokens") or 0) or None,
            duration_ms=ms, error=error)
    except Exception:  # pragma: no cover — recording never fails a fold
        log.exception("recorder.record_llm raised; ignored")


# --------------------------------------------------------------------- #
# What the writer reads
# --------------------------------------------------------------------- #

#: How much of one message reaches the writer; reasoning per block.
MAX_MESSAGE_CHARS = 2_000
MAX_THINKING_CHARS = 2_000
MAX_CALL_ARGS_CHARS = 400


def _cut(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else (
        text[:limit] + f" […{len(text) - limit} more chars]")


def _text(message: dict) -> str:
    content = (message or {}).get("content")
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content
                        if isinstance(b, dict))
    return str(content or "")


def serialize(messages: list[dict],
              numbers: Optional[dict[str, int]] = None) -> str:
    """The folded span as the writer reads it.

    The line markers are the fork's (``[User]:``, ``[Bot]:``,
    ``[Bot thinking]:``, ``[Bot tool calls]:``, ``[Tool result]:``):
    strings the harness writes and a replay has no reason to produce, so
    :func:`cut_echo` can key on them. Earlier digests are not serialized;
    the writer gets the previous one as ``Previous summary:``.

    With ``numbers`` (:func:`tool_ledger.numbers`) each call carries the
    key's number, ``[Bot tool calls]: #7 read_file(…)``: a writer that had
    to count calls to cite them miscounted on long runs of alike reads."""
    lines: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict) or budget.is_compaction_marker(m):
            continue
        role = m.get("role")
        if role == "tool":
            lines.append(f"[Tool result]: {_cut(_text(m).strip(), MAX_MESSAGE_CHARS)}")
            continue
        who = {"user": "User", "system": "Harness note"}.get(role, "Bot")
        if role == "assistant":
            thinking = capped_thinking.thinking_text(m)
            if thinking:
                lines.append(f"[Bot thinking]: {_cut(thinking, MAX_THINKING_CHARS)}")
        text = _text(m).strip()
        if text:
            lines.append(f"[{who}]: {_cut(text, MAX_MESSAGE_CHARS)}")
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            n = (numbers or {}).get((tc or {}).get("id") or "")
            lines.append(f"[Bot tool calls]: {f'#{n} ' if n else ''}"
                         f"{fn.get('name') or '?'}("
                         f"{_cut(tool_ledger.summarize_input(fn.get('arguments')), MAX_CALL_ARGS_CHARS)})")
    return "\n".join(lines).strip()


# --- verbatim user requests (02 P1-d) --------------------------------- #

USER_REQUEST_SHARE = 0.15
MAX_USER_REQUEST_CHARS = 2_000
MIN_USER_REQUEST_CHARS = 200


def collect_user_requests(messages: list[dict]) -> list[str]:
    out: list[str] = []
    for m in messages or []:
        if not budget.is_typed_prompt(m):
            continue
        text = _text(m).strip()
        if text and (not out or out[-1] != text):
            out.append(text)
    return out


def merge_requests(previous: list[str], new: list[str]) -> list[str]:
    """Appended, not merged by content: the same sentence typed at two
    points is two instructions; only an immediate repeat is one."""
    merged = list(previous or [])
    for r in new or []:
        if not merged or merged[-1] != r:
            merged.append(r)
    return merged


def elide_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(40, limit // 2 - 30)
    head, tail = text[:keep].rstrip(), text[-keep:].lstrip()
    return (f"{head}\n… {len(text) - len(head) - len(tail)} characters "
            f"elided by the harness …\n{tail}")


def render_requests(requests: list[str], budget_chars: int) -> list[str]:
    """Every request, never dropped: each gets an even share, clamped to
    [200, 2000] characters, and is elided in the middle beyond it."""
    kept = [r.strip() for r in requests or [] if r and r.strip()]
    if not kept:
        return []
    per = min(MAX_USER_REQUEST_CHARS,
              max(MIN_USER_REQUEST_CHARS, int(budget_chars) // len(kept)))
    return [elide_middle(r, per) for r in kept]


def requests_block(requests: list[str]) -> str:
    return "\n".join(f"<user_request>\n{r}\n</user_request>" for r in requests)


# --- output hygiene (02 P1-c) ----------------------------------------- #

_ECHO = re.compile(r"^(?:Conversation:|Previous summary:|\[Bot tool calls\]:"
                   r"|\[Tool result\]:)")
_FENCE = re.compile(r"^[ \t]*```")


def cut_echo(text: str) -> tuple[str, int]:
    """Cut the transcript a digest copied back out of its own request: at
    the first line outside a fence that starts with one of the request's
    own markers. ``[User]:`` / ``[Bot]:`` are deliberately not markers —
    ordinary prose can say them. The fork measured 8,925 of 13,846 stored
    characters as echo."""
    in_fence = False
    offset = 0
    for line in (text or "").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and _ECHO.match(line):
            kept = text[:offset].rstrip()
            return kept, len(text) - len(kept)
        offset += len(line) + 1
    return text or "", 0


MAX_FENCE_CHARS = 1_200
_FENCED = re.compile(r"^([ \t]*```[^\n]*\n)([\s\S]*?)(^[ \t]*```[ \t]*$|\Z)",
                     re.M)


def trim_fences(text: str, limit: int = MAX_FENCE_CHARS) -> tuple[str, int]:
    """Elide the middle of fenced blocks over ``limit`` characters — the
    file bodies a replay pastes back although asked not to. Prose is never
    trimmed; an unterminated fence runs to the end."""
    trimmed = 0

    def sub(m: re.Match) -> str:
        nonlocal trimmed
        opening, body, closing = m.group(1), m.group(2), m.group(3)
        if len(body) <= limit:
            return m.group(0)
        trimmed += 1
        keep = max(80, limit // 2 - 40)
        head, tail = body[:keep].rstrip(), body[-keep:].lstrip()
        dropped = len(body) - len(head) - len(tail)
        middle = (f"\n… {dropped} characters elided by the harness — this "
                  f"block was not trimmed, and the content is still on disk …\n")
        close = closing if closing.strip() == "```" else "```"
        return f"{opening}{head}{middle}{tail}\n{close}"

    return _FENCED.sub(sub, text or ""), trimmed


def _ensure_files(text: str, dropped: list[dict]) -> str:
    """The files the span touched, appended when the digest lost them:
    losing track of which files are in play is the one failure that makes
    a digest actively misleading."""
    ops = retrospective.collect_file_ops(dropped)
    if not (ops.read or ops.edited) or re.search(r"(?mi)^#+\s*files\b", text):
        return text
    return (text.rstrip() + "\n\n## Files\n"
            f"Read: {', '.join(ops.read) or 'none'}\n"
            f"Edited: {', '.join(ops.edited) or 'none'}")


# --------------------------------------------------------------------- #
# The writer (02 P1-b)
# --------------------------------------------------------------------- #

#: The writer's output floor (the fork's DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS),
#: never above the target. With the target now a quarter of the free
#: content, a small window would otherwise get a replay too short to carry
#: a long tool loop.
SUMMARY_MIN_OUTPUT_TOKENS = 4_096

#: The most of the writer's input the call key may take before its inputs
#: are cut (fork TOOL_LEDGER_KEY_SHARE). A long fold's key reached 23,000
#: characters in the fork's replay.
KEY_SHARE = 0.1


def build_writer_request(prompt: str, conversation: str, *,
                         previous_summary: str = "",
                         requests: Optional[list[str]] = None,
                         ledger_key: str = "",
                         in_context: str = "") -> str:
    """The writer's request. ``in_context`` names the span when the
    writer reads the transcript as its own conversation above (plan F3):
    it takes the place of the ``Conversation:`` section, since pasting the
    transcript again is the prefill a continuation exists to avoid."""
    if in_context:
        prompt = P.in_context(prompt)
    parts = [prompt]
    if previous_summary.strip():
        parts.append("Previous summary:\n" + previous_summary.strip())
    if requests:
        parts.append(
            "What the user asked, in their own words. The harness quoted "
            "these; they are not always present in the transcript, and they "
            "are what you must reproduce verbatim rather than describe:\n\n"
            + requests_block(requests))
    if ledger_key.strip():
        where = ("in the order they appear in the conversation above"
                 if in_context else "in the conversation below as well")
        parts.append(f"The calls you made, numbered. Each one carries its "
                     f"number {where}. Cite these "
                     "as `[#N]` in your replay rather than writing them out:\n"
                     + ledger_key.strip())
    if in_context:
        parts.append(in_context)
    else:
        parts.append("Conversation:\n" + (conversation or "(empty)"))
    return "\n\n".join(parts)


def _opening(message: Optional[dict], limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", serialize([message] if message else [])).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def describe_span(messages: list[dict], dropped: list[dict],
                  pinned: Optional[dict] = None) -> str:
    """Which part of the conversation above the replay covers (fork
    ``describeReplaySpan``): from the first folded message up to the first
    one kept after it, which stays word for word after the replay."""
    ids = {id(m) for m in dropped}
    last = max((i for i, m in enumerate(messages) if id(m) in ids), default=-1)
    after = next((m for m in messages[last + 1:]
                  if m is not pinned), None) if last >= 0 else None
    start = (f" from the message that begins «{_opening(dropped[0])}»"
             if dropped else "")
    if after is None:
        first = (f"The replay covers the conversation above{start} to its "
                 f"end." if start else
                 "The replay covers everything above this message.")
    else:
        first = (f"The replay covers the conversation above{start} up to, "
                 f"and not including, the message that begins "
                 f"«{_opening(after)}». That message and everything after it "
                 "stay in your context word for word, directly after what "
                 "you write, so the replay ends where it begins.")
    lines = [first]
    if pinned is not None:
        lines.append(f"The user's message that begins «{_opening(pinned)}» "
                     "also stays word for word, directly after the replay.")
    lines.append("The instructions and the question at the top of the "
                 "conversation stay as well. Write text only: do not call "
                 "any tool.")
    return "\n\n".join(lines)


def _note_session(scope: Scope, cont: Continuation) -> None:
    last = getattr(scope.chat_client, "last_placement", None)
    placement = last() if callable(last) else None
    if placement and placement.get("session_id"):
        cont.session = placement["session_id"]


def release(scope: Scope) -> None:
    """Give the lane's slot back once the critics are done (fork step 5,
    "erase the slot rather than close it"): its cells hold the transcript
    the replay is about to replace, and the layer covers the head of the
    lane's next call. The session stays running. Idempotent; never
    raises."""
    cont = scope.cont
    if cont is None or cont.released:
        return
    cont.released = True
    mgr = getattr(scope.chat_client, "_placed_manager", None)
    if mgr is None or not cont.session:
        return
    try:
        mgr.erase_session(cont.session)
    except Exception:  # noqa: BLE001 — a release never fails a fold
        log.exception("compaction: releasing %s failed", cont.session)


def write_replay(scope: Scope, request: str, *, system: str,
                 max_tokens: int, model: str, tail: bool,
                 calls: int = 0) -> tuple[str, dict]:
    """Up to ``writer_attempts`` calls. A throw or an empty reply is resent
    as is; an over-budget one is resent with the measured overrun, and one
    citing past the ``calls``-entry key with the numbers it invented. An
    over-budget or past-key text on the last attempt is kept (the caller
    cuts what is past the key); an empty one never."""
    attempts = max(1, int(scope.settings.writer_attempts))
    info: dict = {"attempts": 0, "echo_cut": 0, "trimmed_blocks": 0}
    best = ""
    ask = request
    cont = scope.cont
    for n in range(attempts):
        info["attempts"] = n + 1
        r = call(scope, f"writer#{n + 1}", system, ask, max_tokens,
                 refusal_waits=None, cont=cont)
        if cont is not None:
            _note_session(scope, cont)
        if r.error:
            info["last_failure"] = "threw"
            continue
        if cont is not None and r.text.strip():
            cont.writer_text = r.text.strip()
        text, cut = cut_echo(r.text)
        info["echo_cut"] += cut
        if tail:
            text, trimmed = trim_fences(text)
            info["trimmed_blocks"] += trimmed
        text = text.strip()
        if not text:
            info["last_failure"] = "empty"
            if r.reasoning_chars:
                log.warning("compaction writer returned no text "
                            "(likelyCause=output_budget_consumed_by_reasoning, "
                            "%d reasoning chars)", r.reasoning_chars)
            continue
        if r.done_reason == "length":
            log.warning("compaction writer was cut by its output cap; keeping "
                        "the text it wrote")
        measured = token_calib.estimate_tokens(len(text), model)
        if measured > max_tokens:
            best = text
            info["last_failure"] = "over_budget"
            ask = request + P.describe_overrun(measured, max_tokens)
            continue
        invented = tool_ledger.past_key(text, calls) if calls else []
        if invented and n + 1 < attempts:
            best = text
            info["last_failure"] = "past_key"
            info["past_key"] = info.get("past_key", 0) + len(invented)
            ask = request + P.describe_past_key(invented, calls)
            continue
        return text, info
    if best:
        log.warning("compaction writer stayed over budget after %d attempts; "
                    "keeping the last", attempts)
    return best, info


# --------------------------------------------------------------------- #
# Review: the reduced flow's reviewer, the full flow's council
# --------------------------------------------------------------------- #

#: A merge shorter than this share of the original is a collapse, not a
#: correction; the original is kept (fork COUNCIL_MIN_MERGE_RATIO).
MIN_MERGE_RATIO = 0.5
#: The reviewer may grow the replay this much (prompted, as in the fork).
MAX_GROWTH = 1.1
HALF_MARKER_BAND = 0.3
#: What a continuing critic or reviewer is told in place of the pasted
#: transcript.
CONTINUATION_TOOLS_OFF = ("Not run: tools are off for this turn. Write the "
                          "text you were asked for, from what is already "
                          "in front of you.")
CONTINUATION_SOURCE = ("The transcript the replay was written from is the "
                       "conversation above these instructions. Write text "
                       "only: do not call any tool.")


def _continuing(scope: Scope) -> Optional[Continuation]:
    """The continuation a reviewer or critic follows: only once a writer
    turn exists to continue from, and while the lane's slot is held."""
    cont = scope.cont
    if cont is None or cont.released or not cont.writer_text:
        return None
    return cont
_HALF_LINE = re.compile(r"^[ \t]*<<<\s*HALFWAY\s*>>>[ \t]*$", re.M)


def strip_half_marker(text: str) -> str:
    return _HALF_LINE.sub("", text or "").strip()


def split_at_marker(replay: str) -> Optional[tuple[str, str, str]]:
    """``(first, second, source)``: the writer's marker when it lands in
    the middle 40%, else the blank line nearest the middle, else any line."""
    body = (replay or "").strip()
    if not body:
        return None
    m = _HALF_LINE.search(body)
    if m:
        first, second = body[:m.start()].strip(), body[m.end():].strip()
        total = len(first) + len(second)
        if first and second and HALF_MARKER_BAND <= len(first) / total <= 1 - HALF_MARKER_BAND:
            return first, second, "marker"
    body = strip_half_marker(body)
    mid = len(body) / 2
    for pattern in (r"\n[ \t]*\n", r"\n"):
        points = [x.end() for x in re.finditer(pattern, body)]
        if points:
            at = min(points, key=lambda p: abs(p - mid))
            first, second = body[:at].strip(), body[at:].strip()
            return (first, second, "rebalanced") if first and second else None
    return None


def parse_sections(text: str) -> dict:
    heads = list(re.finditer(r"(?im)^#{1,4}\s*(replay|retrospective)\s*$", text or ""))
    out: dict = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[h.end():end].strip()
        if body:
            out[h.group(1).lower()] = body
    return out


def _answer_format(with_retro: bool) -> list[str]:
    if with_retro:
        return [P.RETROSPECTIVE_REVISION, "",
                "Answer with exactly these two sections and nothing before, "
                "between or after them:", "", "## Replay", "",
                "## Retrospective"]
    return ["Answer with exactly this section and nothing before or after it:",
            "", "## Replay"]


def _accept_merge(merged_text: str, original: str, retro: str,
                  step: str) -> tuple[str, str, bool]:
    sections = parse_sections(merged_text)
    replay = strip_half_marker(sections.get("replay")
                               or ("" if retro else merged_text))
    replay, _ = cut_echo(replay)
    if not replay:
        log.warning("compaction %s returned no replay; keeping the original", step)
        return original, retro, False
    if len(replay) < len(original) * MIN_MERGE_RATIO:
        log.warning("compaction %s returned %d chars for a %d-char replay; "
                    "keeping the original", step, len(replay), len(original))
        return original, retro, False
    if len(replay) > len(original) * MAX_GROWTH * 1.25:
        log.info("compaction %s grew the replay %d -> %d chars", step,
                 len(original), len(replay))
    new_retro = (sections.get("retrospective") or "").strip()
    if new_retro:
        new_retro, _ = cut_echo(new_retro)
    return replay, new_retro or retro, True


def review(scope: Scope, replay: str, retro: str, *, transcript: str,
           ledger_key: str, max_tokens: int) -> tuple[str, str, dict]:
    """The reduced flow's third call: one reviewer of the whole replay."""
    original = strip_half_marker(replay)
    parts = [P.render(P.REVIEWER_PROMPT, original_length=len(original),
                      max_length=round(len(original) * MAX_GROWTH)), ""]
    parts += _answer_format(bool(retro.strip()))
    parts += ["", "---", "", "The replay:", "", original]
    if retro.strip():
        parts += ["", "---", "", "**The retrospective, as written:**", "",
                  retro.strip()]
    if ledger_key.strip():
        parts += ["", "---", "", "The numbered record the citations point at:",
                  "", ledger_key.strip()]
    cont = _continuing(scope)
    if cont is not None:
        parts[0] = P.in_context(parts[0])
        parts += ["", "---", "", CONTINUATION_SOURCE]
    else:
        parts += ["", "---", "", "The transcript the replay was written from:",
                  "", transcript or "(empty)"]
    r = call(scope, "reviewer", P.REVIEWER_SYSTEM_PROMPT, "\n".join(parts),
             max_tokens, cont=cont)
    release(scope)
    if r.error or not r.text:
        return original, retro, {"reviewed": False}
    new_replay, new_retro, merged = _accept_merge(r.text, original, retro,
                                                  "reviewer")
    return new_replay, new_retro, {"reviewed": merged}


def council(scope: Scope, replay: str, retro: str, *, transcript: str,
            ledger_key: str, max_tokens: int) -> tuple[str, str, dict]:
    """The full flow's three calls: two half-critics, serially (C1.2),
    each correcting the *original*, then the synthesizer."""
    halves = split_at_marker(replay)
    if halves is None:
        release(scope)
        return strip_half_marker(replay), retro, {"reviewers": 0, "merged": False}
    first, second, source = halves
    rewritten: dict = {}
    cont = _continuing(scope)
    for half, own, other in (("first", first, second), ("second", second, first)):
        other_name = "second" if half == "first" else "first"
        req = [P.render(P.CRITIC_PROMPT, half=half, other_half=other_name,
                        half_length=len(own)), "", "---", "",
               f"The {half} half of the replay — this is yours, rewrite it:",
               "", own, "", "---", "",
               f"The {other_name} half of the replay — reference only, someone "
               f"else owns it, do not return it:", "", other or "(empty)", "",
               "---", ""]
        if ledger_key.strip():
            req += ["The numbered record the citations point at:", "",
                    ledger_key.strip(), "", "---", ""]
        if cont is not None:
            req[0] = P.in_context(req[0])
            req += [CONTINUATION_SOURCE]
        else:
            req += ["The transcript both halves were written from:", "",
                    transcript or "(empty)"]
        # The summary's own cap, as the fork's critics get: a half asked to
        # come back within 10% of its length must not be cut short by a cap.
        # Serial, each as the lane's next turn: the second finds the stream
        # the writer and the first shared still resident.
        r = call(scope, f"critic:{half}", P.CRITIC_SYSTEM_PROMPT, "\n".join(req),
                 max_tokens, cont=cont)
        if r.error or not r.text:
            continue
        text, _ = cut_echo(r.text)
        emitted = split_at_marker(text)
        # Splice back only on a real marker: a rebalance of a correct half
        # would halve it (fork: every "the writer deleted instead of
        # revising" measurement was this).
        kept = strip_half_marker(
            (emitted[0] if half == "first" else emitted[1])
            if emitted and emitted[2] == "marker" else text)
        if kept:
            rewritten[half] = kept
    # The synthesizer reads the halves, not the transcript: it runs on a
    # request of its own, after the lane's slot is given back.
    release(scope)
    if not rewritten:
        return strip_half_marker(replay), retro, {"reviewers": 0, "merged": False,
                                                  "split": source}
    original_length = len(first) + len(second)
    req = [P.render(P.SYNTHESIZER_PROMPT, original_length=original_length,
                    max_length=round(original_length * MAX_GROWTH)), ""]
    req += _answer_format(bool(retro.strip()))
    req += ["", "---", "", "**First half — as originally written:**", "", first,
            "", "**First half — as rewritten:**", "",
            rewritten.get("first", first), "", "---", "",
            "**Second half — as originally written:**", "", second, "",
            "**Second half — as rewritten:**", "",
            rewritten.get("second", second)]
    if retro.strip():
        req += ["", "---", "", "**The retrospective, as written:**", "",
                retro.strip()]
    r = call(scope, "synthesizer", P.SYNTHESIZER_SYSTEM_PROMPT, "\n".join(req),
             max_tokens)
    original = strip_half_marker(replay)
    if r.error or not r.text:
        return original, retro, {"reviewers": len(rewritten), "merged": False,
                                 "split": source}
    new_replay, new_retro, merged = _accept_merge(r.text, original, retro,
                                                  "synthesizer")
    return new_replay, new_retro, {"reviewers": len(rewritten),
                                   "merged": merged, "split": source}


# --------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------- #

def _capped(messages: list[dict]) -> list[dict]:
    """Each message cut to what :func:`serialize` shows, so the input
    projection prices what the writer reads rather than raw tool output
    (a 21k-char read priced in full pushed whole calls out)."""
    out = []
    for m in messages or []:
        if isinstance(m, dict) and isinstance(m.get("content"), str) \
                and len(m["content"]) > MAX_MESSAGE_CHARS:
            m = {**m, "content": _cut(m["content"], MAX_MESSAGE_CHARS)}
        out.append(m)
    return out


def transcript_budget(context_tokens: int, *, fixed_request: str,
                      summary_max: int, model: str) -> int:
    """How much transcript the compaction calls can carry: the window less
    what the largest request carrying it also holds — the reviewer's
    prompt, the replay it revises (up to ``summary_max``), the fixed part
    of the writer's request, and the reply (``summary_max``) — less the
    margin. The fork gives the writer the summarizer's whole input limit
    (agentic-compaction.ts:673-702); a fixed half of the digest target
    (≈3.8k tokens of a 32k window) dropped the oldest calls of a long span
    before the writer saw them."""
    fixed = token_calib.estimate_tokens(
        len(fixed_request) + len(P.REVIEWER_PROMPT) + len(P.CRITIC_PROMPT) // 2,
        model)
    return int(context_tokens) - fixed - 2 * int(summary_max) \
        - budget.WINDOW_MARGIN_TOKENS


def _continuation(scope: Scope, conversation: list[dict], system: str,
                  request: str, summary_max: int,
                  context_tokens: Optional[int]
                  ) -> tuple[Optional[Continuation], str]:
    """The continuation the writer would run as, or ``None`` and why not:
    only when the lane's prompt, the instruction and the summary fit the
    window (fork: run 0432 let a 92,738-token writer through at an
    85,106-token window by counting neither)."""
    messages = scope.wire(conversation) if scope.wire else list(conversation)
    if context_tokens:
        need = (budget.estimate_messages_tokens(messages, scope.model)
                + budget.estimate_tools_tokens(scope.tools, scope.model)
                + token_calib.estimate_tokens(len(system) + len(request) + 2,
                                              scope.model)
                + int(summary_max))
        if need > int(context_tokens):
            return None, (f"no room in the window for the writer ({need} > "
                          f"{int(context_tokens)}); sent as text")
    return Continuation(messages=messages, tools=scope.tools,
                        options=dict(scope.options or {}),
                        reason="continuation"), "continuation"


def write_digest(scope: Scope, dropped: list[dict], *, target_tokens: int,
                 generation: int = 1,
                 previous: Optional[retrospective.Digest] = None,
                 pinned: Optional[dict] = None,
                 tail: bool = True,
                 context_tokens: Optional[int] = None,
                 task: Optional[dict] = None,
                 conversation: Optional[list[dict]] = None,
                 ) -> retrospective.Digest:
    """Writer → retrospective → review, on the lane's own model.

    The writer writes first against 70% of the generation's combined
    budget (floored at :data:`SUMMARY_MIN_OUTPUT_TOKENS`); the
    retrospective gets what is left, with no floor (an owner rule:
    the replay is load-bearing, the assessment is not)."""
    s = scope.settings
    model = scope.model
    if not dropped:
        return retrospective.Digest(generation=generation,
                                    stand_down="nothing-dropped")
    if scope.chat_client is None:
        return retrospective.Digest(generation=generation, dropped=len(dropped),
                                    stand_down="no-summariser")
    budgets = retrospective.resolve_output_budgets(target_tokens=target_tokens,
                                                   generation=generation)
    summary_max = max(budgets.summary_max_tokens,
                      min(SUMMARY_MIN_OUTPUT_TOKENS, max(1, target_tokens)))
    input_budget = max(retrospective.MIN_INPUT_TOKENS,
                       int(target_tokens * retrospective.MAX_INPUT_SHARE_OF_TARGET))

    # Requests: stored ones carried + the span's; the task (the head's
    # question) and the pinned prompt are shown to the writer but not
    # stored — both survive verbatim around the digest. The writer cannot
    # see either otherwise, and a writer that does not know the task takes
    # one from the prompt's examples ("I am fixing the collision") and
    # summarises the work against it (compaction bench, 2026-10-19; the
    # fork pins and shows its first prompt for the same reason,
    # agentic-compaction.ts:579-605).
    stored_requests = merge_requests(previous.user_requests if previous else [],
                                     collect_user_requests(dropped))
    shown = list(stored_requests)
    if task is not None and budget.is_typed_prompt(task):
        shown = merge_requests([_text(task).strip()], shown)
    if pinned is not None and budget.is_typed_prompt(pinned):
        shown = merge_requests(shown, [_text(pinned).strip()])
    shown = render_requests(shown, int(input_budget * 3 * USER_REQUEST_SHARE))

    # Ledger: the carried calls, evicted to budget, then every call of this
    # span. The span's calls are never evicted from the key: the writer
    # reads them in the transcript, and a key that dropped the oldest and
    # renumbered the rest (≈6 of 9 at a 32k window, priced at the results
    # the key does not even show) had the writer cite by counting and
    # narrate calls it never made (compaction bench trace, 2026-10-19).
    # The budget bounds what is carried to the next generation instead.
    ledger: list[tool_ledger.Entry] = []
    ledger_budget = tool_ledger.budget_chars(target_tokens)
    if s.ledger:
        carried = [tool_ledger.Entry.from_dict(d)
                   for d in (previous.ledger if previous else [])]
        ledger = tool_ledger.merge(tool_ledger.evict(carried, ledger_budget),
                                   tool_ledger.build(dropped))
    # Bounded by shortening inputs, never by leaving calls out, at a tenth
    # of the writer's input (fork TOOL_LEDGER_KEY_SHARE); it is part of the
    # fixed request the transcript budget is measured against.
    key = tool_ledger.render_key(ledger, max(
        ledger_budget, int((context_tokens or input_budget) * 3 * KEY_SHARE)))

    full_flow = s.flow == "full" and tail
    prompt = P.REPLAY_PROMPT if tail else P.FULL_PROMPT
    if full_flow:
        prompt = P.with_halfway_section(prompt)
    system = P.WRITER_SYSTEM_PROMPT if tail else P.FULL_SYSTEM_PROMPT

    def as_text() -> tuple[str, str]:
        writer_budget = input_budget
        if context_tokens:
            fixed = build_writer_request(
                prompt, "", previous_summary=previous.summary if previous else "",
                requests=shown, ledger_key=key)
            writer_budget = max(input_budget, transcript_budget(
                context_tokens, fixed_request=fixed, summary_max=summary_max,
                model=model))
        view = budget_projection.project(_capped(dropped),
                                         target_tokens=writer_budget,
                                         policy_intent="summary_input",
                                         model=model)
        retrospective._log_projection("writer", view)
        text = serialize(view.messages, numbers=tool_ledger.numbers(ledger))
        return build_writer_request(
            prompt, text, previous_summary=previous.summary if previous else "",
            requests=shown, ledger_key=key), text

    cinfo: dict = {}
    replay, winfo = "", {}
    if conversation is not None:
        # The previous summary is in the conversation the writer reads;
        # quoting it again would pay for it twice.
        request = build_writer_request(
            prompt, "", requests=shown, ledger_key=key,
            in_context=describe_span(conversation, dropped, pinned))
        cont, why = _continuation(scope, conversation, system, request,
                                  summary_max, context_tokens)
        cinfo = {"continuation": why}
        if cont is not None:
            scope.cont = cont
            replay, winfo = write_replay(
                scope, request, system=system, max_tokens=summary_max,
                model=model, tail=tail, calls=len(ledger))
            if not replay:
                # A continuation that wrote nothing hands the fold back to
                # the transcript-as-text path it replaced (fork).
                release(scope)
                scope.cont = None
                cinfo["continuation"] = (f"{why}; the writer wrote nothing, "
                                         "sent as text")
    transcript = ""
    if scope.cont is None:
        request, transcript = as_text()
        replay, winfo = write_replay(
            scope, request, system=system, max_tokens=summary_max,
            model=model, tail=tail, calls=len(ledger))

    retro = ""
    spent = token_calib.estimate_tokens(len(replay), model) if replay else 0
    retro_budget = retrospective.resolve_thinking_max_tokens(budgets, spent)
    retro_view = budget_projection.project(
        dropped, target_tokens=input_budget,
        policy_intent="retrospective_input", model=model)
    reasoning = retrospective.serialize_reasoning_with_outcomes(retro_view.messages)
    if reasoning and retro_budget >= retrospective.MIN_PHASE_OUTPUT_TOKENS:
        r = call(scope, "retrospective", P.RETROSPECTIVE_SYSTEM_PROMPT,
                 retrospective.build_retrospective_prompt(
                     retro_view.messages,
                     previous_retrospective=previous.retrospective if previous else None),
                 retro_budget)
        retro, _ = cut_echo(r.text)
        if retro and capped_thinking.is_degenerate_note(retro):
            log.warning("retrospective degenerated into repetition; dropped")
            retro = ""
    elif reasoning:
        log.info("retrospective skipped: the replay spent %d of %d, leaving %d",
                 spent, budgets.combined_tokens, retro_budget)

    rinfo: dict = {}
    if replay and tail:
        if full_flow:
            replay, retro, rinfo = council(scope, replay, retro,
                                           transcript=transcript,
                                           ledger_key=key, max_tokens=summary_max)
        elif s.review and s.flow == "reduced":
            replay, retro, rinfo = review(scope, replay, retro,
                                          transcript=transcript,
                                          ledger_key=key, max_tokens=summary_max)
    release(scope)
    scope.cont = None
    replay = strip_half_marker(replay)
    if tail:
        replay, _ = trim_fences(replay)
    if replay and ledger:
        # The floor under the writer's re-ask: a half-critic or reviewer can
        # bring invented steps back, and the splice alone would drop their
        # numbers and keep the sentences (fork tool-ledger.ts "invalid").
        replay, cut = tool_ledger.drop_past_key(replay, len(ledger))
        if cut:
            log.warning("compaction replay narrated %d step(s) past the "
                        "%d-call record; cut", cut, len(ledger))
    # Lines about files no call opened (unread_files): the writer and the
    # half-critics completed the task's module table for files not yet
    # read, every field wrong (compaction-v7-g2 / -cite). Names kept.
    seen = unread_files.seen_in(dropped)
    if previous is not None:
        if previous.seen_files is not None:
            seen |= set(previous.seen_files)
        else:      # a marker from before the set was carried: lenient
            seen |= set(unread_files.files_in(previous.summary))
            for d in previous.ledger:
                seen.update(str(f) for f in (d.get("files") or []))
    unread_names: list[str] = []
    unread_cut = 0
    if replay:
        replay, unread_names, unread_cut = unread_files.drop_unread(
            replay, seen, exempt="\n".join(shown))
        if unread_cut:
            log.warning("compaction replay stated details of %d file(s) no "
                        "call opened (%d line(s)); cut",
                        len(unread_names), unread_cut)

    spliced = tool_ledger.splice(replay, ledger) if (replay and ledger) else None
    final = spliced.text if spliced else replay
    # What is carried: the key's calls evicted to budget and numbered afresh
    # for the next generation. The uncited calls appended after the text are
    # those that survive it, keeping the numbers the writer saw (fork
    # 46bafaeb71): the rest of the fold is in no ledger after this one.
    gone = tool_ledger.evicted(ledger, ledger_budget)
    uncited_at = set(spliced.uncited) if spliced else {e.index for e in ledger}
    kept = tool_ledger._renumber([e for i, e in enumerate(ledger) if i not in gone])
    uncited = [e for i, e in enumerate(ledger)
               if i not in gone and e.index in uncited_at]
    if spliced and spliced.invalid:
        log.warning("compaction replay cited %d call(s) not in the record: %s",
                    len(spliced.invalid), spliced.invalid[:10])
    if spliced and spliced.repeated:
        log.info("compaction replay cited %d call(s) again; kept at their "
                 "first place", len(spliced.repeated))
    if final:
        final = _ensure_files(final, dropped)
        if unread_cut:
            final = (final.rstrip() + "\n\n"
                     + unread_files.note(unread_names, unread_cut))

    stored = render_requests(stored_requests,
                             int(target_tokens * 3 * USER_REQUEST_SHARE))
    return retrospective.Digest(
        summary=final, retrospective=retro, generation=generation,
        dropped=len(dropped),
        stand_down="" if (final or retro) else "both-phases-empty",
        user_requests=stored_requests, shown_requests=stored,
        ledger=[e.as_dict() for e in kept],
        seen_files=unread_files.carried(
            (previous.seen_files or []) if previous else [], seen),
        uncited=tool_ledger.render(uncited) if final else "",
        invalid_citations=list(spliced.invalid) if spliced else [],
        flow=s.flow if tail else "full-state",
        info={**winfo, **rinfo, **cinfo, "unread_lines_cut": unread_cut})


# --------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------- #

_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()
_locks_guard = threading.Lock()
_MAX_LOCKS = 512


def _lock_for(session: Optional[str]) -> threading.Lock:
    """One fold at a time per lane (xOllama's singleflight)."""
    key = session or "-"
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
            while len(_locks) > _MAX_LOCKS:
                _locks.popitem(last=False)
        else:
            _locks.move_to_end(key)
        return lock


def continues(scope: Scope, reason: str) -> tuple[bool, str]:
    """Whether this compaction's writer may continue the lane, and why
    (``Settings.continuation``). Never in overflow recovery: the lane's
    own prompt is what the provider just refused."""
    mode = scope.settings.continuation
    if mode == "off":
        return False, "off"
    if reason == "overflow":
        return False, "overflow recovery: the lane's prompt no longer fits"
    if mode == "on":
        return True, "on"
    mgr = getattr(scope.chat_client, "_placed_manager", None)
    if mgr is None:
        return False, "not under PolyKV"
    try:
        ok = mgr.continues(getattr(scope.chat_client, "_placed_inner",
                                   scope.chat_client), scope.model)
    except Exception:  # noqa: BLE001 — a probe never blocks a fold
        log.exception("compaction: PolyKV continuation probe failed")
        return False, "PolyKV probe failed"
    return ok, "PolyKV pooled" if ok else "PolyKV root not pooled"


@dataclass
class Outcome:
    messages: list[dict]
    changed: bool
    report: dict = field(default_factory=dict)


def overhead_tokens(messages: list[dict], tools: Optional[list],
                    model: str) -> int:
    """What compaction cannot shrink: the leading system turns and the
    tool schemas."""
    lead = []
    for m in messages:
        if m.get("role") != "system":
            break
        lead.append(m)
    return (budget.estimate_messages_tokens(lead, model)
            + budget.estimate_tools_tokens(tools, model))


def compact(scope: Scope, messages: list[dict], *, plan: "budget.Budget",
            tools: Optional[list] = None, session: Optional[str] = None,
            reason: str = "trigger", require_smaller: bool = False,
            ) -> Outcome:
    """Fold ``messages`` for the next call of ``scope``'s lane.

    ``reason`` names why (``trigger``, ``starved-cap``, ``overflow``,
    ``silent-truncation``). ``require_smaller`` is the provider-refused
    overflow path (plan C5, fork ``compaction.ts:1633-1767``): the
    deterministic basic fold is the floor, then one agentic attempt, kept
    only if it is smaller than the refused request and under the trigger
    — the model may not survive a digest the basic fold already beats."""
    model = scope.model
    s = scope.settings
    with _lock_for(session):
        before = budget.estimate_messages_tokens(messages, model)
        ctx = plan.context_length
        overhead = overhead_tokens(messages, tools, model)
        trigger = plan.trigger_tokens or max(
            1, (ctx or 0) - budget.MAX_OUTPUT_TOKENS - budget.WINDOW_MARGIN_TOKENS)
        target = budget.compaction_target_tokens(
            ctx, overhead_tokens=overhead, trigger_tokens=trigger,
            share=float(s.target_share))
        previous = retrospective.previous_digest(messages)
        # The question the head keeps verbatim: never folded, so the writer
        # is shown it explicitly.
        head = budget._head_end(messages)
        task = messages[head - 1] if 0 < head <= len(messages) else None
        report: dict = {"flow": s.flow, "placement": s.placement,
                        "reason": reason, "before": before, "target": target,
                        "trigger": trigger, "context_length": ctx}
        digests: list = []
        cont_ok, report["continuation"] = continues(scope, reason)
        if cont_ok and scope.tools is None:
            scope.tools = tools

        def marker_for(tail: bool):
            def make(dropped, generation, pinned=None):
                if s.flow == "basic":
                    return None
                try:
                    digest = write_digest(
                        scope, dropped, target_tokens=target,
                        context_tokens=ctx, task=task, generation=generation,
                        previous=previous, pinned=pinned, tail=tail,
                        conversation=messages if cont_ok else None)
                finally:
                    # Every path out gives back what a continuation held.
                    release(scope)
                    scope.cont = None
                digests.append(digest)
                if digest.empty:
                    log.warning("role=%s: compaction digest came back empty "
                                "(%s); the span leaves only the marker",
                                scope.role, digest.stand_down)
                    return None
                return retrospective.render_marker(digest, len(dropped),
                                                   placement=s.placement)
            return make

        result = budget.compact_messages(
            messages, keep_tokens=target, context_length=ctx, model=model,
            make_marker=marker_for(True), overhead_tokens=overhead)
        report["cut"] = result.cut_reason
        out = result.messages if result.changed else messages
        after = budget.estimate_messages_tokens(out, model)

        if result.changed and after > trigger:
            # Still over the trigger with a tail: fold everything, keep the
            # latest typed prompt, and take the smaller (fork 0fc7744c3).
            rescue = budget.compact_messages(
                messages, keep_tokens=target, context_length=ctx, model=model,
                make_marker=marker_for(False), tail=False,
                overhead_tokens=overhead)
            if rescue.changed:
                r_after = budget.estimate_messages_tokens(rescue.messages, model)
                report["rescue"] = {"after": r_after, "taken": r_after < after}
                if r_after < after:
                    out, after = rescue.messages, r_after

        changed = result.changed
        if require_smaller:
            basic = budget.compact_messages(
                messages, keep_tokens=target, context_length=ctx, model=model,
                overhead_tokens=overhead)
            b_after = (budget.estimate_messages_tokens(basic.messages, model)
                       if basic.changed else before)
            agentic_ok = changed and after < before and after <= trigger
            report["overflow"] = {"basic_after": b_after,
                                  "agentic_after": after if changed else None,
                                  "taken": "agentic" if agentic_ok else "basic"}
            if not agentic_ok and basic.changed and b_after < before:
                out, after, changed = basic.messages, b_after, True
        if changed and after >= before:
            log.warning("role=%s: compaction did not shrink the history "
                        "(~%d -> ~%d tok); discarding the fold",
                        scope.role, before, after)
            report["discarded"] = "no-shrink"
            out, after, changed = messages, before, False
        if digests:
            d = digests[-1]
            report["generation"] = d.generation
            report["invalid_citations"] = len(d.invalid_citations)
            report["digest"] = d.public_dict()
        report.update(after=after, calls=[step for step, _ in scope.calls])
        _emit(scope, report)
        return Outcome(out, changed, report)


def _emit(scope: Scope, report: dict) -> None:
    rec = scope.recorder
    if rec is None or not hasattr(rec, "record_event"):
        return
    try:
        rec.record_event(kind="compaction", role=scope.role, round=scope.round,
                         lane_idx=scope.lane_idx,
                         payload={**report, "phase": "compaction"})
    except Exception:  # pragma: no cover
        log.exception("recorder.record_event raised; ignored")


__all__ = ["Settings", "DEFAULT_SETTINGS", "Scope", "Outcome", "CallResult",
           "FLOWS", "PLACEMENTS", "REASONING_REPLAY", "CONFIG_KEYS",
           "settings_from", "call", "compact", "write_digest", "write_replay",
           "review", "council", "serialize", "cut_echo", "trim_fences",
           "split_at_marker", "parse_sections", "collect_user_requests",
           "merge_requests", "render_requests", "build_writer_request",
           "overhead_tokens"]
