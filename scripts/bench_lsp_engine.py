"""Latency benchmark — LSP engine vs ruff-only baseline.

Measures the per-edit cost of two paths the PostToolUse hook can
take after a Claude Code edit:

1. **ruff-only**: ``subprocess.run([ruff, "check", file])``. What
   the current hook does today. New process per edit.
2. **LSP engine**: ``did_open`` then ``diagnostics`` through the
   per-project daemon over UNIX socket. One spawn per project,
   reused across all edits.

The fair comparison: both paths read a file and return a list of
diagnostics. Pyright (via the engine) gives more precise type info
than ruff; ruff gives faster per-edit feedback. The benchmark
quantifies the second axis so we know when to recommend each.

Reports p50 / p90 / p99 over ``--iterations`` (default 50). First
run discarded as warm-up.

Usage::

    python scripts/bench_lsp_engine.py
    python scripts/bench_lsp_engine.py --iterations 100 --json
    python scripts/bench_lsp_engine.py --skip-engine        # ruff only
    python scripts/bench_lsp_engine.py --skip-ruff          # engine only
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# This import has to follow the sys.path mutation above so a checkout
# clone runs without `pip install -e .`. The E402 lint is intentional.
from claude_hooks.lsp_engine import (  # noqa: E402
    Daemon,
    LspEngineClient,
    LspServerSpec,
    socket_path_for,
)


_SAMPLE_PYTHON = '''\
"""A representative Python file for benchmarking. ~50 lines,
multiple imports, a class, a couple of functions, nothing heavy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Item:
    name: str
    quantity: int
    metadata: Optional[dict] = None

    def total(self, unit_price: float) -> float:
        return self.quantity * unit_price


def load_items(path: Path) -> list[Item]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Item(name=row["name"],
             quantity=int(row["quantity"]),
             metadata=row.get("metadata"))
        for row in raw
    ]


def summarize(items: list[Item]) -> dict[str, float]:
    return {
        "count": len(items),
        "total_quantity": sum(i.quantity for i in items),
    }


if __name__ == "__main__":
    items = load_items(Path(os.environ.get("ITEMS", "items.json")))
    print(summarize(items))
