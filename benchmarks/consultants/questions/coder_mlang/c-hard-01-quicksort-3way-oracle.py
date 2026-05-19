"""Oracle for c-hard-01-quicksort-3way."""

import os
import random
import sys
from pathlib import Path
import pytest

_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError, compile_and_run,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.c"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="c", source=SOURCE,
            stdin_input=stdin_input, timeout_s=15,
        )
    except CompileError as e:
        raise AssertionError(f"gcc rejected solution.c:\n{e.stderr}") from None


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


@pytest.mark.constraint
def test_function_named_correctly():
    src = SOURCE.read_text()
    assert "quicksort3" in src, "quicksort3 function missing"


@pytest.mark.constraint
def test_no_qsort_shortcut():
    src = SOURCE.read_text()
    assert "qsort(" not in src, (
        "qsort() shortcut not allowed; implement quicksort3 by hand"
    )


def test_already_sorted_small():
    rc, out, err = _run("5\n1 2 3 4 5\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "1 2 3 4 5"


def test_reverse_sorted():
    rc, out, err = _run("5\n5 4 3 2 1\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "1 2 3 4 5"


def test_random_with_duplicates():
    rng = random.Random(7)
    arr = [rng.randint(-10, 10) for _ in range(20)]
    stdin = f"{len(arr)}\n{' '.join(str(x) for x in arr)}\n"
    rc, out, err = _run(stdin)
    assert rc == 0, f"runtime: {err}"
    expected = sorted(arr)
    got = [int(x) for x in out.strip().split()]
    assert got == expected, f"got: {got}\nexpected: {expected}"


def test_all_duplicates():
    # 3-way partition's killer feature: O(N) on all-duplicates.
    rc, out, err = _run("10\n7 7 7 7 7 7 7 7 7 7\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "7 7 7 7 7 7 7 7 7 7"


def test_negatives_and_zero():
    rc, out, err = _run("6\n0 -5 3 -1 0 -5\n")
    assert rc == 0, f"runtime: {err}"
    got = [int(x) for x in out.strip().split()]
    assert got == sorted([0, -5, 3, -1, 0, -5])


def test_empty():
    rc, out, err = _run("0\n\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == ""


def test_large_random():
    rng = random.Random(42)
    arr = [rng.randint(-1000, 1000) for _ in range(5000)]
    stdin = f"{len(arr)}\n{' '.join(str(x) for x in arr)}\n"
    rc, out, err = _run(stdin)
    assert rc == 0, f"runtime rc={rc}: {err[:200]}"
    expected = sorted(arr)
    got = [int(x) for x in out.strip().split()]
    assert got == expected, "large random failed"
