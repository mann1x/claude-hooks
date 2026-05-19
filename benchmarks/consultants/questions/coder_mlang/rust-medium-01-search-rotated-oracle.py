"""Oracle for rust-medium-01-search-rotated."""

import os
import random
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
SOURCE = SANDBOX / "solution.rs"


def _run(stdin_input: str, timeout_s: int = 10) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="rust", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"rustc rejected solution.rs:\n{e.stderr}") from None


def _query(arr, tgt):
    body = f"{len(arr)}\n{' '.join(str(x) for x in arr)}\n{tgt}\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    return int(out.strip())


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


def test_classic_present():
    assert _query([4, 5, 6, 7, 0, 1, 2], 0) == 4


def test_classic_absent():
    assert _query([4, 5, 6, 7, 0, 1, 2], 3) == -1


def test_empty_array():
    assert _query([], 5) == -1


def test_single_element_hit():
    assert _query([1], 1) == 0


def test_single_element_miss():
    assert _query([1], 0) == -1


def test_no_rotation():
    assert _query([1, 2, 3, 4, 5], 3) == 2


def test_full_rotation():
    # Rotated by N == not rotated.
    assert _query([1, 2, 3, 4, 5], 4) == 3


def test_pivot_element():
    # Pivot is at index 4: value 0 is the smallest, target it.
    assert _query([4, 5, 6, 7, 0, 1, 2], 0) == 4


def test_max_element():
    # Element right before the pivot.
    assert _query([4, 5, 6, 7, 0, 1, 2], 7) == 3


def test_adversarial_size_must_be_olog():
    n = 100_000
    pivot = 51_234
    base = list(range(n))
    arr = base[pivot:] + base[:pivot]
    target = 99_999
    # Should be found very quickly (binary search). Anything > 1s
    # implies linear scan.
    t0 = time.monotonic()
    idx = _query(arr, target)
    dt = time.monotonic() - t0
    assert idx == arr.index(target), f"got {idx}"
    # Generous wall budget (subprocess compile + run dominates).
    # But for binary search the actual search itself is microseconds.
    assert dt < 5.0, f"too slow ({dt:.2f}s) — likely linear scan"


def test_random_consistency():
    rng = random.Random(13)
    for _ in range(8):
        size = rng.randint(2, 50)
        base = sorted(rng.sample(range(-200, 200), size))
        pivot = rng.randint(0, size - 1)
        arr = base[pivot:] + base[:pivot]
        # Target picks: half present, half absent.
        if rng.random() < 0.5:
            tgt = rng.choice(arr)
            expected = arr.index(tgt)
        else:
            tgt = 1000 + rng.randint(0, 10)
            expected = -1
        got = _query(arr, tgt)
        assert got == expected, (
            f"arr={arr} tgt={tgt} expected={expected} got={got}"
        )
