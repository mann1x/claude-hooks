"""Oracle for go-very_hard-01-spsc-queue."""

import os
import re
import sys
from pathlib import Path

_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError, compile_and_run,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "solution.go"


def _run(stdin_input: str, timeout_s: int = 30) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="go", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"go build rejected solution.go:\n{e.stderr}") from None


def _query(capacity, n_items):
    rc, out, err = _run(f"{capacity} {n_items}\n", timeout_s=30)
    assert rc != 124, "TIMEOUT — possible deadlock"
    assert rc == 0, f"runtime: {err}"
    parts = out.strip().split()
    assert len(parts) == 2, f"expected 2 values, got: {parts}"
    return int(parts[0]), int(parts[1])


def test_source_present():
    assert SOURCE.is_file()


def test_no_mutex_shortcut():
    src = SOURCE.read_text()
    forbidden = re.search(
        r"\bsync\.(Mutex|RWMutex)\b",
        src,
    )
    assert forbidden is None, (
        f"sync.Mutex/RWMutex not allowed — the spec requires "
        f"sync/atomic only. Match: {forbidden.group(0)!r}"
    )


def test_uses_atomic_package():
    src = SOURCE.read_text()
    has_atomic = "atomic." in src or "/atomic\"" in src
    assert has_atomic, "sync/atomic package not used"


def test_small_capacity_high_count():
    s, c = _query(4, 100_000)
    expected_sum = 100_000 * 100_001 // 2
    assert c == 100_000, f"lost items: count={c}"
    assert s == expected_sum, f"sum mismatch: got {s}, want {expected_sum}"


def test_capacity_one():
    s, c = _query(1, 1000)
    expected_sum = 1000 * 1001 // 2
    assert c == 1000
    assert s == expected_sum


def test_capacity_power_of_two():
    s, c = _query(64, 50_000)
    expected_sum = 50_000 * 50_001 // 2
    assert c == 50_000
    assert s == expected_sum


def test_capacity_non_power_of_two():
    s, c = _query(7, 20_000)
    expected_sum = 20_000 * 20_001 // 2
    assert c == 20_000
    assert s == expected_sum


def test_zero_items():
    s, c = _query(4, 0)
    assert c == 0
    assert s == 0


def test_high_volume_million():
    # Stress test. If the implementation has a subtle ordering
    # bug, items will be lost under high volume.
    s, c = _query(16, 1_000_000)
    expected_sum = 1_000_000 * 1_000_001 // 2
    assert c == 1_000_000, f"lost items at high volume: {c}"
    assert s == expected_sum, f"sum mismatch at high volume"
