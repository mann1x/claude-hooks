"""Verbatim self-repetition inside one completed reasoning block (plan D1).

The streaming guard (:mod:`claude_hooks.reasoning_loop_guard`) catches a
channel that has *collapsed*. This catches something milder and, on the
fork's data, far more common: a model that re-derives the same conclusion
in fresh-looking prose, paragraph after paragraph, and keeps calling tools
while it does — so every turn-level detector sees a productive turn.

Ported from Cerebriline (``sdk/packages/agents/src/reasoning-repetition.ts``)
with its thresholds and their measurements:

- A paragraph counts only when it carries a sentence (``min_stopwords``):
  a model quoting the same source block five times while comparing edits
  is working.
- **Both** signals are required (``max_repeat`` and ``duplicate_fraction``);
  either alone admits legitimate blocks.
- **It nudges, it never cuts.** Flagged blocks land in runs that still
  fixed the task 14% of the time.
- One nudge, then a cooldown that refuses a second nudge into the same
  loop; the budget is earned back after ``recovery_tokens`` of clean
  reasoning, so a long run can reach it again for a different loop.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class RepetitionConfig:
    min_chars: int = 4000
    min_paragraph_chars: int = 60
    min_stopwords: int = 3
    max_repeat_trip: int = 4
    duplicate_fraction_trip: float = 0.25
    cooldown_turns: int = 3
    max_nudges: int = 1
    recovery_tokens: int = 4000


DEFAULT = RepetitionConfig()

#: The nudger sees text, not tokens; the window is a coarse "has real
#: work happened since", not an accounting of spend.
CHARS_PER_TOKEN = 4

_STOPWORDS = re.compile(
    r"\b(the|is|are|to|and|of|it|that|this|be|for|but|so|if|will|need|"
    r"should|let|now|then|because|actually|wait)\b")
_FENCED = re.compile(r"```[\s\S]*?```")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Measurement:
    paragraphs: int
    duplicates: int
    duplicate_fraction: float
    max_repeat: int
    sample: str

    def public_dict(self) -> dict:
        d = asdict(self)
        d["sample"] = " ".join(self.sample.split())[:200]
        d["duplicate_fraction"] = round(self.duplicate_fraction, 3)
        return d


def _bears_prose(paragraph: str, min_stopwords: int) -> bool:
    trimmed = paragraph.strip()
    subject = _FENCED.sub("", trimmed) if trimmed.startswith("```") else trimmed
    return len(_STOPWORDS.findall(subject.lower())) >= min_stopwords


def measure(text: str, config: RepetitionConfig = DEFAULT) -> Optional[Measurement]:
    """One completed reasoning block, or ``None`` when there is nothing to
    judge (too short, or no prose in it)."""
    if not text or len(text) < config.min_chars:
        return None
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(text)]
    paragraphs = [p for p in paragraphs
                  if len(p) >= config.min_paragraph_chars
                  and _bears_prose(p, config.min_stopwords)]
    if not paragraphs:
        return None
    counts: dict[str, int] = {}
    for p in paragraphs:
        counts[p] = counts.get(p, 0) + 1
    duplicates = 0
    max_repeat = 0
    sample = ""
    for p, n in counts.items():
        duplicates += n - 1
        if n > max_repeat:
            max_repeat, sample = n, p
    return Measurement(len(paragraphs), duplicates,
                       duplicates / len(paragraphs), max_repeat, sample)


def is_loop(m: Optional[Measurement], config: RepetitionConfig = DEFAULT) -> bool:
    return (m is not None and m.max_repeat >= config.max_repeat_trip
            and m.duplicate_fraction >= config.duplicate_fraction_trip)


def describe(m: Measurement) -> str:
    """What the model is told: its own sentence quoted back (which thought
    it is stuck on), the count, that the reasoning is not carried, and one
    instruction."""
    excerpt = " ".join(m.sample.split())[:200]
    more = "…" if len(m.sample) > 200 else ""
    return "\n".join([
        f"[SYSTEM MESSAGE] In your last reply you wrote this same passage "
        f"{m.max_repeat} times, word for word:",
        "",
        f'    "{excerpt}{more}"',
        "",
        f"That reply restated {m.duplicates} of its {m.paragraphs} paragraphs "
        "verbatim. Re-deriving a conclusion you have already reached will not "
        "produce a new one, and none of that reasoning is visible to the user.",
        "",
        "You will not find it above: your reasoning from that turn is not "
        "carried into this request, so the passage is quoted here because it "
        "is the only copy you have. Take it as given rather than looking "
        "for it.",
        "",
        "Take the next concrete step instead: make the change, run the check, "
        "or say plainly what is blocking you and what you would need to get "
        "past it.",
    ])


class RepetitionNudger:
    """Rationed nudges: the guard may interrupt a cycle, not narrate it."""

    def __init__(self, config: RepetitionConfig = DEFAULT) -> None:
        self.config = config
        self.spent = 0
        self._last_turn: Optional[int] = None
        self._clean_chars = 0
        self._recovery_chars = max(0, config.recovery_tokens) * CHARS_PER_TOKEN
        self.last: Optional[Measurement] = None

    def inspect(self, text: str, turn: int) -> Optional[str]:
        """The nudge for this turn's reasoning, or ``None``."""
        cfg = self.config
        m = measure(text, cfg)
        if not is_loop(m, cfg):
            # Recovery first, so the turn that completes the window is the
            # turn the budget comes back. A tripping block adds nothing.
            if self._recovery_chars > 0 and self.spent > 0:
                self._clean_chars += len(text or "")
                if self._clean_chars >= self._recovery_chars:
                    self.spent = 0
                    self._clean_chars = 0
            return None
        if self.spent >= cfg.max_nudges:
            return None
        # Not reset by recovery: it refuses a second nudge into the same loop.
        if self._last_turn is not None and turn - self._last_turn < cfg.cooldown_turns:
            return None
        self.spent += 1
        self._clean_chars = 0
        self._last_turn = turn
        self.last = m
        return describe(m)


__all__ = ["RepetitionConfig", "DEFAULT", "Measurement", "measure",
           "is_loop", "describe", "RepetitionNudger", "CHARS_PER_TOKEN"]
