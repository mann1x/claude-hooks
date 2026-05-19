"""Oracle for c-medium-01-longest-valid-parens."""

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
SOURCE = SANDBOX / "solution.c"


def _run(stdin_input: str, timeout_s: int = 10) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="c", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"gcc rejected solution.c:\n{e.stderr}") from None


def _query(s):
    rc, out, err = _run(s + "\n")
    assert rc == 0, f"runtime: {err}"
    return int(out.strip())


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


def test_example_1():
    assert _query("(()") == 2


def test_example_2():
    assert _query(")()())") == 4


def test_empty_string():
    assert _query("") == 0


def test_all_open():
    assert _query("(((((") == 0


def test_all_close():
    assert _query(")))))") == 0


def test_only_three_balanced_inside_eleven_open():
    # ((((((((((()))  →  ((()))  has length 6
    assert _query("((((((((((()))") == 6


def test_alternating_long():
    s = "()" * 100
    assert _query(s) == 200


def test_nested_deep():
    s = "(" * 100 + ")" * 100
    assert _query(s) == 200


def test_unbalanced_prefix_doesnt_eat_suffix():
    # ))))((()) — suffix "(())" length 4, prefix is unbalanced.
    assert _query("))))((()") == 2


def test_olN_required():
    # 30_000 alternating chars. O(N^2) TLEs.
    s = "()" * 15_000
    t0 = time.monotonic()
    answer = _query(s)
    dt = time.monotonic() - t0
    assert answer == 30_000, f"got {answer}"
    assert dt < 5.0, f"too slow ({dt:.2f}s) — likely O(N^2)"


def test_complex_mixed():
    # Carefully crafted: ()(()(()))  → length 10 (whole thing)
    assert _query("()(()(()))") == 10