'''


def _percentiles(values: list[float], pct: list[int]) -> dict[int, float]:
    if not values:
        return {p: 0.0 for p in pct}
    s = sorted(values)
    out = {}
    n = len(s)
    for p in pct:
        # Nearest-rank percentile.
        k = max(1, int(round(p / 100.0 * n))) - 1
        out[p] = s[min(k, n - 1)]
    return out


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    pcs = _percentiles(values, [50, 90, 99])
    return {
        "n": len(values),
        "min_ms": min(values) * 1000,
        "p50_ms": pcs[50] * 1000,
        "p90_ms": pcs[90] * 1000,
        "p99_ms": pcs[99] * 1000,
        "max_ms": max(values) * 1000,
        "mean_ms": statistics.fmean(values) * 1000,
    }


def bench_ruff(target: Path, iterations: int) -> Optional[dict]:
    ruff = shutil.which("ruff")
    if ruff is None:
        return None
    times: list[float] = []
    # Discard first run as warm-up (filesystem cache, etc).
    for i in range(iterations + 1):
        t0 = time.monotonic()
        subprocess.run(
            [ruff, "check", "--quiet", "--no-cache", "--output-format=concise", str(target)],
            capture_output=True,
            check=False,
        )
        elapsed = time.monotonic() - t0
        if i > 0:
            times.append(elapsed)
    return _stats(times)


def bench_engine(
    project: Path,
    target: Path,
    iterations: int,
    *,
    state_base: Path,
) -> Optional[dict]:
    """Two measurements per iteration:

    - ``ipc_only``: did_change → return. Pure UNIX-socket round-trip
      cost; pyright still analyses in the background but we don't
      wait for it. This is the "I just sent the LSP an update" cost.
    - ``full_round_trip``: did_change → wait for diagnostics. The
      "I have answers" cost. Dominated by pyright's analysis time
      on real Python files.

    The IPC-only number is what the locked-plan latency targets
    (5 ms p50, 15 ms p99) actually measure. The full-round-trip
    number is what users feel.
    """
    pyright = shutil.which("pyright-langserver")
    if pyright is None:
        return None
    spec = LspServerSpec(
        extensions=("py", "pyi"),
        command=(pyright, "--stdio"),
    )
    daemon = Daemon(
        project_root=project,
        servers=[spec],
        state_base=state_base,
        startup_timeout=15.0,
        request_timeout=10.0,
    )
    daemon.start()
    sock = socket_path_for(project, base=state_base)
    ipc_only_times: list[float] = []
    full_times: list[float] = []
    try:
        with LspEngineClient(sock, "bench") as c:
            content = target.read_text(encoding="utf-8")
            # Warm up: open + first diagnostics. Pyright cold-start
            # dominates this and would skew p50.
            c.did_open(target, content)
            c.diagnostics(target, lock_timeout_ms=200, diag_timeout_s=15.0)
            for i in range(iterations):
                mutated_ipc = content + f"\n# ipc-only iter {i}\n"
                t0 = time.monotonic()
                c.did_change(target, mutated_ipc)
                ipc_only_times.append(time.monotonic() - t0)
                # Drain pyright's response for this version so it
                # doesn't queue up behind the next iteration.
                c.diagnostics(target, lock_timeout_ms=200, diag_timeout_s=15.0)

                mutated_full = content + f"\n# full iter {i}\n"
                t0 = time.monotonic()
                c.did_change(target, mutated_full)
                c.diagnostics(target, lock_timeout_ms=200, diag_timeout_s=15.0)
                full_times.append(time.monotonic() - t0)
    finally:
        daemon.stop()
    return {
        "ipc_only": _stats(ipc_only_times),
        "full_round_trip": _stats(full_times),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--iterations", type=int, default=50)
    p.add_argument("--skip-engine", action="store_true")
    p.add_argument("--skip-ruff", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        target = project / "sample.py"
        target.write_text(_SAMPLE_PYTHON, encoding="utf-8")
        state_base = Path(tmp) / "state"

        results: dict = {"iterations": args.iterations, "target": str(target)}

        if not args.skip_ruff:
            results["ruff"] = bench_ruff(target, args.iterations)
        if not args.skip_engine:
            results["engine"] = bench_engine(
                project, target, args.iterations, state_base=state_base,
            )

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"Iterations: {args.iterations} (warm-up discarded)")
    print(f"Target file: {target.name}")
    print()

    def _print_block(label: str, block: Optional[dict]) -> None:
        if block is None:
            print(f"{label}: SKIPPED (binary not found on PATH)")
            return
        if block.get("n", 0) == 0:
            print(f"{label}: no samples")
            return
        print(f"{label}:")
        for k in ("min_ms", "p50_ms", "p90_ms", "p99_ms", "max_ms", "mean_ms"):
            print(f"  {k:>8}: {block[k]:>8.2f} ms")

    _print_block("ruff (subprocess per edit)", results.get("ruff"))
    print()
    engine = results.get("engine")
    if engine is None:
        print("engine: SKIPPED (pyright-langserver not on PATH)")
    else:
        _print_block("engine IPC-only (did_change)", engine.get("ipc_only"))
        print()
        _print_block(
            "engine full round-trip (did_change + diagnostics)",
            engine.get("full_round_trip"),
        )
        print()
        ipc = engine.get("ipc_only") or {}
        full = engine.get("full_round_trip") or {}
        if ipc.get("p50_ms") and full.get("p50_ms"):
            ipc_share = ipc["p50_ms"] / full["p50_ms"] * 100
            print(
                f"IPC overhead = {ipc_share:.1f}% of total; "
                f"the rest is the LSP's analysis time."
            )

    ruff_p50 = (results.get("ruff") or {}).get("p50_ms")
    eng_full_p50 = ((results.get("engine") or {}).get("full_round_trip") or {}).get("p50_ms")
    if ruff_p50 is not None and eng_full_p50 is not None and eng_full_p50 > 0:
        print(
            f"engine full / ruff p50 ratio: {eng_full_p50 / ruff_p50:.1f}x  "
            f"(ruff lints, pyright type-checks — different work)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
