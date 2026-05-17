"""Oracle for medium-01-multifile-audit."""

from __future__ import annotations

import json
import os
import re


OUTPUT = os.environ.get("TOOL_EXEC_OUTPUT", "")
CALLS_RAW = os.environ.get("TOOL_EXEC_CALLS", "[]")


def _calls() -> list[dict]:
    try:
        return json.loads(CALLS_RAW)
    except (TypeError, ValueError):
        return []


def test_output_nonempty() -> None:
    assert OUTPUT.strip(), "tool_executor produced no text"


def test_mentions_all_three_subsystems() -> None:
    """The answer must name each subsystem prefix at least once
    (TODO(api), TODO(auth), TODO(cache))."""
    for prefix in ("api", "auth", "cache"):
        assert prefix in OUTPUT.lower(), (
            f"missing TODO group '{prefix}' in answer; "
            f"got: {OUTPUT[:600]}"
        )


def test_cites_all_three_files() -> None:
    """A complete audit must cite each of the three files at
    least once."""
    for f in ("api.py", "auth.py", "cache.py"):
        assert f in OUTPUT, (
            f"missing file citation '{f}'; got: {OUTPUT[:600]}"
        )


def test_at_least_three_line_citations() -> None:
    """At least three distinct ``file.py:N`` style citations must
    appear. Less than that and the response is closer to a
    paraphrase than a real audit."""
    matches = set(re.findall(r"(?:api|auth|cache)\.py:\d+", OUTPUT))
    assert len(matches) >= 3, (
        f"expected ≥3 file:line citations; got {len(matches)}: "
        f"{matches} — full text: {OUTPUT[:600]}"
    )


def test_used_grep() -> None:
    """The intended path is grep; this is the canonical
    multi-file scan tool."""
    names = [c.get("tool") for c in _calls()]
    assert "grep" in names, (
        f"expected grep in tool calls; got: {names}"
    )


def test_did_not_blindly_read_every_file() -> None:
    """A model that read every file with read_file (3+ calls) is
    wasteful — penalise it. ≤2 read_file calls is acceptable."""
    names = [c.get("tool") for c in _calls()]
    reads = sum(1 for n in names if n == "read_file")
    assert reads <= 2, (
        f"expected ≤2 read_file calls (grep should have done it); "
        f"saw {reads}; got: {names}"
    )
