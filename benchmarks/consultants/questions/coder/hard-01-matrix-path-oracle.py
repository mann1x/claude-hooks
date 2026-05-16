"""Oracle for hard-01-matrix-path."""

import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

mp_mod = None
pytest_fail_reason = ""
try:
    import min_path as mp_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if mp_mod is None:
        raise AssertionError(
            f"min_path module not importable: {pytest_fail_reason}"
        )
    return getattr(mp_mod, "min_path_sum", None)


@contextmanager
def _timeout(seconds: float):
    def _handler(signum, frame):
        raise TimeoutError(f"exceeded {seconds}s")
    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_single_cell():
    fn = _fn()
    assert callable(fn), "expected `min_path_sum` in min_path.py"
    assert fn([[7]]) == 7
    assert fn([[-3]]) == -3


def test_single_row():
    """Only path: walk right through every cell."""
    fn = _fn()
    assert fn([[1, 2, 3, 4]]) == 10
    assert fn([[5, -1, 2]]) == 6


def test_single_column():
    fn = _fn()
    assert fn([[1], [2], [3], [4]]) == 10
    assert fn([[5], [-1], [2]]) == 6


def test_canonical_2x2():
    """Two paths: 1->3->4 = 8 vs 1->2->4 = 7. Min is 7."""
    fn = _fn()
    grid = [
        [1, 2],
        [3, 4],
    ]
    assert fn(grid) == 7


def test_canonical_3x3():
    """
    1 3 1
    1 5 1
    4 2 1
    Best path: 1->1->4->2->1 = 9 (down, down, right, right, right)
    Actually: 1+1+4+2+1 = 9. Let's verify all paths:
    1->3->1->1->1 = 7  (right, right, down, down)
    1->3->5->1->1 = 11
    1->1->5->1->1 = 9
    1->1->4->2->1 = 9
    Min = 7
    """
    fn = _fn()
    grid = [
        [1, 3, 1],
        [1, 5, 1],
        [4, 2, 1],
    ]
    assert fn(grid) == 7


def test_negative_values():
    fn = _fn()
    grid = [
        [1, -2],
        [-3, 4],
    ]
    # Paths: 1 + -2 + 4 = 3 (right, down); 1 + -3 + 4 = 2 (down, right)
    assert fn(grid) == 2


def test_all_negative():
    fn = _fn()
    grid = [
        [-1, -2, -3],
        [-4, -5, -6],
    ]
    # Min path is the most-negative sum. -1 + -2 + -3 + -6 = -12 (right, right, down)
    # vs -1 + -4 + -5 + -6 = -16 (down, right, right, right) — but wait, can only move
    # right or down, so from (0,0) to (1,2) we have:
    #   right, right, down: -1 + -2 + -3 + -6 = -12
    #   right, down, right: -1 + -2 + -5 + -6 = -14
    #   down, right, right: -1 + -4 + -5 + -6 = -16
    # Min = -16
    assert fn(grid) == -16


def test_performance_10x10():
    """A 10x10 grid has C(18,9) = 48620 paths. Naive recursion
    enumerates all of them; DP solves it in 100 cell-visits.
    3s timeout catches the naive submission."""
    fn = _fn()
    grid = [[(i * 7 + j * 3) % 17 for j in range(10)] for i in range(10)]
    with _timeout(3.0):
        result = fn(grid)
    # Just assert it ran and returned an int.
    assert isinstance(result, int)


def test_empty_grid_raises():
    fn = _fn()
    with pytest.raises(ValueError):
        fn([])


def test_empty_row_raises():
    fn = _fn()
    with pytest.raises(ValueError):
        fn([[]])


def test_jagged_raises():
    fn = _fn()
    with pytest.raises(ValueError):
        fn([[1, 2, 3], [4, 5]])
