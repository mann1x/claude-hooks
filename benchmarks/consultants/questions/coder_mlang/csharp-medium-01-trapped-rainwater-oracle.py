"""Oracle for csharp-medium-01-trapped-rainwater."""

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
SOURCE = SANDBOX / "solution.cs"


def _run(stdin_input: str, timeout_s: int = 60) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="csharp", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"dotnet rejected solution.cs:\n{e.stderr}") from None


def _query(heights):
    body = f"{len(heights)}\n{' '.join(str(h) for h in heights)}\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    return int(out.strip())


def _reference(h):
    n = len(h)
    if n == 0:
        return 0
    lmax = [0] * n
    rmax = [0] * n
    lmax[0] = h[0]
    for i in range(1, n):
        lmax[i] = max(lmax[i-1], h[i])
    rmax[n-1] = h[n-1]
    for i in range(n - 2, -1, -1):
        rmax[i] = max(rmax[i+1], h[i])
    return sum(min(lmax[i], rmax[i]) - h[i] for i in range(n))


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


def test_example_one():
    assert _query([0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1]) == 6


def test_example_two():
    assert _query([4, 2, 0, 3, 2, 5]) == 9


def test_empty():
    assert _query([]) == 0


def test_single_bar():
    assert _query([7]) == 0


def test_flat():
    assert _query([3, 3, 3, 3, 3]) == 0


def test_monotonic_up():
    assert _query([1, 2, 3, 4, 5]) == 0


def test_monotonic_down():
    assert _query([5, 4, 3, 2, 1]) == 0


def test_deep_valley():
    # Two 10-tall walls with a 0-floor between them, 8 wide.
    bars = [10] + [0] * 8 + [10]
    # Floor traps 10 units per cell × 8 cells = 80.
    assert _query(bars) == 80


def test_olarge_random_consistent():
    rng = random.Random(7)
    h = [rng.randint(0, 100) for _ in range(2000)]
    got = _query(h)
    assert got == _reference(h), f"got {got}, expected {_reference(h)}"


def test_100k_must_be_oN():
    rng = random.Random(99)
    h = [rng.randint(0, 1000) for _ in range(100_000)]
    expected = _reference(h)
    t0 = time.monotonic()
    got = _query(h)
    dt = time.monotonic() - t0
    assert got == expected, f"got {got}, expected {expected}"
    # The dotnet startup alone is ~5-8s; the actual O(N) compute
    # is microseconds. An O(N^2) implementation on N=100_000 is
    # ~10^10 ops which takes minutes — well beyond the 60s
    # timeout. The strict bound here catches it before the
    # timeout fires.
    assert dt < 30.0, f"too slow ({dt:.2f}s) — likely O(N^2)"
