"""Ask the endpoint how big its context is, and spend it deliberately.

Before this module, every consultants role sent ``{"model", "messages",
"stream", "think"}`` and nothing else. No ``num_predict``, no
``num_ctx``, no awareness of the window. Two distinct failure modes
hide behind that:

**The output cap.** With ``num_predict`` unset, the ceiling on
generation is whatever the far side happens to default to. For a local
Ollama that is unlimited; for a ``:cloud`` model it is the provider's
default, which we neither control nor observe. Incident
``csl-2026-08-12-0831-905a`` ended a 43-minute audit after roughly 650
completion tokens with 94k tokens of window still free — the prompt was
nowhere near the limit, so the only thing that could have stopped it was
an output cap nobody had set or seen. Sending an explicit, generous
``num_predict`` can only *raise* such a default, never lower one below
what we would otherwise have received, which is why doing so is safe
even where it is unnecessary.

**The window.** Nothing in the pipeline compacts. A council that grows
its history until the prompt exceeds the model's context does not
degrade gracefully; the backend truncates from one end and answers
confidently from what is left. :func:`plan` reports when the prompt no
longer leaves room to answer, and :func:`compact_messages` elides the
middle of the history to make room — keeping the system prompt and the
original question (which carry the task) and the most recent exchanges
(which carry the state), and dropping from the middle, which is where
redundancy lives.

Token counts here are **estimates**, deliberately. A real tokenizer per
backend is a dependency and a per-call cost, and the decision this
module makes only needs to be right to within a wide margin: the
estimate is biased conservative (assumes *more* tokens than likely) so
it errs toward leaving headroom rather than toward discovering the
limit by hitting it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("consultants.engine.budget")

#: Conservative chars-per-token. English prose runs ~4; code, base64,
#: and LaTeX run closer to 2.5-3. Dividing by a *small* number
#: over-estimates the token count, which is the safe direction.
CHARS_PER_TOKEN = 3.0

#: What we ask for when the window allows it. Large enough for a full
#: synthesizer answer with citations; the model stops when it is done,
#: so an unused budget costs nothing.
TARGET_OUTPUT_TOKENS = 32768

#: Never request less than this. A budget this small means the prompt
#: has crowded out the answer, and the right response is to compact,
#: not to ask for a two-paragraph audit.
MIN_OUTPUT_TOKENS = 4096

#: Left free for the backend's own framing (chat template, tool schema
#: re-serialisation, thinking blocks that don't appear in ``content``).
WINDOW_MARGIN_TOKENS = 2048


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate the prompt cost of a message list, including a small
    per-message allowance for role framing."""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):  # multimodal / block form
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(str(block.get("text") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(str(tc))
        total += 4  # role + delimiters
    return total


@dataclass(frozen=True)
class Budget:
    """What one call may spend, and whether it fits."""
    context_length: Optional[int]   # None when the endpoint didn't say
    prompt_tokens: int              # estimated
    output_tokens: int              # what to request as num_predict
    needs_compaction: bool
    detail: str = ""

    def public_dict(self) -> dict:
        return {"context_length": self.context_length,
                "prompt_tokens_est": self.prompt_tokens,
                "num_predict": self.output_tokens,
                "needs_compaction": self.needs_compaction}


def context_length_of(chat_client: Any, model: str) -> Optional[int]:
    """Best-effort probe of the model's real context window.

    Uses the client's ``context_length()`` when it has one — for the
    Ollama client that reads ``model_info["<arch>.context_length"]`` off
    ``/api/show`` and caches it. Clients without the method (llamafile,
    OpenAI-compatible passthrough) return ``None``, which callers must
    treat as "unknown", never as a default: guessing 4096 for a model
    with a 1M window would compact away most of a council's evidence.
    """
    probe = getattr(chat_client, "context_length", None)
    if not callable(probe):
        return None
    try:
        value = probe(model)
    except Exception:  # pragma: no cover — probing must never fail a call
        log.debug("context_length probe failed for %s", model, exc_info=True)
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def plan(chat_client: Any, model: str, messages: list[dict], *,
         target_output: int = TARGET_OUTPUT_TOKENS) -> Budget:
    """Decide the output budget for one call.

    With an unknown window we still request ``target_output``: the point
    of sending it is to override a provider default we cannot see, and
    that motive is unchanged by not knowing the window. Backends clamp a
    ``num_predict`` larger than they can serve; none of them fail on it.
    """
    prompt_tokens = estimate_messages_tokens(messages)
    ctx = context_length_of(chat_client, model)

    if ctx is None:
        return Budget(None, prompt_tokens, target_output, False,
                      "context length unknown; requesting target budget")

    available = ctx - prompt_tokens - WINDOW_MARGIN_TOKENS
    if available < MIN_OUTPUT_TOKENS:
        return Budget(
            ctx, prompt_tokens, MIN_OUTPUT_TOKENS, True,
            f"prompt ~{prompt_tokens} tok leaves {available} of {ctx} — "
            f"below the {MIN_OUTPUT_TOKENS} tok floor for an answer",
        )
    return Budget(ctx, prompt_tokens, min(target_output, available), False,
                  f"prompt ~{prompt_tokens} tok of {ctx}")


def compact_messages(messages: list[dict], *, keep_tokens: int,
                     keep_recent: int = 4) -> tuple[list[dict], bool]:
    """Drop from the middle until the history fits ``keep_tokens``.

    Returns ``(messages, changed)``. What survives:

    * every leading ``system`` message — the role's instructions;
    * the first non-system message — the question being answered;
    * the last ``keep_recent`` messages — the live state of the work.

    The elided span is replaced by a visible marker rather than removed
    silently, because a model that can see it was given an abridged
    history reasons differently about gaps in it than one that believes
    it has the whole record.

    Assistant/tool message *pairs* are not specially protected: callers
    whose backends reject an orphaned ``tool`` result should pass a
    ``keep_recent`` that covers their loop's turn width. The consultants
    agent loop caps at 4 calls per turn, which is why that is the
    default.
    """
    if not messages:
        return messages, False
    if estimate_messages_tokens(messages) <= keep_tokens:
        return messages, False

    head: list[dict] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        head.append(messages[i])
        i += 1
    if i < len(messages):
        head.append(messages[i])   # the question
        i += 1

    tail = messages[len(messages) - keep_recent:] if keep_recent else []
    middle_start, middle_end = i, len(messages) - len(tail)
    if middle_end <= middle_start:
        return messages, False     # nothing between head and tail to drop

    dropped = middle_end - middle_start
    marker = {
        "role": "system",
        "content": (
            f"[{dropped} earlier message(s) elided to fit the model's "
            f"context window. The instructions above and the most recent "
            f"exchanges below are intact; intermediate working notes are "
            f"not. Say so if the elided span is load-bearing for your "
            f"answer rather than inferring what it contained.]"
        ),
    }
    out = head + [marker] + tail
    log.warning(
        "compacted history: %d -> %d messages (~%d -> ~%d tok, budget %d)",
        len(messages), len(out), estimate_messages_tokens(messages),
        estimate_messages_tokens(out), keep_tokens,
    )
    return out, True


__all__ = ["Budget", "estimate_tokens", "estimate_messages_tokens",
           "context_length_of", "plan", "compact_messages",
           "TARGET_OUTPUT_TOKENS", "MIN_OUTPUT_TOKENS", "CHARS_PER_TOKEN"]
