"""Oracle for hard-02-digit-filter."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

df_mod = None
pytest_fail_reason = ""
try:
    import digit_filter as df_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if df_mod is None:
        raise AssertionError(
            f"digit_filter module not importable: {pytest_fail_reason}"
        )
    return getattr(df_mod, "sum_odd_first_last", None)


def test_empty_returns_zero():
    fn = _fn()
    assert callable(fn), "expected `sum_odd_first_last` in digit_filter.py"
    assert fn([]) == 0


def test_basic_positive():
    """11: first=1, last=1, both odd. > 10. Count = 1."""
    fn = _fn()
    assert fn([11]) == 1


def test_basic_excluded_even_first():
    """22: first=2 (even). Don't count."""
    fn = _fn()
    assert fn([22]) == 0


def test_basic_excluded_even_last():
    """13: first=1 (odd), last=3 (odd). DO count. 14: last=4 (even). Don't."""
    fn = _fn()
    assert fn([13]) == 1
    assert fn([14]) == 0


def test_boundary_at_10_excluded():
    """10 is NOT strictly greater than 10."""
    fn = _fn()
    assert fn([10]) == 0


def test_single_digits_excluded():
    """Every value <= 10 is excluded regardless of digit parity."""
    fn = _fn()
    for v in (1, 3, 5, 7, 9):
        assert fn([v]) == 0, f"{v} should not count (<=10)"


def test_eleven_is_included():
    fn = _fn()
    assert fn([11]) == 1
    assert fn([99]) == 1


def test_three_digit_numbers():
    """123: first=1, last=3, both odd. Count. 124: last=4. Don't.
    245: first=2 (even). Don't."""
    fn = _fn()
    assert fn([123]) == 1
    assert fn([124]) == 0
    assert fn([245]) == 0
    assert fn([357]) == 1  # first=3 odd, last=7 odd


def test_mix():
    fn = _fn()
    # 11 (ok), 22 (even first), 33 (ok), 44 (even first),
    # 10 (boundary), 5 (too small), 357 (ok)
    assert fn([11, 22, 33, 44, 10, 5, 357]) == 3


def test_negatives_excluded_by_value():
    """Negative values are < 10, so excluded by the > 10 check.
    Even though the abs-value digits would qualify."""
    fn = _fn()
    assert fn([-11, -33, -357]) == 0


def test_zero_excluded():
    fn = _fn()
    assert fn([0]) == 0


def test_large_value():
    """1234567: first=1, last=7, both odd. > 10. Count."""
    fn = _fn()
    assert fn([1234567]) == 1


def test_repeated_values():
    fn = _fn()
    # Each occurrence counts once.
    assert fn([11, 11, 11]) == 3
