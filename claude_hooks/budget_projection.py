"""Fit a message list to a token target by degrading it in a fixed order.

Ported from the Cline fork's
``sdk/packages/core/src/extensions/context/budget-projection/project.ts``.

The problem it solves is narrower than compaction and easier to get
wrong. Compaction decides *which span of history to discard*. A
projection takes a span that is already going somewhere — a summariser,
a retrospective — and makes it fit a budget without producing a request
the backend will reject. The naive version ("cut it at N characters")
does produce such requests: it can sever an assistant's tool call from
the tool result that answers it, which is not a smaller conversation but
an invalid one.

**The order of degradation is the design.** Cheapest and least
informative first:

1. **Reasoning**, per the intent's policy. A summariser reading the
   transcript to describe it has no use for how the model talked itself
   into each call; a retrospective has nothing *but* that.
2. **Unsafe blocks** — anything that cannot be truncated without being
   corrupted (images, redacted reasoning) outside the protected tail.
3. **Text truncation**, newest-first, on messages that carry
   truncatable text.
4. **Whole messages**, oldest-first, and only in **tool-pair closures**:
   dropping an assistant turn takes its tool results with it and vice
   versa, because half a pair is a malformed request rather than a
   cheaper one.

**What is never dropped.** The first typed user message (the question
being answered), the latest typed user message, and the *live tail* —
the trailing run beginning at the most recent assistant message whose
tool calls have no results yet. That message and its pending results are
the turn in flight; removing either half breaks the exchange.

**Why it reports actions.** A projection that silently changed the
transcript would make a bad summary indistinguishable from a bad
summariser. Every mutation is recorded with its path and reason, and a
projection that could not reach the target says so (``status:
"failed"``) rather than returning something over budget as if it were
fine.

Shape note: the fork works on Anthropic-style content-block arrays; this
works on the OpenAI/Ollama shape the council actually sends — string
``content``, ``tool_calls`` on the assistant message, and separate
``role: "tool"`` messages carrying ``tool_call_id``. The pairing logic
is the same relation under a different spelling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from claude_hooks import token_calib

log = logging.getLogger("claude_hooks.budget_projection")

#: What becomes of the transcript's reasoning.
#:
#: ``keep`` — every request that is not a summarisation: reasoning is
#: the model's own working memory and nothing here is entitled to spend
#: it. Also the retrospective's input, where reasoning *is* the subject.
#:
#: ``drop_all`` — the summariser's input. It is reading the transcript
#: to describe it, and how the model talked itself into each tool call
#: is not part of that description.
#:
#: ``keep_latest_if_it_fits`` — what compaction leaves behind. The turn
#: compaction keeps is the one the model is still working on, and
#: handing it back its own last turn with the thinking cut out is
#: handing back a conclusion with no reasoning attached. So the most
#: recent block survives, and only while the result still lands inside
#: the target.
THINKING_POLICIES = ("keep", "drop_all", "keep_latest_if_it_fits")


@dataclass(frozen=True)
class ProjectionPolicy:
    thinking: str = "keep"
    protect_first_typed_user: bool = True
    protect_latest_typed_user: bool = True
    protect_live_tail: bool = True
    #: Whether tool results may be truncated harder than ordinary text.
    #: True for the retrospective, whose serializer reduces every result
    #: to a one-line verdict anyway — spending budget carrying their
    #: full text into the projection only to discard it is waste.
    shrink_tool_results_first: bool = False


#: The intents the council actually projects for.
POLICIES: dict[str, ProjectionPolicy] = {
    # The summariser describes what happened. Reasoning is the other
    # phase's input and would crowd out the transcript here.
    "summary_input": ProjectionPolicy(thinking="drop_all"),
    # The retrospective assesses *how the work went*, which exists only
    # in the reasoning. Dropping it would leave the phase with nothing
    # to assess; the tool results go instead.
    "retrospective_input": ProjectionPolicy(
        thinking="keep", shrink_tool_results_first=True),
    # A normal request: reasoning is the model's working memory.
    "provider_request": ProjectionPolicy(thinking="keep"),
    # Compaction's own output.
    "compaction_projection": ProjectionPolicy(
        thinking="keep_latest_if_it_fits"),
}

#: Never truncate a message below this many characters — below it the
#: remnant says nothing and the message would be better dropped whole.
MIN_TRUNCATED_CHARS = 16


@dataclass
class ProjectionAction:
    kind: str        # truncated_text | dropped_message | dropped_field | preserved
    reason: str      # over_budget | unsafe_to_truncate | tool_pair_boundary | protected
    index: int
    original_size: int = 0
    final_size: int = 0

    def public_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason, "index": self.index,
                "original_size": self.original_size,
                "final_size": self.final_size}


@dataclass
class ProjectionResult:
    status: str                       # "ok" | "failed"
    messages: list[dict]
    estimated_tokens: int
    actions: list[ProjectionAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return any(a.kind != "preserved" for a in self.actions)

    def summary_line(self) -> str:
        kinds: dict[str, int] = {}
        for a in self.actions:
            if a.kind != "preserved":
                kinds[a.kind] = kinds.get(a.kind, 0) + 1
        detail = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        return (f"{self.status}: ~{self.estimated_tokens} tok"
                + (f" ({detail})" if detail else ""))


Estimator = Callable[[dict], int]


def _default_estimate(message: dict, model: str = "") -> int:
    return token_calib.estimate_request_tokens([message], model=model)


def _size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return len(str(value))


def _total(messages: list[dict], estimate: Estimator) -> int:
    return sum(estimate(m) for m in messages)


# --------------------------------------------------------------------- #
# Structure: what pairs with what, and what must not move
# --------------------------------------------------------------------- #

def _tool_ids(message: dict) -> set[str]:
    """Every tool-call id this message participates in — as the caller
    or as the answer."""
    ids: set[str] = set()
    for tc in message.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("id"):
            ids.add(str(tc["id"]))
    if message.get("tool_call_id"):
        ids.add(str(message["tool_call_id"]))
    return ids


def _is_tool_result(message: dict) -> bool:
    return message.get("role") == "tool"


def _is_typed_user(message: dict) -> bool:
    """A user message a person (or the caller) actually wrote, as
    opposed to a tool result wearing the user role."""
    return message.get("role") == "user" and not _is_tool_result(message)


def first_typed_user_index(messages: list[dict]) -> int:
    for i, m in enumerate(messages):
        if _is_typed_user(m):
            return i
    return -1


def latest_typed_user_index(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if _is_typed_user(messages[i]):
            return i
    return -1


def live_tail_start_index(messages: list[dict]) -> int:
    """Where the turn in flight begins.

    The most recent assistant message with a tool call that nothing has
    answered yet. That message and everything after it is the exchange
    currently open; removing either half of it produces a malformed
    request rather than a cheaper one.

    ``len(messages)`` when every call has been answered — nothing is in
    flight, so nothing is protected on these grounds.

    Uses the positional pair index rather than a set of answered ids,
    for the same reason :func:`build_pair_index` does: with synthesized
    ids repeating across turns, one answered ``tc_0`` would make every
    turn's ``tc_0`` look answered and hide a genuinely open exchange.
    """
    pairs = build_pair_index(messages)
    for i in range(len(messages) - 1, -1, -1):
        calls = [tc for tc in messages[i].get("tool_calls") or []
                 if isinstance(tc, dict)]
        if calls and len(pairs.get(i) or ()) < len(calls):
            return i
    return len(messages)


def build_pair_index(messages: list[dict]) -> dict[int, set[int]]:
    """Map each message to the messages it is paired with.

    Pairing is **positional**, not by id alone: each tool result binds to
    the nearest *preceding* unbound assistant call carrying that id.

    That distinction is not academic here. When a model returns a tool
    call without an id, ``chat_client._from_ollama`` synthesizes one from
    the call's index within its own message — so the first call of every
    turn is ``tc_0``. Matching on the id alone therefore links every turn
    in the conversation into a single closure, and dropping one message
    takes the entire history with it. Found by a test whose fixture
    happened to reproduce the same collision.
    """
    pairs: dict[int, set[int]] = {i: set() for i in range(len(messages))}
    # id -> indexes of assistant calls still waiting for a result, oldest
    # first, so a result claims the closest one before it.
    pending: dict[str, list[int]] = {}
    for i, m in enumerate(messages):
        result_id = m.get("tool_call_id")
        if result_id is not None:
            waiting = pending.get(str(result_id))
            if waiting:
                caller = waiting.pop()
                pairs[i].add(caller)
                pairs[caller].add(i)
            continue
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id") is not None:
                pending.setdefault(str(tc["id"]), []).append(i)
    return pairs


def message_closure(messages: list[dict], start: int) -> set[int]:
    """Every message that must leave with ``messages[start]``.

    Transitive over the pair index: an assistant turn takes its results,
    and a result takes the turn that called it. Half a pair is not a
    smaller conversation, it is an invalid one.
    """
    pairs = build_pair_index(messages)
    closure: set[int] = set()
    queue = [start]
    while queue:
        i = queue.pop(0)
        if i in closure or not (0 <= i < len(messages)):
            continue
        closure.add(i)
        queue.extend(j for j in pairs.get(i, ()) if j not in closure)
    return closure


# --------------------------------------------------------------------- #
# Degradation steps
# --------------------------------------------------------------------- #

def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= MIN_TRUNCATED_CHARS:
        return text[:max(1, max_chars)]
    marker_len = len(f"\n…[truncated {len(text)} chars]")
    keep = max(1, max_chars - marker_len)
    return f"{text[:keep]}\n…[truncated {len(text) - keep} chars]"


def _last_thinking_index(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        for key in token_calib.REASONING_KEYS:
            if (messages[i].get(key) or "").strip():
                return i
    return -1


def _drop_thinking(messages: list[dict], actions: list[ProjectionAction],
                   exempt: int = -1) -> list[dict]:
    out = []
    for i, m in enumerate(messages):
        if i == exempt:
            out.append(m)
            continue
        present = [k for k in token_calib.REASONING_KEYS if m.get(k)]
        if not present:
            out.append(m)
            continue
        stripped = {k: v for k, v in m.items() if k not in present}
        actions.append(ProjectionAction(
            kind="dropped_field", reason="unsafe_to_truncate", index=i,
            original_size=_size(m), final_size=_size(stripped)))
        out.append(stripped)
    return out


def _thinking_exemption_fits(messages: list[dict], target: int,
                             estimate: Estimator) -> bool:
    """Whether the transcript still lands inside its target with the
    latest reasoning left in.

    Priced against the target rather than assumed: a single block can be
    fourteen thousand tokens at high effort, and an exemption that puts
    the result out of reach is not an exemption — it is the next
    compaction.
    """
    if _last_thinking_index(messages) < 0:
        return False
    priced = _drop_thinking(messages, [], _last_thinking_index(messages))
    return _total(priced, estimate) <= target


def _truncatable_chars(message: dict) -> int:
    content = message.get("content")
    return len(content) if isinstance(content, str) else 0


def project(messages: list[dict], *, target_tokens: int,
            policy_intent: str = "provider_request",
            model: str = "",
            estimate: Optional[Estimator] = None) -> ProjectionResult:
    """Degrade ``messages`` until they fit ``target_tokens``.

    Returns the projected list plus a record of every change. ``status``
    is ``"failed"`` when the target could not be reached without
    violating a protection — the messages come back as close as the
    projection could get, but the caller is told rather than left to
    discover it from a rejected request.
    """
    policy = POLICIES.get(policy_intent) or ProjectionPolicy()
    est: Estimator = estimate or (lambda m: _default_estimate(m, model))
    actions: list[ProjectionAction] = []
    warnings: list[str] = []

    if target_tokens <= 0:
        return ProjectionResult(
            status="failed", messages=list(messages),
            estimated_tokens=_total(messages, est), actions=actions,
            warnings=["target budget must be greater than zero"])

    work = [dict(m) for m in messages if isinstance(m, dict)]

    # 1. Reasoning, per intent.
    if policy.thinking != "keep":
        exempt = -1
        if (policy.thinking == "keep_latest_if_it_fits"
                and _thinking_exemption_fits(work, target_tokens, est)):
            exempt = _last_thinking_index(work)
        work = _drop_thinking(work, actions, exempt)

    total = _total(work, est)
    if total <= target_tokens:
        return ProjectionResult(status="ok", messages=work,
                                estimated_tokens=total, actions=actions)

    # 2. Tool results first when the caller has no use for their text.
    if policy.shrink_tool_results_first:
        for i, m in enumerate(work):
            if total <= target_tokens:
                break
            if not _is_tool_result(m) or not _truncatable_chars(m):
                continue
            before = _size(m)
            m["content"] = _truncate_text(str(m.get("content") or ""),
                                          MIN_TRUNCATED_CHARS * 8)
            actions.append(ProjectionAction(
                kind="truncated_text", reason="over_budget", index=i,
                original_size=before, final_size=_size(m)))
            total = _total(work, est)

    # 3. Text truncation, newest-first. Newest because the oldest
    #    messages are the ones step 4 will drop whole, and truncating
    #    something you are about to delete spends work for nothing.
    for i in range(len(work) - 1, -1, -1):
        if total <= target_tokens:
            break
        if _protected(work, i, policy):
            continue
        if not _truncatable_chars(work[i]):
            continue
        before = _size(work[i])
        per_message_chars = max(
            MIN_TRUNCATED_CHARS,
            int(target_tokens * token_calib.chars_per_token(model)
                / max(1, len(work))))
        work[i] = dict(work[i])
        work[i]["content"] = _truncate_text(
            str(work[i].get("content") or ""), per_message_chars)
        actions.append(ProjectionAction(
            kind="truncated_text", reason="over_budget", index=i,
            original_size=before, final_size=_size(work[i])))
        total = _total(work, est)

    # 4. Whole messages, oldest-first, in tool-pair closures.
    original_indexes = list(range(len(work)))
    i = 0
    while i < len(work) and total > target_tokens:
        if _protected(work, i, policy):
            actions.append(ProjectionAction(
                kind="preserved", reason="protected", index=original_indexes[i],
                original_size=_size(work[i]), final_size=_size(work[i])))
            i += 1
            continue
        closure = message_closure(work, i)
        if _closure_touches_protected(work, closure, policy):
            i += 1
            continue
        for j in sorted(closure):
            actions.append(ProjectionAction(
                kind="dropped_message",
                reason=("tool_pair_boundary" if len(closure) > 1
                        else "over_budget"),
                index=original_indexes[j],
                original_size=_size(work[j]), final_size=0))
        work = [m for k, m in enumerate(work) if k not in closure]
        original_indexes = [x for k, x in enumerate(original_indexes)
                            if k not in closure]
        total = _total(work, est)

    if total > target_tokens:
        warnings.append(
            "could not reach the budget without dropping protected "
            "content; returning the closest projection")
        return ProjectionResult(status="failed", messages=work,
                                estimated_tokens=total, actions=actions,
                                warnings=warnings)
    return ProjectionResult(status="ok", messages=work,
                            estimated_tokens=total, actions=actions,
                            warnings=warnings)


def _protected(messages: list[dict], index: int,
               policy: ProjectionPolicy) -> bool:
    if policy.protect_first_typed_user and index == first_typed_user_index(
            messages):
        return True
    if policy.protect_latest_typed_user and index == latest_typed_user_index(
            messages):
        return True
    if policy.protect_live_tail and index >= live_tail_start_index(messages):
        return True
    return False


def _closure_touches_protected(messages: list[dict], closure: set[int],
                               policy: ProjectionPolicy) -> bool:
    return any(_protected(messages, j, policy) for j in closure)


__all__ = ["ProjectionPolicy", "ProjectionAction", "ProjectionResult",
           "POLICIES", "THINKING_POLICIES", "project",
           "first_typed_user_index", "latest_typed_user_index",
           "live_tail_start_index", "message_closure"]
