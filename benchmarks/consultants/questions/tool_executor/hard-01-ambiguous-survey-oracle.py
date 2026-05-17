"""Oracle for hard-01-ambiguous-survey."""

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


def test_mentions_json() -> None:
    """JSON codec must be named."""
    assert re.search(r"\bjson\b", OUTPUT, re.IGNORECASE), (
        f"expected mention of JSON serialization; got: {OUTPUT[:500]}"
    )


def test_mentions_binary() -> None:
    """Binary codec must be named — accept 'binary' or
    'length-prefixed' as recognisably-on-topic."""
    ok = (
        re.search(r"\bbinary\b", OUTPUT, re.IGNORECASE)
        or re.search(r"length[-\s]?prefix", OUTPUT, re.IGNORECASE)
        or re.search(r"\boctet[-\s]?stream\b", OUTPUT, re.IGNORECASE)
    )
    assert ok, (
        f"expected mention of binary/length-prefixed codec; "
        f"got: {OUTPUT[:500]}"
    )


def test_mentions_csv() -> None:
    """CSV codec must be named — the smallest file in the cohort
    is the classic miss case."""
    assert re.search(r"\bcsv\b", OUTPUT, re.IGNORECASE), (
        f"expected mention of CSV codec; got: {OUTPUT[:500]}"
    )


def test_cites_at_least_two_codec_files() -> None:
    """The answer should cite at least two of the codec files."""
    file_hits = sum(
        1 for f in ("json_codec.py", "binary_codec.py", "csv_codec.py")
        if f in OUTPUT
    )
    assert file_hits >= 2, (
        f"expected at least 2 codec file citations; got {file_hits}; "
        f"full text: {OUTPUT[:500]}"
    )


def test_used_discovery_tool() -> None:
    """The model must have surveyed or listed to discover the
    cohort; no file path was given in the prompt."""
    names = [c.get("tool") for c in _calls()]
    discovery = {"survey_project", "list_files", "glob", "grep"}
    assert any(n in discovery for n in names), (
        f"expected a discovery tool call; got: {names}"
    )


def test_read_at_least_one_codec() -> None:
    """The model must have actually read at least one of the
    codec files — listing alone isn't grounded."""
    names = [c.get("tool") for c in _calls()]
    assert "read_file" in names, (
        f"expected read_file in tool calls; got: {names}"
    )
