"""Oracle for python-very_hard-01-parser-combinator."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

import solution  # noqa: E402


def test_public_symbols_present():
    for name in ("Lit", "Seq", "Or", "Many", "parse", "ParseError"):
        assert hasattr(solution, name), f"solution.{name} missing"


def _grammar():
    return solution.Seq(
        solution.Lit("a"),
        solution.Many(solution.Or(solution.Lit("b"), solution.Lit("c"))),
    )


def test_parse_single_a():
    result = solution.parse(_grammar(), "a")
    assert result == ("a", []), f"got: {result!r}"


def test_parse_a_b():
    result = solution.parse(_grammar(), "ab")
    assert result == ("a", ["b"]), f"got: {result!r}"


def test_parse_a_bc():
    result = solution.parse(_grammar(), "abc")
    assert result == ("a", ["b", "c"]), f"got: {result!r}"


def test_parse_long_alternations():
    result = solution.parse(_grammar(), "acbcb")
    assert result == ("a", ["c", "b", "c", "b"]), f"got: {result!r}"


def test_fail_without_leading_a():
    import pytest
    with pytest.raises(solution.ParseError):
        solution.parse(_grammar(), "b")


def test_fail_on_unconsumed_input():
    import pytest
    with pytest.raises(solution.ParseError):
        solution.parse(_grammar(), "abx")


def test_fail_on_empty_input():
    import pytest
    with pytest.raises(solution.ParseError):
        solution.parse(_grammar(), "")


def test_or_falls_through_on_no_consume():
    # Or(Lit("a"), Lit("b")) on "b" should pick the second.
    g = solution.Or(solution.Lit("a"), solution.Lit("b"))
    v, pos = g("b", 0)
    assert v == "b"
    assert pos == 1


def test_many_is_greedy():
    # Many(Lit("x")) on "xxxy" should consume 3 x's, leave "y".
    g = solution.Many(solution.Lit("x"))
    v, pos = g("xxxy", 0)
    assert v == ["x", "x", "x"]
    assert pos == 3
