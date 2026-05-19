"""Oracle for go-hard-01-shortest-path-k-stops."""

import heapq
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
SOURCE = SANDBOX / "solution.go"


def _run(stdin_input: str, timeout_s: int = 10) -> tuple[int, str, str]:
    try:
        return compile_and_run(
            lang="go", source=SOURCE,
            stdin_input=stdin_input, timeout_s=timeout_s,
        )
    except CompileError as e:
        raise AssertionError(f"go build rejected solution.go:\n{e.stderr}") from None


def _query(n, edges, src, dst, K):
    lines = [f"{n} {len(edges)} {src} {dst} {K}"]
    for u, v, w in edges:
        lines.append(f"{u} {v} {w}")
    body = "\n".join(lines) + "\n"
    rc, out, err = _run(body)
    assert rc == 0, f"runtime: {err}"
    return int(out.strip())


def _reference(n, edges, src, dst, K):
    # Brute reference: state-augmented Dijkstra.
    adj = [[] for _ in range(n)]
    for u, v, w in edges:
        adj[u].append((v, w))
    INF = float("inf")
    best = [[INF] * (K + 2) for _ in range(n)]
    best[src][0] = 0
    pq = [(0, src, 0)]
    while pq:
        c, u, stops = heapq.heappop(pq)
        if u == dst:
            return c
        if stops > K:
            continue
        for v, w in adj[u]:
            nc = c + w
            ns = stops + 1
            if ns <= K + 1 and nc < best[v][ns]:
                best[v][ns] = nc
                heapq.heappush(pq, (nc, v, ns))
    return -1


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file()


def test_classic_with_stops_allowed():
    edges = [(0, 1, 100), (1, 2, 100), (0, 2, 500)]
    assert _query(3, edges, 0, 2, 1) == 200


def test_classic_no_stops_allowed():
    edges = [(0, 1, 100), (1, 2, 100), (0, 2, 500)]
    assert _query(3, edges, 0, 2, 0) == 500


def test_four_node_with_constraint():
    edges = [(0, 1, 100), (0, 2, 500), (1, 2, 100), (2, 3, 100)]
    # K=1 (1 stop): 0→2→3 (1 stop) costs 600, 0→1→2→3 (2 stops) excluded.
    assert _query(4, edges, 0, 3, 1) == 600


def test_no_path_returns_minus_one():
    edges = [(0, 1, 100), (2, 3, 100)]
    assert _query(4, edges, 0, 3, 5) == -1


def test_src_equals_dst():
    edges = [(0, 1, 100), (1, 2, 100)]
    assert _query(3, edges, 0, 0, 0) == 0


def test_parallel_edges_pick_cheapest():
    # Two parallel edges 0→1 with costs 50 and 200; pick 50.
    edges = [(0, 1, 200), (0, 1, 50), (1, 2, 100)]
    assert _query(3, edges, 0, 2, 1) == 150


def test_cycle_doesnt_loop_forever():
    # Cycle: 0 -> 1 -> 0. dst = 2 is unreachable; must return -1.
    edges = [(0, 1, 10), (1, 0, 10), (1, 2, 100)]
    assert _query(3, edges, 0, 2, 0) == -1   # 0 stops → must be direct
    assert _query(3, edges, 0, 2, 1) == 110  # 0→1→2 with 1 stop


def test_random_small_graphs():
    rng = random.Random(31)
    for _ in range(5):
        n = rng.randint(4, 8)
        m = rng.randint(n, n * 2)
        edges = []
        for _ in range(m):
            u = rng.randint(0, n - 1)
            v = rng.randint(0, n - 1)
            w = rng.randint(1, 50)
            edges.append((u, v, w))
        src = 0
        dst = n - 1
        K = rng.randint(0, n - 1)
        expected = _reference(n, edges, src, dst, K)
        got = _query(n, edges, src, dst, K)
        assert got == expected, (
            f"n={n} edges={edges} src={src} dst={dst} K={K} "
            f"got={got} expected={expected}"
        )
