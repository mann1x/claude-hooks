"""Oracle for cpp-hard-01-expr-eval."""

import os
import sys
from pathlib import Path

_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError, compile_and_run,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.cpp"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="cpp", source=SOURCE,
            stdin_input=stdin_input, timeout_s=10,
        )
    except CompileError as e:
        raise AssertionError(f"g++ rejected solution.cpp:\n{e.stderr}") from None


def test_source_present():
    assert SOURCE.is_file()


def test_uses_recursive_descent():
    src = SOURCE.read_text()
    # Soft check: must mention parseExpr or expr() or similar
    # naming. The spec requires a Parser-style structure but
    # accepts variants in naming.
    pat = any(t in src for t in (
        "parseExpr", "parseTerm", "parseFactor",
        "parse_expr", "parse_term", "parse_factor",
        "Expr(", "Term(", "Factor(",
    ))
    assert pat, "no recursive-descent function names found"


def test_basic_addition():
    rc, out, err = _run("1+2\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "3", f"got: {out!r}"


def test_precedence():
    rc, out, err = _run("1+2*3\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "7"


def test_parens_override_precedence():
    rc, out, err = _run("(1+2)*3\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "9"


def test_subtraction_left_associative():
    rc, out, err = _run("10-3-2\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "5"


def test_integer_division():
    rc, out, err = _run("10/3\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "3"


def test_unary_negative():
    rc, out, err = _run("-5+8\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "3"


def test_whitespace_ignored():
    rc, out, err = _run("  2  +  3  *  4  \n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "14"


def test_nested_parens():
    rc, out, err = _run("((1+2)*(3+4))\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "21"


def test_multiple_expressions():
    rc, out, err = _run("1+2\n5*6\n(7-2)/2\n")
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.split("\n") if s.strip()]
    assert lines == ["3", "30", "2"], f"got: {lines!r}"
