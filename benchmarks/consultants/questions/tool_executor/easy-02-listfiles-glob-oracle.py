"""Oracle for easy-02-listfiles-glob."""

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


_MODULE_NAMES = ("parser", "runner", "serializer", "validator")

# Keywords specific to each module that must appear in any
# half-credible summary. Two of these must match (i.e. the model
# correctly described at least two of the modules) to pass.
_MODULE_KEYWORDS = {
    "parser":     ("parse", "token", "ast"),
    "runner":     ("run", "pipeline", "orchestrat", "entry"),
    "serializer": ("serial", "dump", "bytes", "encod"),
    "validator":  ("validate", "schema", "required", "rule"),
}


def test_output_nonempty() -> None:
    assert OUTPUT.strip(), "tool_executor produced no text"


def test_lists_all_module_names() -> None:
    """All four non-init module names should appear somewhere in
    the answer — the model must at least have enumerated them.
    """
    missing = [m for m in _MODULE_NAMES if m not in OUTPUT]
    assert not missing, (
        f"missing module names in answer: {missing}; "
        f"got: {OUTPUT[:600]}"
    )


def test_describes_at_least_three_modules() -> None:
    """For at least 3 modules, the summary must include at least
    one of the per-module keywords (case-insensitive)."""
    lower = OUTPUT.lower()
    described = 0
    for mod, keywords in _MODULE_KEYWORDS.items():
        if mod not in lower:
            continue
        if any(kw in lower for kw in keywords):
            described += 1
    assert described >= 3, (
        f"only {described}/4 modules got a content-bearing "
        f"description; got: {OUTPUT[:600]}"
    )


def test_used_discovery_tool() -> None:
    """The model must have called at least one discovery tool
    (list_files or glob). A model that only read_files by guessing
    paths is brittle and not what the question rewards."""
    names = [c.get("tool") for c in _calls()]
    assert ("list_files" in names) or ("glob" in names), (
        f"expected list_files or glob in tool calls; got: {names}"
    )


def test_used_read_file() -> None:
    names = [c.get("tool") for c in _calls()]
    assert "read_file" in names, (
        f"expected read_file in tool calls; got: {names}"
    )
