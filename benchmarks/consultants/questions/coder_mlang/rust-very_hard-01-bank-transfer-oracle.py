"""Oracle for rust-very_hard-01-bank-transfer."""

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
SOURCE = SANDBOX / "solution.rs"


def _run(stdin_input: str, timeout_s: int = 15) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="rust", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"rustc rejected solution.rs:\n{e.stderr}") from None


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


@pytest.mark.constraint
def test_uses_mutex_and_arc():
    src = SOURCE.read_text()
    assert "Mutex" in src, "Mutex missing"
    assert "Arc" in src, "Arc missing"


def test_trivial_two_account_transfer():
    # 2 accounts, 1 thread, 1 txn. Balance moves 30 from acct0 to acct1.
    rc, out, err = _run("2 1 1\n100 50\n0 1 30\n")
    assert rc == 0, f"runtime: rc={rc}\n{err}"
    lines = [int(x) for x in out.strip().split()]
    assert lines == [70, 80], f"got: {lines}"


def test_overdraft_is_skipped():
    rc, out, err = _run("2 1 1\n10 0\n0 1 999\n")
    assert rc == 0, f"runtime: {err}"
    lines = [int(x) for x in out.strip().split()]
    assert lines == [10, 0], f"got: {lines}"


def test_conservation_under_concurrency():
    # 4 accounts, 4 threads, 200 random transfers. Total balance
    # must remain constant; no deadlock; rc must be 0 (not 124).
    rng = random.Random(42)
    n_accounts, n_threads, n_txns = 4, 4, 200
    initial = [1000, 1000, 1000, 1000]
    total_in = sum(initial)
    lines = [
        f"{n_accounts} {n_threads} {n_txns}",
        " ".join(str(x) for x in initial),
    ]
    for _ in range(n_txns):
        a = rng.randint(0, 3); b = rng.randint(0, 3)
        amt = rng.randint(1, 100)
        lines.append(f"{a} {b} {amt}")
    stdin_input = "\n".join(lines) + "\n"
    rc, out, err = _run(stdin_input, timeout_s=10)
    assert rc != 124, (
        "TIMEOUT — solution.rs deadlocked. Lock-acquisition "
        "order is not consistent. Hint: lock in account-id order."
    )
    assert rc == 0, f"runtime: rc={rc}\n{err}"
    finals = [int(x) for x in out.strip().split()]
    assert len(finals) == n_accounts
    assert sum(finals) == total_in, (
        f"conservation violated: sum={sum(finals)} expected={total_in}"
    )
    for b in finals:
        assert b >= 0, f"negative balance: {finals}"


def test_self_transfer_is_noop():
    rc, out, err = _run("2 1 1\n100 50\n0 0 30\n")
    assert rc == 0, f"runtime: {err}"
    lines = [int(x) for x in out.strip().split()]
    assert lines == [100, 50], f"got: {lines}"


def test_zero_txns():
    rc, out, err = _run("3 2 0\n10 20 30\n")
    assert rc == 0, f"runtime: {err}"
    lines = [int(x) for x in out.strip().split()]
    assert lines == [10, 20, 30]
