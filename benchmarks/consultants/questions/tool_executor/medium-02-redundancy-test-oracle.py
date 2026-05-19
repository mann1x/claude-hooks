"""Oracle for medium-02-redundancy-test.

The answer is embedded in the question's ``why`` block. A
well-calibrated tool_executor should answer without re-reading.
This oracle checks content fidelity AND tool-call frugality.
"""

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


def test_states_pool_size_value() -> None:
    """The answer must include the value 50."""
    assert re.search(r"\b50\b", OUTPUT), (
        f"expected the value 50 in the answer; got: {OUTPUT[:500]}"
    )


def test_names_constant() -> None:
    """The constant name should appear so the researcher can
    pattern-match the fact."""
    assert "DEFAULT_POOL_SIZE" in OUTPUT, (
        f"expected DEFAULT_POOL_SIZE in answer; got: {OUTPUT[:500]}"
    )


def test_no_redundant_reads() -> None:
    """At most ONE filesystem-touching tool call is acceptable
    (defensive grep / recall_memory). Two or more wastes tokens
    given the answer is in the prompt's why block."""
    names = [c.get("tool") for c in _calls()]
    fs_tool_calls = sum(
        1 for n in names
        if n in {"read_file", "grep", "glob", "list_files", "survey_project"}
    )
    assert fs_tool_calls <= 1, (
        f"redundancy violation: {fs_tool_calls} filesystem-touching "
        f"tool calls when the answer was already in the why block; "
        f"calls={names}"
    )


def test_did_not_read_notes_md() -> None:
    """The fixture's notes.md does NOT contain the answer. If the
    model read it, that's clearly redundant work."""
    for c in _calls():
        if c.get("tool") != "read_file":
            continue
        args = c.get("args", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {}
        path = (args.get("path") if isinstance(args, dict) else "") or ""
        assert "notes.md" not in str(path), (
            f"read_file against notes.md is pure redundancy; "
            f"args={args}"
        )
