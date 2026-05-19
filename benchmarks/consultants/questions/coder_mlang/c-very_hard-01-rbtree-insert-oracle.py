"""Oracle for c-very_hard-01-rbtree-insert."""

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
def test_has_red_black_logic():
    src = SOURCE.read_text()
    has_color = "RED" in src and "BLACK" in src
    has_rotate = any(t in src for t in (
        "rotate_left", "rotate_right", "rotateLeft", "rotateRight",
        "rotate(", "rot_l", "rot_r",
    ))
    assert has_color, "RED / BLACK color enum or constants missing"
    assert has_rotate, "no rotation function found"


def test_single_insert():
    rc, out, err = _run("1\n42\n")
    assert rc == 0, f"runtime: {err}"
    lines = out.strip().split("\n")
    assert lines[0] == "42", f"inorder: {lines[0]!r}"
    assert lines[1] == "ok", f"verifier: {lines[1]!r}"


def test_three_ascending_keeps_invariants():
    # 1, 2, 3 inserted in order is the classic case requiring
    # rotation + recolor. Both LL and CLRS variants pass this.
    rc, out, err = _run("3\n1 2 3\n")
    assert rc == 0, f"runtime: {err}"
    lines = out.strip().split("\n")
    assert lines[0] == "1 2 3"
    assert lines[1] == "ok"


def test_descending_keeps_invariants():
    rc, out, err = _run("5\n5 4 3 2 1\n")
    assert rc == 0, f"runtime: {err}"
    lines = out.strip().split("\n")
    assert lines[0] == "1 2 3 4 5"
    assert lines[1] == "ok"


def test_random_keeps_invariants():
    rng = random.Random(99)
    vals = [rng.randint(-100, 100) for _ in range(50)]
    body = f"{len(vals)}\n{' '.join(str(v) for v in vals)}\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    lines = out.strip().split("\n")
    expected = " ".join(str(v) for v in sorted(vals))
    assert lines[0] == expected, "inorder traversal not sorted"
    assert lines[1] == "ok", "RB invariants violated after random inserts"


def test_zero_keys():
    rc, out, err = _run("0\n\n")
    assert rc == 0, f"runtime: {err}"
    # Allow either "ok" alone or empty + "ok"
    text = out.strip()
    assert "ok" in text, f"got: {text!r}"
