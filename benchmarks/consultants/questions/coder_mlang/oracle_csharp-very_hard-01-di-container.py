"""Oracle for csharp-very_hard-01-di-container."""

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


def _run(stdin_input: str = "") -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="csharp", source=SOURCE,
            stdin_input=stdin_input, timeout_s=60,
        )
    except CompileError as e:
        raise AssertionError(f"dotnet rejected solution.cs:\n{e.stderr}") from None


def test_source_present():
    assert SOURCE.is_file()


def test_has_container_class():
    src = SOURCE.read_text()
    assert "class Container" in src, "Container class missing"
    assert "CircularDependencyException" in src, (
        "CircularDependencyException type missing"
    )


def test_uses_reflection():
    src = SOURCE.read_text()
    assert (
        "GetConstructors" in src
        or "GetConstructor" in src
        or "ConstructorInfo" in src
    ), "no reflection-based constructor lookup found"


def test_end_to_end_output():
    rc, out, err = _run()
    assert rc == 0, f"runtime: {err}"
    lines = [s for s in out.strip().split("\n") if s.strip()]
    # Exactly 3 lines expected per the driver spec:
    #   users@console/console
    #   same
    #   cycle detected
    assert len(lines) == 3, f"expected 3 lines, got: {lines}"
    assert lines[0] == "users@console/console", \
        f"resolve+describe: {lines[0]!r}"
    assert lines[1] == "same", f"singleton check: {lines[1]!r}"
    assert lines[2] == "cycle detected", \
        f"cycle detection: {lines[2]!r}"
