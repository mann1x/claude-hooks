"""
Stop-phrase scanner — stellaraccident's behaviour canaries (#42796).

A tiny in-stream pattern matcher that tags assistant text output with
behaviour-category counts:

  ownership_dodging        "not caused by my changes" etc.
  permission_seeking       "should I continue?" etc.
  premature_stopping       "good stopping point" etc.
  known_limitation_labeling  "known limitation", "future work"
  session_length_excuses   "continue in a new session"
  simplest_fix             "simplest fix / approach"
  reasoning_reversal       "oh wait", "actually,"
  self_admitted_error      "that was lazy", "I rushed this"

One scanner instance per response. Opt-in — only constructed when
``proxy.scan_stop_phrases`` is true. Phrase list lives in
``config/stop_phrases.yaml`` so it can be tuned without code changes.

Stdlib only. The YAML loader is a hand-rolled subset (key / list /
string values) — enough for this file, no PyYAML dep required.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.proxy.stop_phrase_guard")


DEFAULT_PHRASES_PATH = Path(__file__).resolve().parents[2] / "config" / "stop_phrases.yaml"


class StopPhraseScanner:
    """Incrementally scan streaming text for stop-phrase matches.

    Usage::

        scanner = StopPhraseScanner.from_file(Path("config/stop_phrases.yaml"))
        for chunk in text_chunks:
            scanner.feed(chunk)
        print(scanner.category_counts)

    ``feed`` is safe to call with empty strings or bytes (decoded
    automatically). Matches are counted per category, not per phrase
    — if the same phrase fires twice, the category tally goes +2.

    A tail buffer (``_carry``) bridges chunk boundaries so patterns
    like ``"should I continue"`` still match when ``"should I "`` and
    ``"continue"`` arrive in separate chunks.
    """

    # Longest phrase we care about is ~60 chars; 256 covers it
    # comfortably without unbounded memory use per scanner.
    _CARRY_LEN = 256

    def __init__(self, categories: dict[str, list[str]]):
        self.categories: dict[str, list[re.Pattern]] = {}
        for name, patterns in categories.items():
            compiled = []
            for p in patterns:
                try:
                    compiled.append(re.compile(p, re.IGNORECASE))
                except re.error as e:
                    log.warning("stop_phrase %r / %r: %s", name, p, e)
            if compiled:
                self.categories[name] = compiled
        self.category_counts: dict[str, int] = {k: 0 for k in self.categories}
        self._carry: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "StopPhraseScanner":
        return cls(_load_phrases_yaml(path))

    def feed(self, text) -> None:
        """Append ``text`` to the scanning window and count matches.

        Only the last ``_CARRY_LEN`` characters of the previous feed
        are re-scanned — the counter is incremented solely by new
        matches that straddle or appear inside the new chunk, so
        matches are never double-counted across chunk boundaries.
        """
        if not text:
            return
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", errors="replace")
            except Exception:
                return
        window = self._carry + text
        for name, patterns in self.categories.items():
            for pat in patterns:
                # Restrict matches to positions that touch the new
                # chunk. If a match ends within ``self._carry`` it was
                # already counted on a prior feed().
                for m in pat.finditer(window):
                    if m.end() <= len(self._carry):
                        continue
                    self.category_counts[name] += 1
        # Update carry with the tail of the full window.
        self._carry = window[-self._CARRY_LEN:]

    def total_hits(self) -> int:
        return sum(self.category_counts.values())


# ---------------------------------------------------------------------- #
# Minimal YAML subset loader — supports:
#   top_level_key:
#     - "literal string"
#     - 'single-quoted'
#     - unquoted_plain
# Comments (lines starting with '#', or trailing ' # ...') are
# stripped. Blank lines ignored. No anchors, flow style, or nested
# maps — intentionally simple so stdlib is enough.
# ---------------------------------------------------------------------- #
def _load_phrases_yaml(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        log.warning("stop_phrase file %s not found — scanner disabled", path)
        return {}
    out: dict[str, list[str]] = {}
    current_key: Optional[str] = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if stripped.startswith((" ", "\t")):
            # Must be a list item under the current key.
            item = stripped.lstrip().lstrip("-").strip()
            if current_key is None or not item:
                continue
            item = _unquote(item)
            out.setdefault(current_key, []).append(item)
        else:
            # New top-level key: ``name:``
            if stripped.endswith(":"):
                current_key = stripped[:-1].strip()
                out.setdefault(current_key, [])
            else:
                current_key = None   # malformed line, drop
    return out


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        # Undo common escapes. Limited set — matches what we put in
        # the YAML file above.
        return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
    return s
