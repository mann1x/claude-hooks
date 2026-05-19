"""Oracle for python-medium-01-meeting-rooms."""

import os
import random
import sys
from pathlib import Path
import pytest

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

import solution  # noqa: E402


@pytest.mark.constraint
def test_callable():
    assert callable(getattr(solution, "min_meeting_rooms", None))


def test_empty():
    assert solution.min_meeting_rooms([]) == 0


def test_single_interval():
    assert solution.min_meeting_rooms([(0, 10)]) == 1


def test_classic_three_overlap():
    assert solution.min_meeting_rooms([(0, 30), (5, 10), (15, 20)]) == 2


def test_disjoint_intervals():
    assert solution.min_meeting_rooms([(7, 10), (2, 4)]) == 1


def test_half_open_boundary_no_conflict():
    # End of one meeting equals start of next — half-open means
    # they don't conflict.
    assert solution.min_meeting_rooms([(1, 5), (5, 10)]) == 1


def test_triple_overlap_at_single_point():
    assert solution.min_meeting_rooms([(1, 5), (4, 10), (3, 8)]) == 3


def test_unsorted_input_handled():
    # Same set as classic_three_overlap, scrambled.
    intervals = [(15, 20), (0, 30), (5, 10)]
    assert solution.min_meeting_rooms(intervals) == 2


def test_one_contains_another():
    # (0, 100) contains (50, 60); needs 2 rooms.
    assert solution.min_meeting_rooms([(0, 100), (50, 60)]) == 2


def test_adversarial_all_overlap_at_a_point():
    # 5000 intervals all touching t=500 — answer is 5000. A naive
    # O(N^2) overlap scan would TLE; O(N log N) finishes fast.
    intervals = [(i, 1000 - i) for i in range(500)]
    intervals += [(500 - i, 500 + i) for i in range(1, 4501)]
    n = solution.min_meeting_rooms(intervals)
    assert n == 5000, f"got {n}"


def test_large_random_sane():
    rng = random.Random(42)
    intervals = []
    for _ in range(2000):
        a = rng.randint(0, 100_000)
        b = a + rng.randint(1, 100)
        intervals.append((a, b))
    n = solution.min_meeting_rooms(intervals)
    # Reference: line-sweep with end-priority on ties.
    events = []
    for a, b in intervals:
        events.append((a, 1))
        events.append((b, -1))
    events.sort()
    cur = best = 0
    for _t, d in events:
        cur += d
        if cur > best:
            best = cur
    assert n == best, f"got {n}, expected {best}"
