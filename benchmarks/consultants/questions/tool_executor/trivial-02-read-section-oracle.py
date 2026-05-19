"""Oracle for trivial-02-read-section.

Validates that the answer surfaces every env-var the README's
Configuration section names, with the correct defaults, and that
the model cited the file (not just hallucinated the content).
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


# Three env-var names that MUST appear in the answer.
_ENV_VARS = ("DEMO_HOST", "DEMO_PORT", "DEMO_TIMEOUT_S")


def test_output_nonempty() -> None:
    assert OUTPUT.strip(), "tool_executor produced no text"


def test_all_env_vars_present() -> None:
    """Every documented env-var name should appear."""
    missing = [v for v in _ENV_VARS if v not in OUTPUT]
    assert not missing, (
        f"missing env-var names in answer: {missing}; "
        f"got: {OUTPUT[:500]}"
    )


def test_defaults_present() -> None:
    """At least two of the three defaults should appear textually.
    Allow some slack on formatting (30 vs 30.0)."""
    hits = 0
    if "127.0.0.1" in OUTPUT:
        hits += 1
    if "8080" in OUTPUT:
        hits += 1
    if "30.0" in OUTPUT or re.search(r"\b30\b", OUTPUT):
        hits += 1
    assert hits >= 2, (
        f"expected ≥2 defaults (127.0.0.1 / 8080 / 30); "
        f"hits={hits}; got: {OUTPUT[:500]}"
    )


def test_cites_readme() -> None:
    """The answer should reference README.md so a reader can
    trace it back."""
    assert "README" in OUTPUT or "readme" in OUTPUT.lower(), (
        f"expected a README citation; got: {OUTPUT[:500]}"
    )


def test_used_read_file() -> None:
    """A trivial read task should at minimum call read_file. grep
    is acceptable too as a discovery step but the file must be
    read."""
    names = [c.get("tool") for c in _calls()]
    assert "read_file" in names, (
        f"expected read_file to appear in tool calls; got: {names}"
    )
