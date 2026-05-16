"""Oracle for csharp-hard-01-async-debounce."""

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
SOURCE = SANDBOX / "solution.cs"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="csharp", source=SOURCE,
            stdin_input=stdin_input, timeout_s=60,
        )
    except CompileError as e:
        raise AssertionError(f"dotnet rejected solution.cs:\n{e.stderr}") from None


def test_source_present():
    assert SOURCE.is_file()


def test_uses_async_idioms():
    src = SOURCE.read_text()
    assert "async" in src, "async keyword missing"
    assert "await" in src, "await keyword missing"
    assert "CancellationTokenSource" in src, (
        "CancellationTokenSource missing — required idiom for "
        "cancellable Task.Delay"
    )


def test_single_push_emits():
    rc, out, err = _run("push 42\n")
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.strip().split("\n") if s.strip()]
    # Single push → flushed at EOF → exactly 1 line "42".
    assert lines == ["42"], f"got: {lines!r}"


def test_coalesces_rapid_pushes():
    # Three pushes within 50 ms each. Window is 100 ms, so all
    # three coalesce into the final value emitted on flush.
    rc, out, err = _run(
        "push 1\nsleep 30\npush 2\nsleep 30\npush 3\n"
    )
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.strip().split("\n") if s.strip()]
    # Either the coalesced value emits via timer (200 ms quiet
    # before flush) OR it emits via FlushAsync. Either way the
    # FINAL line must be "3".
    assert lines and lines[-1] == "3", f"got: {lines!r}"


def test_quiet_window_emits_then_new_push():
    # Push 1, wait 200 ms (quiet window passes -> emit 1), push 2,
    # EOF flushes -> emit 2.
    rc, out, err = _run("push 1\nsleep 200\npush 2\n")
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.strip().split("\n") if s.strip()]
    assert lines == ["1", "2"], f"got: {lines!r}"


def test_no_input_no_output():
    rc, out, err = _run("")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == ""


def test_long_quiet_emits_each():
    rc, out, err = _run(
        "push 10\nsleep 200\npush 20\nsleep 200\npush 30\n"
    )
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.strip().split("\n") if s.strip()]
    assert lines == ["10", "20", "30"], f"got: {lines!r}"
