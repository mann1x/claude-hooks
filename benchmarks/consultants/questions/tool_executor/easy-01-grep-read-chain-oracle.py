"""Oracle for easy-01-grep-read-chain."""

from __future__ import annotations

import json
import os


OUTPUT = os.environ.get("TOOL_EXEC_OUTPUT", "")
CALLS_RAW = os.environ.get("TOOL_EXEC_CALLS", "[]")


def _calls() -> list[dict]:
    try:
        return json.loads(CALLS_RAW)
    except (TypeError, ValueError):
        return []


def test_output_nonempty() -> None:
    assert OUTPUT.strip(), "tool_executor produced no text"


def test_function_signature_present() -> None:
    """The answer must include the function signature."""
    assert "def handle_auth" in OUTPUT, (
        f"expected 'def handle_auth' in answer; got: {OUTPUT[:500]}"
    )


def test_function_body_marker_present() -> None:
    """At least one identifying line from the function body must
    be present. Picking ``record = store.get`` because it's the
    first non-docstring line and is unique to this function."""
    assert "store.get(username)" in OUTPUT, (
        f"expected the body line referencing store.get(username); "
        f"got: {OUTPUT[:500]}"
    )


def test_raises_permission_error_present() -> None:
    """The error path is part of the function contract; if the
    model returned only the happy path it has cut the body."""
    assert ("PermissionError" in OUTPUT
            or "raise PermissionError" in OUTPUT), (
        f"expected PermissionError in the returned body; "
        f"got: {OUTPUT[:500]}"
    )


def test_cites_auth_py() -> None:
    """The citation must reference auth.py specifically (not just
    'the auth module')."""
    assert "auth.py" in OUTPUT, (
        f"expected explicit 'auth.py' citation; got: {OUTPUT[:500]}"
    )


def test_used_grep() -> None:
    """The intended path is grep → read. A model that immediately
    reads every file in the cohort is wasteful; require grep
    appears in the call log."""
    names = [c.get("tool") for c in _calls()]
    # ``glob`` is an acceptable substitute for grep here (both can
    # locate the file). list_files alone isn't — that's just an ls.
    assert ("grep" in names) or ("glob" in names), (
        f"expected grep or glob to appear before read_file; "
        f"got: {names}"
    )


def test_read_file_used() -> None:
    names = [c.get("tool") for c in _calls()]
    assert "read_file" in names, (
        f"expected read_file in tool calls; got: {names}"
    )
