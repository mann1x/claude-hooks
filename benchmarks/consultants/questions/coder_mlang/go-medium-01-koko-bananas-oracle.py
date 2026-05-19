"""Oracle for go-medium-01-koko-bananas."""

import math
import os
import sys
import time
from pathlib import Path
import pytest

_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError, compile_and_run,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.go"


def _run(stdin_input: str, timeout_s: int = 10) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="go", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"go build rejected solution.go:\n{e.stderr}") from None


def _query(piles, H):
    body = f"{len(piles)} {H}\n{' '.join(str(x) for x in piles)}\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    return int(out.strip())


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


def test_example_one():
    assert _query([3, 6, 7, 11], 8) == 4


def test_example_two():
    assert _query([30, 11, 23, 4, 20], 5) == 30


def test_example_three():
    assert _query([30, 11, 23, 4, 20], 6) == 23


def test_single_pile_one_hour():
    assert _query([50], 1) == 50


def test_single_pile_h_equals_pile():
    assert _query([50], 50) == 1


def test_h_equals_n():
    # H == n: Koko must finish each pile in one hour; K = max pile.
    assert _query([3, 7, 11, 2, 5], 5) == 11


def test_huge_piles_olog_required():
    # Max pile near 10^9; H == n+1. Binary search finds answer
    # in O(log 10^9) ≈ 30 iters; linear scan is 10^9 iters.
    piles = [999_999_983]
    H = 2
    t0 = time.monotonic()
    answer = _query(piles, H)
    dt = time.monotonic() - t0
    expected = math.ceil(999_999_983 / 2)
    assert answer == expected, f"got {answer}, expected {expected}"
    assert dt < 5.0, f"too slow ({dt:.2f}s) — likely linear scan"


def test_ceiling_division_correctness():
    # If naive floor division is used, this test fails.
    # piles=[10], H=3: K=4 → ceil(10/4)=3 hours (fits)
    # K=3 → ceil(10/3)=4 hours (doesn't fit)
    assert _query([10], 3) == 4


def test_min_h_equals_total_sum():
    # Total bananas = 20, H = 20 → K = 1.
    assert _query([5, 5, 5, 5], 20) == 1
