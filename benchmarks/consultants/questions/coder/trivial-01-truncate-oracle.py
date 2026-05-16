"""Oracle for trivial-01-truncate. Imports the model-produced
``truncate.py`` from ``$CODER_SANDBOX`` and verifies edge cases."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

# Imported lazily — pytest collection should not fail when the
# module is missing; tests should report individually.
truncate_mod = None
try:
    import truncate as truncate_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)
else:
    pytest_fail_reason = ""


def _fn():
    if truncate_mod is None:
        raise AssertionError(
            f"truncate module not importable: {pytest_fail_reason}"
        )
    return getattr(truncate_mod, "truncate", None)


def test_basic_case():
    fn = _fn()
    assert callable(fn), "expected function `truncate` in truncate.py"
    assert fn("hello", 3) == "hel"


def test_n_greater_than_length():
    fn = _fn()
    assert fn("hi", 10) == "hi"


def test_n_equals_length():
    fn = _fn()
    assert fn("abc", 3) == "abc"


def test_n_zero_returns_empty():
    fn = _fn()
    assert fn("hello", 0) == ""


def test_n_negative_returns_empty():
    fn = _fn()
    assert fn("hello", -3) == ""


def test_empty_string():
    fn = _fn()
    assert fn("", 5) == ""
    assert fn("", 0) == ""


def test_unicode_safe():
    # Edge case: multi-byte unicode counted as one char per code point.
    fn = _fn()
    assert fn("héllo", 2) == "hé"
