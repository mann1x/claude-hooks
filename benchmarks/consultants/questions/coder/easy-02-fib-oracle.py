"""Oracle for easy-02-fib."""

import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

fib_mod = None
pytest_fail_reason = ""
try:
    import fib as fib_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if fib_mod is None:
        raise AssertionError(
            f"fib module not importable: {pytest_fail_reason}"
        )
    return getattr(fib_mod, "fib", None)


@contextmanager
def _timeout(seconds: float):
    """Wall-clock timeout. POSIX uses SIGALRM to interrupt the block
    mid-run; Windows (no SIGALRM) falls back to a post-hoc elapsed check
    — sufficient here because the canonical submission completes well
    within budget, and a runaway one is still bounded by the harness's
    outer pytest/subprocess timeout."""
    if hasattr(signal, "SIGALRM"):
        def _handler(signum, frame):
            raise TimeoutError(f"exceeded {seconds}s")
        old = signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    else:
        import time as _time
        start = _time.monotonic()
        yield
        if _time.monotonic() - start > seconds:
            raise TimeoutError(f"exceeded {seconds}s")


def test_base_zero():
    fn = _fn()
    assert callable(fn), "expected function `fib` in fib.py"
    assert fn(0) == 0


def test_base_one():
    fn = _fn()
    assert fn(1) == 1


def test_small_values():
    fn = _fn()
    expected = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55]
    for i, v in enumerate(expected):
        assert fn(i) == v, f"fib({i}) expected {v}, got {fn(i)}"


def test_fib_10():
    fn = _fn()
    assert fn(10) == 55


def test_fib_20():
    fn = _fn()
    assert fn(20) == 6765


def test_fib_40_is_fast():
    """Naive recursion takes seconds for n=40; iteration is
    microseconds. 2s timeout catches the slow path."""
    fn = _fn()
    with _timeout(2.0):
        assert fn(40) == 102334155


def test_negative_raises():
    fn = _fn()
    with pytest.raises(ValueError):
        fn(-1)
    with pytest.raises(ValueError):
        fn(-5)
