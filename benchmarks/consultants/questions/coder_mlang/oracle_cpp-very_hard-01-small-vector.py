"""Oracle for cpp-very_hard-01-small-vector."""

import os
import sys
from pathlib import Path

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


def test_source_present():
    assert SOURCE.is_file()


def test_uses_smallvector_template():
    src = SOURCE.read_text()
    assert "SmallVector" in src, "SmallVector template missing"


def test_no_std_vector_shortcut():
    src = SOURCE.read_text()
    assert "std::vector" not in src, (
        "std::vector shortcut not allowed; implement SmallVector "
        "with raw storage"
    )


def test_inline_under_capacity():
    rc, out, err = _run("3\npush 1\npush 2\nprint\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "2 1", f"got: {out!r}"


def test_inline_at_exact_capacity():
    rc, out, err = _run("5\npush 1\npush 2\npush 3\npush 4\nprint\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "4 1", f"got: {out!r}"


def test_heap_after_overflow():
    rc, out, err = _run("6\npush 1\npush 2\npush 3\npush 4\npush 5\nprint\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "5 0", f"got: {out!r}"


def test_pop_back():
    rc, out, err = _run(
        "8\npush 1\npush 2\npush 3\npush 4\npush 5\npop\npop\nprint\n"
    )
    assert rc == 0, f"runtime: {err}"
    # Size = 3 after 5 pushes and 2 pops. Implementations may keep
    # heap allocation after migration (so is_inline()==0) OR shrink
    # back to inline. Either is acceptable.
    parts = out.strip().split()
    assert len(parts) == 2, f"got: {parts!r}"
    assert parts[0] == "3", f"size: {parts[0]}"
    assert parts[1] in ("0", "1"), f"inline flag: {parts[1]}"


def test_many_pushes():
    body = "21\n" + "\n".join(f"push {i}" for i in range(20)) + "\nprint\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    parts = out.strip().split()
    assert parts == ["20", "0"], f"got: {parts!r}"


def test_empty_vector():
    rc, out, err = _run("1\nprint\n")
    assert rc == 0, f"runtime: {err}"
    parts = out.strip().split()
    assert parts == ["0", "1"], f"empty vector should be inline; got: {parts}"
