"""Oracle for medium-02-prime-length."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

prime_length_mod = None
pytest_fail_reason = ""
try:
    import prime_length as prime_length_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if prime_length_mod is None:
        raise AssertionError(
            f"prime_length module not importable: {pytest_fail_reason}"
        )
    return getattr(prime_length_mod, "prime_length", None)


def _s(n: int) -> str:
    """Build a string of length n. Content doesn't matter — only
    length does."""
    return "x" * n


def test_length_0_is_not_prime():
    fn = _fn()
    assert callable(fn), "expected `prime_length` in prime_length.py"
    assert fn(_s(0)) is False


def test_length_1_is_not_prime():
    fn = _fn()
    assert fn(_s(1)) is False


def test_length_2_is_prime():
    """The only even prime — easy to miss with `n % 2 == 0 -> false`."""
    fn = _fn()
    assert fn(_s(2)) is True


def test_small_primes_true():
    fn = _fn()
    for n in (2, 3, 5, 7, 11, 13):
        assert fn(_s(n)) is True, f"expected prime_length({n}) True"


def test_small_composites_false():
    fn = _fn()
    for n in (4, 6, 8, 9, 10, 12, 14, 15):
        assert fn(_s(n)) is False, f"expected prime_length({n}) False"


def test_medium_primes():
    fn = _fn()
    for n in (17, 19, 23, 29, 31, 37):
        assert fn(_s(n)) is True, f"expected prime_length({n}) True"


def test_medium_composites():
    fn = _fn()
    for n in (25, 27, 33, 35, 49, 51):
        assert fn(_s(n)) is False, f"expected prime_length({n}) False"
