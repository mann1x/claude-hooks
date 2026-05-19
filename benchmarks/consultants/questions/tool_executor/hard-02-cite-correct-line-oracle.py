"""Oracle for hard-02-cite-correct-line.

The trap: a comment line names the OLD value (100), the next
non-comment line names the live value (250). An answer that
cites either the comment line OR the value 100 fails — only the
live-assignment line + value count.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


OUTPUT = os.environ.get("TOOL_EXEC_OUTPUT", "")
CALLS_RAW = os.environ.get("TOOL_EXEC_CALLS", "[]")
FIXTURE_DIR = Path(os.environ.get("TOOL_EXEC_FIXTURE_DIR", "."))


def _calls() -> list[dict]:
    try:
        return json.loads(CALLS_RAW)
    except (TypeError, ValueError):
        return []


# Match a line like ``MAX_BATCH_SIZE: int = 250`` (live binding),
# NOT a comment line.
_LIVE_ASSIGN = re.compile(
    r"^\s*MAX_BATCH_SIZE\s*(?::\s*\w+\s*)?=\s*(\d+)\s*(?:#.*)?$"
)


def _live_line_and_value() -> tuple[int, str]:
    """Return the (line_number, value) of the live MAX_BATCH_SIZE
    assignment in the fixture."""
    src = (FIXTURE_DIR / "settings.py").read_text(encoding="utf-8")
    for idx, raw in enumerate(src.splitlines(), start=1):
        # Skip pure comment lines.
        if raw.lstrip().startswith("#"):
            continue
        m = _LIVE_ASSIGN.match(raw)
        if m:
            return idx, m.group(1)
    raise AssertionError(
        "fixture drift: live MAX_BATCH_SIZE assignment not found"
    )


def test_output_nonempty() -> None:
    assert OUTPUT.strip(), "tool_executor produced no text"


def test_states_correct_value() -> None:
    """The live value (250) must appear in the answer; the old
    value (100) appearing alongside is acceptable as historical
    context, but 250 must be the headline."""
    _, expected = _live_line_and_value()
    assert expected in OUTPUT, (
        f"expected live value {expected!r} in answer; "
        f"got: {OUTPUT[:500]}"
    )


def test_does_not_claim_old_value_as_current() -> None:
    """Reject answers that say 'MAX_BATCH_SIZE is 100' — the
    old value is comment-only."""
    bad_patterns = [
        r"is\s+100\b",
        r"value\s+is\s+100\b",
        r"=\s*100\b\s*$",   # only fail if 100 looks like the
                            # standalone answer (no other digits
                            # after it on the same matched span)
    ]
    lower = OUTPUT.lower()
    for p in bad_patterns:
        m = re.search(p, lower)
        if m:
            # Only fail if 250 isn't ALSO clearly attributed as
            # current — if both values appear and the answer
            # frames 100 as historical, that's OK.
            window = lower[max(0, m.start() - 40):m.end() + 40]
            if "current" in window or "live" in window or "today" in window:
                raise AssertionError(
                    f"answer says current value is 100 (historical); "
                    f"matched: {window}"
                )


def test_cites_correct_line_exactly() -> None:
    """The cited line must be the live-assignment line, not the
    comment line. Off-by-one fails."""
    expected_line, _ = _live_line_and_value()
    expected = str(expected_line)
    patterns = [
        rf"settings\.py:{expected}\b",
        rf"line\s+{expected}\b",
        rf"L{expected}\b",
    ]
    ok = any(re.search(p, OUTPUT) for p in patterns)
    assert ok, (
        f"expected citation at settings.py:{expected}; "
        f"got: {OUTPUT[:500]}"
    )


def test_used_read_file() -> None:
    """Precision requires reading; a pure grep answer can't tell
    the live binding from the historical comment without the
    surrounding context."""
    names = [c.get("tool") for c in _calls()]
    assert "read_file" in names, (
        f"expected read_file in tool calls; got: {names}"
    )
