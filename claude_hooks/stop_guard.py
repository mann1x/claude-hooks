"""
Stop-phrase guard.

Scans the last assistant message for "ownership-dodging" and
"session-quitting" phrases and returns a correction that forces the
assistant to continue working. Prevents premature stopping when the
model claims an issue is "pre-existing", asks permission mid-task, or
declares a "good stopping point" before the work is done.

The phrase list is inspired by rtfpessoa/code-factory's
``hooks/stop-phrase-guard.sh``:
https://github.com/rtfpessoa/code-factory/blob/main/hooks/stop-phrase-guard.sh

Patterns are opinionated defaults — override ``stop_guard.patterns`` in
``config/claude-hooks.json`` to customise for your own golden rules.
"""

from __future__ import annotations

import re
from typing import Optional


# Each entry: (case-insensitive regex, correction message).
# Order matters — first match wins, so put severe/specific patterns first.
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    # --- Ownership dodging: dismissing failures as not-my-fault ---
    (
        r"pre-existing",
        "NOTHING IS PRE-EXISTING. You own every change. If a test/build/lint fails, "
        "investigate and fix it. Never dismiss a failure as pre-existing.",
    ),
    (
        r"not (from|caused by|introduced by|related to) (my|the) chang",
        "NOTHING IS PRE-EXISTING. You own the codebase as it is now. "
        "Investigate the failure.",
    ),
    (
        r"(already existed|before my|prior to my) chang",
        "NOTHING IS PRE-EXISTING. There is no 'before your changes' — you own the "
        "codebase as it is now. Fix it or explain the specific technical reason it "
        "cannot be fixed.",
    ),
    (
        r"an existing (issue|bug)|existing bug",
        "NOTHING IS PRE-EXISTING. Investigate and fix, or explain exactly what is "
        "wrong and what the fix requires.",
    ),
    # --- Known-limitation dodging ---
    (
        r"known (limitation|issue)",
        "NO KNOWN LIMITATIONS. Explain the specific technical reason it cannot be "
        "fixed right now, or fix it.",
    ),
    (
        r"(future work|left as an exercise|TODO: revisit)",
        "NO KNOWN LIMITATIONS. Fix it now or describe exactly what the fix requires "
        "— not as a TODO, as a technical explanation.",
    ),
    # --- Session-length quitting ---
    (
        r"(session length|session depth|session has (gotten|been) long|"
        r"lengthy session|getting long|given the length of this)",
        "Sessions are unlimited. If work remains, do the work. Continue.",
    ),
    (
        r"(good|natural|logical) (place to stop|stopping (point|place)|checkpoint)",
        "Is the task done? If not, continue working. Sessions are unlimited.",
    ),
    (
        r"(pick (this|it) up|come back|wrap up|save it|call it) "
        r"(in a new|in the next|later|for next time|for now|here|to this later)",
        "There is no 'later' or 'next session.' Sessions are unlimited. Continue "
        "working now.",
    ),
    (
        r"\b(come back|pick (this|it) up|revisit (this|it)|continue this) "
        r"(later|next time|tomorrow)",
        "There is no 'later' or 'next time.' Sessions are unlimited. Continue now.",
    ),
    (
        r"(next (session|conversation)|follow-up (session|conversation))",
        "There is no 'next session' or 'follow-up.' This session is unlimited. "
        "Continue working.",
    ),
    (
        r"(pause here|stop here for now)",
        "Do not pause. The task is not done. Continue.",
    ),
    # --- Permission-seeking mid-task ---
    (
        r"(should|shall|would you like (me )?to|want (me )?to)"
        r"( I| we)?( now| next)? "
        r"(continue|keep going|proceed)",
        "Do not ask. The task is not done. Continue working. The user will "
        "interrupt if they want you to stop.",
    ),
    (
        r"want to continue.*or ",
        "Do not ask. Continue working. The task is not done.",
    ),
]


# Cache keyed by the raw tuple so identical config hits compile once.
_PATTERN_CACHE: dict[tuple, list[tuple[re.Pattern, str]]] = {}


def reset_pattern_cache() -> None:
    """Clear the compiled-pattern cache. For tests."""
    _PATTERN_CACHE.clear()


