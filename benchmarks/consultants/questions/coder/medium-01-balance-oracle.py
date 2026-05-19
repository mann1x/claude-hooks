"""Oracle for medium-01-balance."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

balance_mod = None
pytest_fail_reason = ""
try:
    import balance as balance_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if balance_mod is None:
        raise AssertionError(
            f"balance module not importable: {pytest_fail_reason}"
        )
    return getattr(balance_mod, "is_balanced", None)


def test_empty_is_balanced():
    fn = _fn()
    assert callable(fn), "expected function `is_balanced` in balance.py"
    assert fn("") is True


def test_simple_pairs():
    fn = _fn()
    assert fn("()") is True
    assert fn("[]") is True
    assert fn("{}") is True


def test_nested_balanced():
    fn = _fn()
    assert fn("([])") is True
    assert fn("([{}])") is True
    assert fn("[()(){}]") is True


def test_unmatched_open():
    fn = _fn()
    assert fn("(") is False
    assert fn("([") is False
    assert fn("[(") is False


def test_unmatched_close():
    fn = _fn()
    assert fn(")") is False
    assert fn("])") is False


def test_wrong_order_close():
    """The counter-trap case: a naive counter says True; a stack
    correctly says False."""
    fn = _fn()
    assert fn("(]") is False
    assert fn("([)]") is False
    assert fn("{[}]") is False


def test_non_bracket_chars_ignored():
    fn = _fn()
    assert fn("a(b)c") is True
    assert fn("foo[bar]baz{quux}") is True
    assert fn("hello world") is True   # no brackets => balanced
    assert fn("hello(world") is False  # one bracket, no match


def test_deep_nesting():
    fn = _fn()
    assert fn("(" * 100 + ")" * 100) is True
    assert fn("(" * 100 + ")" * 99) is False
