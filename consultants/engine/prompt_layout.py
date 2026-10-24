"""The shared prompt layout on PolyKV backends (plan F4).

Every council role opens with its own system prompt, so on an engine
that shares prefixes the roles shared nothing: the critic, meta-critic,
synthesizer and adversary each prefilled the same question, plan and
reports behind a different first turn. On a PolyKV root (``pooled`` /
``assured``, ``[polykv] layout = "shared"``) :func:`role_last` puts what
the roles have in common first and each role's own instructions last:

- the system turn is :data:`council.COUNCIL_PREAMBLE`, the same for every
  role, followed by the grounding turns as they were;
- the role's own text (its system prompt past the preamble, and the
  ``# Output Budget`` section wherever it was appended) goes to the end of
  the first user turn, under :data:`ROLE_HEADING`.

Nothing else moves, so a lane's tool loop keeps one stable prefix from
turn to turn, and the transform is applied at the wire only (the
recorder, compaction and every reader keep the messages as built).

:func:`block_cuts` names where the shared blocks of that first user turn
end — after the question, after the plan, before the last report — from
the exact headers the builders write, so the run manager can stack a
layer per block. The last report's end is never guessed from the text
(reports hold blank lines of their own); the manager finds it as the
longest prefix two calls share (:func:`snap`).

A role whose system turn does not open with the preamble (compaction
steps, audits, tool_executor, coder) is left as it is.
"""

from __future__ import annotations

import re
from typing import Optional

#: Where the role's own instructions begin in the reordered user turn.
ROLE_HEADING = "# Your role in the council"
#: Said after the preamble, so the model knows where its job is written.
ROLE_POINTER = ("Your own role and instructions come last, at the end of "
                "the user's message under \"" + ROLE_HEADING + "\".")

_BUDGET_HEADING = "\n\n# Output Budget"

#: Headers the builders open each shared block with (``build_*_messages``
#: in ``consultants/engine/council.py``), as they appear after the
#: ``"\n".join`` of parts that start with ``"\n"``.
QUESTION_HEADER = "USER QUESTION:\n"
PLAN_HEADERS = ("\n\nPLANNER'S PLAN:\n", "\n\nPLAN FROM PLANNER:\n")
REPORT_HEADER = "\n\nRESEARCHER REPORT "


def _preamble() -> str:
    from consultants.engine.council import COUNCIL_PREAMBLE
    return COUNCIL_PREAMBLE


def shared_system() -> str:
    return _preamble() + "\n\n" + ROLE_POINTER


def _split_budget(text: str) -> tuple[str, str]:
    at = text.find(_BUDGET_HEADING)
    if at < 0:
        return text, ""
    return text[:at], text[at:]


def role_last(messages: list) -> list:
    """``messages`` in the shared layout, or ``messages`` itself when it
    is not a council role's conversation (or is laid out already)."""
    if not messages:
        return messages
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    if head == 0 or head >= len(messages) \
            or messages[head].get("role") != "user":
        return messages
    preamble = _preamble()
    role_at = next((i for i in range(head - 1, -1, -1)
                    if str(messages[i].get("content") or "")
                    .startswith(preamble)), None)
    if role_at is None:
        return messages
    role_content = str(messages[role_at].get("content") or "")
    if role_content.startswith(shared_system()):
        return messages                      # already laid out
    role_text, budget = _split_budget(role_content[len(preamble):].lstrip("\n"))
    grounding = []
    for i in range(head):
        if i == role_at:
            continue
        m = dict(messages[i])
        content = m.get("content")
        if isinstance(content, str) and _BUDGET_HEADING in content:
            m["content"], extra = _split_budget(content)
            budget = budget or extra
        grounding.append(m)
    user = dict(messages[head])
    own = role_text.strip() + budget
    user["content"] = (str(user.get("content") or "").rstrip()
                       + "\n\n" + ROLE_HEADING + "\n\n" + own)
    return ([{"role": "system", "content": shared_system()}] + grounding
            + [user] + list(messages[head + 1:]))


def block_cuts(text: str) -> list[int]:
    """Offsets in a first user turn where a shared block ends: after the
    question, after the plan, and before the last report. Empty when the
    turn does not open with the question header."""
    if not text.startswith(QUESTION_HEADER):
        return []
    cuts = []
    plan_at = min((i for i in (text.find(h) for h in PLAN_HEADERS) if i > 0),
                  default=-1)
    if plan_at < 0:
        return []
    cuts.append(plan_at)
    first_report = text.find(REPORT_HEADER, plan_at + 1)
    if first_report < 0:
        return cuts
    cuts.append(first_report)
    last_report = text.rfind(REPORT_HEADER)
    if last_report > first_report:
        cuts.append(last_report)
    return cuts


_PARAGRAPH = re.compile(r"\S\n\n")


def snap(text: str, n: int) -> int:
    """The largest offset ``<= n`` that ends a paragraph (non-space text
    followed by a blank line), so a layer never ends inside a word or a
    run of whitespace the tokenizer may merge differently. 0 when none."""
    n = min(n, len(text))
    best = 0
    # A match inside text[:n + 2] starts by n - 1, so it ends by n.
    for m in _PARAGRAPH.finditer(text, 0, n + 2):
        best = m.start() + 1
    return best


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    # Chunked, then by character: reports run to tens of thousands.
    step = 4096
    while i + step <= n and a[i:i + step] == b[i:i + step]:
        i += step
    while i < n and a[i] == b[i]:
        i += 1
    return i


def first_user(messages: list) -> Optional[tuple[int, str]]:
    """``(index, content)`` of the user turn right after the leading
    system run, or None."""
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    if head >= len(messages) or messages[head].get("role") != "user":
        return None
    content = messages[head].get("content")
    return (head, content) if isinstance(content, str) else None


__all__ = ["ROLE_HEADING", "block_cuts", "common_prefix", "first_user",
           "role_last", "shared_system", "snap"]
