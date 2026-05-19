"""Oracle for rust-hard-01-iter-window-pairs."""

import os
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
SOURCE = SANDBOX / "solution.rs"


def _run(stdin_input: str) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="rust", source=SOURCE,
            stdin_input=stdin_input, timeout_s=10,
        )
    except CompileError as e:
        raise AssertionError(f"rustc rejected solution.rs:\n{e.stderr}") from None


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file(), (
        f"missing solution.rs; sandbox: "
        f"{[p.name for p in SANDBOX.iterdir()]}"
    )


@pytest.mark.constraint
def test_uses_custom_iterator_adapter():
    src = SOURCE.read_text()
    assert "WindowPairs" in src, (
        "WindowPairs struct missing — the spec requires a custom "
        "iterator adapter named WindowPairs"
    )
    assert "WindowPairsExt" in src, (
        "WindowPairsExt trait missing — the spec requires a chainable "
        "extension trait named WindowPairsExt"
    )


def test_basic_four_elements():
    rc, out, err = _run("10 20 30 40\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "10 20\n20 30\n30 40", f"got: {out!r}"


def test_two_elements():
    rc, out, err = _run("5 7\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "5 7"


def test_single_element_emits_nothing():
    rc, out, err = _run("42\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "", f"got: {out!r}"


def test_empty_input_emits_nothing():
    rc, out, err = _run("\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "", f"got: {out!r}"


def test_negative_values():
    rc, out, err = _run("-1 -2 -3\n")
    assert rc == 0, f"runtime: {err}"
    assert out.strip() == "-1 -2\n-2 -3"


@pytest.mark.constraint
def test_implements_iterator_trait():
    src = SOURCE.read_text()
    assert "impl" in src and "Iterator for WindowPairs" in src, (
        "Iterator trait impl missing for WindowPairs"
    )
