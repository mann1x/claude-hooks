"""Oracle for cpp-medium-01-cycle-list."""

import os
import re
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
SOURCE = SANDBOX / "solution.cpp"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="cpp", source=SOURCE,
            stdin_input=stdin_input, timeout_s=10,
        )
    except CompileError as e:
        raise AssertionError(f"g++ rejected solution.cpp:\n{e.stderr}") from None


def _query(values, cycle_to):
    body = f"{len(values)}\n{' '.join(str(v) for v in values)}\n{cycle_to}\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    return out.strip()


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


@pytest.mark.constraint
def test_uses_two_pointer_walk_not_set():
    src = SOURCE.read_text()
    # Must NOT use unordered_set / set for cycle detection.
    bad = re.search(
        r"std::(unordered_set|set)\s*<\s*\w+\s*\*\s*>",
        src,
    )
    assert bad is None, (
        f"set-based cycle detection not allowed; the spec "
        f"requires Floyd's two-pointer algorithm. Match: "
        f"{bad.group(0)!r}"
    )


def test_acyclic_list():
    assert _query([1, 2, 3, 4, 5], -1) == "none"


def test_classic_cycle_to_value_3():
    assert _query([1, 2, 3, 4, 5], 2) == "3"


def test_cycle_at_head():
    assert _query([10, 20, 30], 0) == "10"


def test_cycle_at_last_node_only():
    # 4 nodes, tail points to itself (index 3).
    assert _query([1, 2, 3, 9], 3) == "9"


def test_single_node_no_cycle():
    assert _query([42], -1) == "none"


def test_single_node_self_loop():
    assert _query([42], 0) == "42"


def test_empty_list():
    assert _query([], -1) == "none"


def test_duplicate_values_in_cycle():
    # Values can repeat — the answer is the value at the cycle
    # entry node, not the first occurrence of that value.
    assert _query([7, 7, 7], 1) == "7"
