"""Oracle for trivial-01-find-symbol.

Reads the captured tool_executor output via env vars set by the
bench harness:

  TOOL_EXEC_OUTPUT       — final assistant text from the lane.
  TOOL_EXEC_CALLS        — JSON list of ordered tool calls.
  TOOL_EXEC_FIXTURE_DIR  — absolute path to the fixture cohort.

The expected line is hard-coded against the fixture content; if
the fixture is reflowed, this oracle must be updated to match.
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


# Locate the constant line in the real fixture so the test is
# resilient if the file gets re-indented later without anyone
# remembering to update the hard-coded number here.
_EXPECTED_PATTERN = re.compile(r"^DEFAULT_TIMEOUT_S\s*[:=]")


def _fixture_line() -> int:
    src = (FIXTURE_DIR / "config.py").read_text(encoding="utf-8")
    for idx, raw in enumerate(src.splitlines(), start=1):
        if _EXPECTED_PATTERN.match(raw.strip()):
            return idx
    raise AssertionError(
        "fixture drift: DEFAULT_TIMEOUT_S not found in config.py"
    )


def test_output_contains_constant_value() -> None:
    """The constant value (30.0 / 30) must appear in the answer."""
    assert OUTPUT, "tool_executor produced no text"
    # Accept either '30.0' or '30' or '30 ' — the value, in some
    # plausible numeric form, must show up.
    assert ("30.0" in OUTPUT) or re.search(r"\b30\b", OUTPUT), (
        f"expected the value 30.0 in the answer; got: {OUTPUT[:400]}"
    )


def test_output_cites_correct_file() -> None:
    """The citation must reference config.py (the only fixture
    file that defines the constant)."""
    assert "config.py" in OUTPUT, (
        f"expected citation to config.py; got: {OUTPUT[:400]}"
    )


def test_output_cites_correct_line() -> None:
    """The citation must include the line where the constant is
    defined. Accept ``config.py:18`` or ``line 18``-style; reject
    nearby off-by-one to enforce precision."""
    expected = _fixture_line()
    line_str = str(expected)
    patterns = [
        rf"config\.py:{line_str}\b",
        rf"line\s+{line_str}\b",
        rf"L{line_str}\b",
    ]
    ok = any(re.search(p, OUTPUT) for p in patterns)
    assert ok, (
        f"expected citation at line {expected}; got: {OUTPUT[:400]}"
    )


def test_used_grep_or_read() -> None:
    """The role should have invoked at least one of grep / read_file
    to ground the answer. Pure recall_memory hits would not be
    credible here."""
    names = {c.get("tool") for c in _calls()}
    assert names & {"grep", "read_file", "list_files", "glob"}, (
        f"expected grep/read_file/list_files/glob in tool calls; "
        f"got: {sorted(names)}"
    )
