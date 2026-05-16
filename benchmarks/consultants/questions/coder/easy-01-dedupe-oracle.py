"""Oracle for easy-01-dedupe."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

dedupe_mod = None
pytest_fail_reason = ""
try:
    import dedupe as dedupe_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if dedupe_mod is None:
        raise AssertionError(
            f"dedupe module not importable: {pytest_fail_reason}"
        )
    return getattr(dedupe_mod, "dedupe", None)


def test_basic():
    fn = _fn()
    assert callable(fn), "expected function `dedupe` in dedupe.py"
    assert fn([1, 2, 3, 4]) == [1, 2, 3, 4]


def test_simple_duplicates():
    fn = _fn()
    assert fn([1, 2, 1, 3, 2, 4]) == [1, 2, 3, 4]


def test_all_same():
    fn = _fn()
    assert fn([7, 7, 7, 7]) == [7]


def test_order_preserved_under_duplicates():
    """Naive `list(set(items))` will fail this in general because
    set iteration order is insertion-order-on-modern-cpython BUT
    only for the most-recent values; we need first-occurrence
    order."""
    fn = _fn()
    out = fn([3, 1, 4, 1, 5, 9, 2, 6, 5, 3])
    assert out == [3, 1, 4, 5, 9, 2, 6]


def test_empty():
    fn = _fn()
    assert fn([]) == []


def test_mixed_types_hashable():
    fn = _fn()
    out = fn(["a", 1, "b", 1, "a", 2])
    assert out == ["a", 1, "b", 2]


def test_input_not_mutated():
    fn = _fn()
    original = [1, 2, 2, 3]
    snapshot = list(original)
    _ = fn(original)
    assert original == snapshot, "input list was mutated"


def test_singleton():
    fn = _fn()
    assert fn([42]) == [42]
