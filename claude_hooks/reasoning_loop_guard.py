"""Streaming repetition guard for a model's reasoning channel (plan D1).

A model that collapses into a repetition cycle mid-thought keeps
generating until something stops it. A local think budget cuts the block
at a token count, but a provider without one (Ollama Cloud) leaves nothing
between a degenerate draw and the context window — and ``chat_streamed``
counts every reasoning chunk as liveness, so the stall monitor sees a
healthy stream until the per-call hard cap (5–60 min of billed time).

Ported verbatim from Cerebriline (``sdk/packages/agents/src/
reasoning-loop-guard.ts``, ``823f20dc6``). Three signals, all derived from
real collapses:

``low-uniqueness``  a full window of completed lines carrying almost no
                    distinct lines (GLM-5.3-Flash flipping two checklist
                    items between ``[ ]`` and ``[x]`` for 60k characters,
                    which defeats an exact-period test).
``line-cycle``      the same 1..N lines repeating with an exact period.
``phrase-cycle``    one fragment repeating inside a single unbroken line.
``block-cycle``     ours, not the fork's: a block of 7..128 lines repeating
                    with an exact period, which the 60-line window of the
                    first two cannot hold. The guard replay (2026-10-16,
                    ``benchmarks/consultants/guard_replay.py``) found six
                    turns of 190k-250k characters in 4,117 recorded blocks
                    whose last half was one 34-62-line block (code + prose)
                    repeated verbatim, which none of the three caught.

Length is never the signal: healthy agentic reasoning runs 150k–260k
characters a turn. Thresholds were set on 1,556 reasoning blocks from 82
recorded sessions plus 196k characters of a run that completed its task.

Always on, on every backend (owner, 2026-10-10): loop guards are not part
of the strict profile.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass(frozen=True)
class GuardConfig:
    min_chars: int = 4000
    window: int = 60
    max_period: int = 6
    min_cycles: int = 20
    max_unique_in_window: int = 4
    min_line_chars: int = 2000
    phrase_max_period: int = 120
    phrase_min_cycles: int = 12
    #: block-cycle: the lines kept, the longest period tried, the repeats
    #: required (the block itself included) and the characters they must
    #: span, checked every ``block_check_every`` new lines.
    block_window: int = 512
    block_max_period: int = 128
    block_min_cycles: int = 3
    block_min_chars: int = 6000
    block_check_every: int = 16
    #: Consecutive cut turns that end the lane.
    max_consecutive_trips: int = 3


DEFAULT = GuardConfig()


@dataclass(frozen=True)
class Verdict:
    kind: str                  # low-uniqueness | line-cycle | phrase-cycle | block-cycle
    chars: int                 # reasoning characters seen at the trip
    sample: str                # a short excerpt of the repeating unit
    period: Optional[int] = None
    cycles: Optional[int] = None
    distinct: Optional[int] = None

    def public_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def describe(v: Verdict) -> str:
    """The diagnosis only, never the consequence: the caller decides."""
    where = f"after {v.chars:,} characters of reasoning"
    sample = f" Repeating: {v.sample!r}" if v.sample else ""
    if v.kind == "low-uniqueness":
        return (f"The model's reasoning collapsed into {v.distinct} distinct "
                f"lines over {where}.{sample}")
    if v.kind in ("line-cycle", "block-cycle"):
        return (f"The model's reasoning repeated a {v.period}-line block "
                f"{v.cycles} times {where}.{sample}")
    return (f"The model's reasoning repeated a {v.period}-character phrase "
            f"{v.cycles} times {where}.{sample}")


@dataclass
class ReasoningLoopGuard:
    config: GuardConfig = field(default_factory=GuardConfig)
    chars: int = 0
    lines: list = field(default_factory=list)
    pending: str = ""
    verdict: Optional[Verdict] = None
    block: list = field(default_factory=list)
    _since_block_check: int = 0

    def push(self, delta: str) -> Optional[Verdict]:
        """Feed one reasoning delta. Returns the verdict the first time the
        block is judged degenerate, and the same verdict after that."""
        if self.verdict is not None or not delta:
            return self.verdict
        cfg = self.config
        self.chars += len(delta)
        self.pending += delta
        while True:
            nl = self.pending.find("\n")
            if nl == -1:
                break
            line = self.pending[:nl].strip()
            self.pending = self.pending[nl + 1:]
            if line:
                self.lines.append(line)
                if len(self.lines) > cfg.window:
                    self.lines.pop(0)
                self.block.append(line)
                if len(self.block) > cfg.block_window:
                    self.block.pop(0)
                self._since_block_check += 1
        if self.chars < cfg.min_chars:
            return None
        found = (self._low_uniqueness() or self._line_cycle()
                 or self._phrase_cycle() or self._block_cycle())
        if found is not None:
            self.verdict = found
        return self.verdict

    def _low_uniqueness(self) -> Optional[Verdict]:
        cfg = self.config
        if len(self.lines) < cfg.window:
            return None
        distinct = list(dict.fromkeys(self.lines))
        if len(distinct) > cfg.max_unique_in_window:
            return None
        return Verdict("low-uniqueness", self.chars,
                       " / ".join(distinct)[:160], distinct=len(distinct))

    def _line_cycle(self) -> Optional[Verdict]:
        cfg = self.config
        lines = self.lines
        n = len(lines)
        if n < cfg.max_period * 2:
            return None
        for p in range(1, cfg.max_period + 1):
            cycles = 0
            i = n - p
            while i - p >= 0:
                if lines[i:i + p] != lines[i - p:i]:
                    break
                cycles += 1
                i -= p
            if cycles >= cfg.min_cycles:
                return Verdict("line-cycle", self.chars,
                               " / ".join(lines[n - p:])[:160],
                               period=p, cycles=cycles + 1)
        return None

    def _phrase_cycle(self) -> Optional[Verdict]:
        cfg = self.config
        tail = self.pending
        if len(tail) < cfg.min_line_chars:
            return None
        for p in range(4, cfg.phrase_max_period + 1):
            unit = tail[len(tail) - p:]
            cycles = 0
            at = len(tail) - p
            while at - p >= 0 and tail.startswith(unit, at - p):
                cycles += 1
                at -= p
            if cycles >= cfg.phrase_min_cycles:
                return Verdict("phrase-cycle", self.chars,
                               " ".join(unit.split())[:160],
                               period=p, cycles=cycles + 1)
        return None

    def _block_cycle(self) -> Optional[Verdict]:
        cfg = self.config
        if self._since_block_check < cfg.block_check_every:
            return None
        self._since_block_check = 0
        lines = self.block
        n = len(lines)
        for p in range(cfg.max_period + 1, cfg.block_max_period + 1):
            if n < p * cfg.block_min_cycles:
                break
            unit = lines[n - p:]
            repeats = 1
            i = n - p
            while i - p >= 0 and lines[i - p:i] == unit:
                repeats += 1
                i -= p
            if repeats >= cfg.block_min_cycles and len(set(unit)) > 1 \
                    and sum(map(len, unit)) * repeats >= cfg.block_min_chars:
                return Verdict("block-cycle", self.chars,
                               " / ".join(unit[:3])[:160],
                               period=p, cycles=repeats)
        return None


def scan(text: str, config: GuardConfig = DEFAULT,
         chunk: int = 512) -> Optional[Verdict]:
    """The guard over a finished block, fed in stream-sized pieces — for a
    reply that was not streamed. Same verdicts as the live guard."""
    guard = ReasoningLoopGuard(config)
    for i in range(0, len(text or ""), chunk):
        v = guard.push(text[i:i + chunk])
        if v is not None:
            return v
    return None


#: ``done_reason`` a cut reply carries, verbatim through every translator.
DONE_REASON = "reasoning_loop"

__all__ = ["GuardConfig", "DEFAULT", "Verdict", "ReasoningLoopGuard",
           "describe", "scan", "DONE_REASON"]
