"""Detect a cut-off completion, so it stops looking like a finished one.

**The incident.** ``csl-2026-08-12-0831-905a`` was an ``xhigh``
adversarial audit: 43 minutes, 113 LLM calls, 2.86M prompt tokens. Its
final answer was **655 characters** and ended mid-token —
``...count the **presence** of $\\text{``. It was written to
``summary.md``, recorded with ``status: completed`` and ``error: null``,
and handed over as the finished product. The critic in the same run was
cut the same way, mid-backtick.

**Why nothing noticed.** Ollama reports the terminal condition in
``done_reason``: ``"stop"`` for a natural end, ``"length"`` when
generation was cut. ``ChatClient._from_ollama`` used to compute::

    finish_reason = "tool_calls" if tool_calls else "stop"

— discarding ``done_reason`` and hardcoding a clean stop. The evidence
of truncation was destroyed in translation, one layer below anything
that could have acted on it. (``caliber_proxy/ollama.py`` had always
mapped it correctly; the translator every consultants role actually uses
did not.)

That is fixed at the source, which makes ``done_reason`` **the**
authoritative signal. This module adds the second line of defence,
because an authoritative signal you cannot audit is one you are trusting
rather than checking:

``reported``
    ``done_reason``/``finish_reason`` is ``length``. Definitive.

``structural``
    The text simply stops inside a construct it opened — an unclosed
    backtick, ``$…$``, ``**…**``, or a brace-taking LaTeX macro. Prose
    that ends naturally does not end halfway through its own markup.
    This is what catches a backend that reports ``stop`` on a cut
    response, and it is the signal that would have caught the incident
    even with the translator bug in place: both the synthesizer and the
    critic ended inside markup they had opened.

Structural detection is deliberately narrow. It looks only at whether a
delimiter opened in the final line is still open at the very end —
never at "does this read as finished", which is a judgement call this
layer has no business making. A false positive costs one retry; a false
negative is what shipped a 655-character audit.

Lives in ``claude_hooks`` rather than ``consultants`` because both the
consultants council and the shared ``agent_loop.runner`` need it, and
``claude_hooks`` may not import ``consultants``. Pure and stdlib-only:
no LangGraph, no HTTP, so the whole thing is testable in the main suite.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("claude_hooks.truncation")

#: Below this, a structural check on the tail is meaningless — a short
#: intentional reply ("Yes.", a bare tool rationale) trips delimiter
#: heuristics constantly. ``reported`` still applies at any length.
_MIN_CHARS_FOR_STRUCTURAL = 40

#: LaTeX-ish macros that must be followed by a closed brace group.
_MACRO_RE = re.compile(r"\\[A-Za-z]+\{")


@dataclass(frozen=True)
class Truncation:
    """One completion judged incomplete."""
    kind: str            # "reported" | "structural"
    detail: str          # human-readable why
    role: str = ""
    model: str = ""
    chars: int = 0
    completion_tokens: int = 0

    def public_dict(self) -> dict:
        out = {"kind": self.kind, "detail": self.detail,
               "chars": self.chars}
        if self.role:
            out["role"] = self.role
        if self.model:
            out["model"] = self.model
        if self.completion_tokens:
            out["completion_tokens"] = self.completion_tokens
        return out


def _count_unescaped(text: str, ch: str) -> int:
    n = 0
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == ch:
            n += 1
        i += 1
    return n


def unterminated_construct(text: str) -> Optional[str]:
    """Name the construct the text stops inside, or ``None``.

    Only the **final line** is examined. Markdown delimiters do not span
    blank lines, so scanning the whole document would report every
    ordinary use of a dollar sign in a table as an open construct.
    Fenced code blocks are the exception and are handled separately.
    """
    if not text:
        return None

    # An unclosed ``` fence is a whole-document property, not a
    # last-line one.
    if text.count("```") % 2 == 1:
        return "unclosed ``` code fence"

    tail = text.rstrip()
    if not tail:
        return None
    last_line = tail.rsplit("\n", 1)[-1]

    # Inline code: an odd number of backticks on the closing line means
    # one was opened and never closed. This is exactly how the critic
    # was cut ("...count row presence of `").
    #
    # Fence markers are removed first: a correctly closed code block
    # ends on a line of exactly ``` — three backticks, an odd count,
    # which the naive check reads as an open inline span. Fence pairing
    # was already settled above.
    if _count_unescaped(last_line.replace("```", ""), "`") % 2 == 1:
        return "unclosed ` inline code"

    # Inline math. The synthesizer died here: "...of $\\text{".
    if _count_unescaped(last_line, "$") % 2 == 1:
        return "unclosed $ math span"

    # A brace-taking macro with no closing brace after it.
    macros = list(_MACRO_RE.finditer(last_line))
    if macros:
        after = last_line[macros[-1].end():]
        if after.count("}") <= after.count("{"):
            return f"unclosed {macros[-1].group(0)!r} group"

    # Bold/italic emphasis left open on the final line.
    if last_line.count("**") % 2 == 1:
        return "unclosed ** emphasis"

    return None


def classify(response: Any, *, text: Optional[str] = None,
             role: str = "", model: str = "") -> Optional[Truncation]:
    """Judge one chat-completion response. ``None`` means it looks whole.

    ``response`` is the OpenAI-shape dict; ``text`` overrides the content
    when the caller has already extracted it (the agent loop concatenates
    across iterations, and only the caller knows the final string).
    """
    if not isinstance(response, dict):
        return None

    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    msg = choice.get("message") or response.get("message") or {}
    content = text if text is not None else (msg.get("content") or "")
    content = content or ""

    usage = response.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens")
                            or response.get("eval_count") or 0)

    # 1. Reported. Checked first and at any length: authoritative.
    #    ``done_reason`` is read before ``finish_reason`` because a
    #    tool-call turn legitimately reports finish_reason="tool_calls"
    #    while done_reason still says the generation was cut — and a
    #    truncated tool call is *more* dangerous than truncated prose,
    #    since its arguments may be half-written.
    reported = (choice.get("done_reason")
                or response.get("done_reason")
                or choice.get("finish_reason"))
    if reported == "length":
        return Truncation(
            kind="reported",
            detail="the backend reported done_reason='length' — "
                   "generation hit the output limit and was cut",
            role=role, model=model, chars=len(content),
            completion_tokens=completion_tokens,
        )

    # 2. Structural. Catches a backend that reports a clean stop on a
    #    response that plainly is not one.
    if len(content) >= _MIN_CHARS_FOR_STRUCTURAL:
        construct = unterminated_construct(content)
        if construct is not None:
            return Truncation(
                kind="structural",
                detail=f"content ends inside an {construct} it opened — "
                       f"tail: {content[-60:]!r}",
                role=role, model=model, chars=len(content),
                completion_tokens=completion_tokens,
            )
    return None


__all__ = ["Truncation", "classify", "unterminated_construct"]
