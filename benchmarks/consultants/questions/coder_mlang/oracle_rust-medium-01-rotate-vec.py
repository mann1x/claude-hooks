"""Oracle for rust-medium-01-rotate-vec.

Compiles ``solution.rs`` from the sandbox via ``rustc -O`` and
exercises the resulting binary with controlled stdin / asserts on
stdout. The first non-Python oracle of the ``coder_mlang`` suite
— if this passes, the shared ``compile_and_run`` helper is wired
correctly.
"""

import os
import sys
from pathlib import Path

# Make ``benchmarks.consultants`` importable from anywhere pytest
# might be invoked. ``CODER_SANDBOX`` is set by the harness when
# the oracle runs. ``parents[4]`` lands on the repo root:
#   ../coder_mlang/oracle_*.py
#   ../questions/
#   ../consultants/
#   ../benchmarks/
#   ../<repo root>
_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError, compile_and_run,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.rs"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="rust", source=SOURCE,
            stdin_input=stdin_input, timeout_s=10,
        )
    except CompileError as e:
        raise AssertionError(
            f"rustc rejected solution.rs:\n{e.stderr}"
        ) from None


def test_source_file_produced():
    assert SOURCE.is_file(), (
        f"missing solution.rs; sandbox contains: "
        f"{[p.name for p in SANDBOX.iterdir()]}"
    )


def test_basic_rotate_by_2():
    rc, out, err = _run("5 2\n1 2 3 4 5\n")
    assert rc == 0, f"runtime error rc={rc}: {err}"
    assert out.strip() == "3 4 5 1 2", f"got: {out!r}"


def test_rotate_by_zero():
    rc, out, err = _run("4 0\n10 20 30 40\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "10 20 30 40"


def test_rotate_by_n_is_identity():
    rc, out, err = _run("3 3\n7 8 9\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "7 8 9"


def test_rotate_k_greater_than_n():
    # k=7, n=5 → effective k=2
    rc, out, err = _run("5 7\n1 2 3 4 5\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "3 4 5 1 2"


def test_empty_sequence():
    rc, out, err = _run("0 3\n\n")
    assert rc == 0, f"runtime error: {err}"
    # An empty line on its own is fine (".strip() == ''").
    assert out.strip() == "", f"got: {out!r}"


def test_single_element():
    rc, out, err = _run("1 5\n42\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "42"


def test_handles_negative_values():
    rc, out, err = _run("5 1\n-1 -2 -3 -4 -5\n")
    assert rc == 0, f"runtime error: {err}"
    assert out.strip() == "-2 -3 -4 -5 -1"