def load_patterns(cfg_patterns: list) -> list[tuple[re.Pattern, str]]:
    """Compile user-provided or default patterns for matching.

    cfg_patterns: list of [{"pattern": "...", "correction": "..."}] dicts, OR
                  an empty list to use DEFAULT_PATTERNS, OR
                  None to use DEFAULT_PATTERNS.

    Compiled results are cached — repeat calls with the same config hit
    the cache instead of recompiling every regex on every Stop event.
    """
    raw: list[tuple[str, str]]
    if cfg_patterns:
        raw = [
            (str(item.get("pattern", "")), str(item.get("correction", "")))
            for item in cfg_patterns
            if isinstance(item, dict) and item.get("pattern")
        ]
    else:
        raw = list(DEFAULT_PATTERNS)

    key = tuple(raw)
    cached = _PATTERN_CACHE.get(key)
    if cached is not None:
        return cached

    compiled: list[tuple[re.Pattern, str]] = []
    for pattern, correction in raw:
        try:
            compiled.append((re.compile(pattern, re.IGNORECASE), correction))
        except re.error:
            # Bad regex — skip silently rather than breaking the hook.
            continue

    _PATTERN_CACHE[key] = compiled
    return compiled


# Phrases that, when present in the message, signal the assistant is
# DISCUSSING the guard's patterns (documentation, examples, testing)
# rather than actually dodging ownership / asking to stop. Matching one
# of these makes us skip the guard entirely for this message.
DEFAULT_META_MARKERS: tuple[str, ...] = (
    "example of what would trigger",
    "would trigger the hook",
    "should trigger the hook",
    "triggers the hook",
    "stop_guard",          # direct module mention = meta discussion
    "stop-phrase-guard",
    "trigger phrase",
    "trigger phrases",
    "example trigger",
    "meta-context escape",
    "test the hook",
    "testing the hook",
)


def _strip_quoted_spans(message: str) -> str:
    """Remove text inside matched quote pairs ("…", '…', `…`, `…`).

    Patterns quoted by the assistant (e.g. documenting a trigger phrase)
    aren't really the assistant dodging — they're discussing the rule.
    We check the de-quoted version first; only a match OUTSIDE quotes
    counts as a real violation.
    """
    # Non-greedy match between identical quote characters, including
    # Markdown-style backtick code spans. Does not handle nested quotes
    # or unmatched quotes — that's OK; in those cases we just fall
    # through to the raw-match check.
    patterns = [
        r'"[^"\n]{0,400}"',
        r"'[^'\n]{0,400}'",
        r"`[^`\n]{0,400}`",
        r"“[^”\n]{0,400}”",
        r"‘[^’\n]{0,400}’",
    ]
    out = message
    for p in patterns:
        out = re.sub(p, " ", out)
    return out


def _contains_meta_marker(message: str, markers: tuple[str, ...]) -> bool:
    lower = message.lower()
    return any(m.lower() in lower for m in markers)


def check_message(
    message: str,
    patterns: Optional[list[tuple[re.Pattern, str]]] = None,
    *,
    skip_meta_context: bool = True,
    meta_markers: Optional[tuple[str, ...]] = None,
) -> Optional[str]:
    """Return the correction string for the first matching pattern, or None.

    If ``skip_meta_context`` is True (default), the check is short-circuited
    when the message looks like meta-discussion of the guard itself:
      * a match only occurs inside quoted spans ("…", '…', `…`); OR
      * the message contains a meta-marker phrase like "trigger phrase"
        or "stop_guard".
    """
    if not message:
        return None
    compiled = patterns if patterns is not None else load_patterns([])

    # First pass: find any candidate match on the raw message.
    first_match: Optional[tuple[re.Pattern, str]] = None
    for regex, correction in compiled:
        if regex.search(message):
            first_match = (regex, correction)
            break
    if first_match is None:
        return None

    if skip_meta_context:
        markers = meta_markers if meta_markers is not None else DEFAULT_META_MARKERS
        if _contains_meta_marker(message, markers):
            return None
        # Re-run the matched pattern against the de-quoted message. If the
        # match disappears when quotes are stripped, it was a quoted
        # example — not a real violation.
        dequoted = _strip_quoted_spans(message)
        regex, _ = first_match
        if not regex.search(dequoted):
            return None

    return first_match[1]
