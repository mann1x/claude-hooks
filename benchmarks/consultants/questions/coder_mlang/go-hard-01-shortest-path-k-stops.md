---
id: go-hard-01-shortest-path-k-stops
tier: hard
source: leetcode-787
task: 'Cheapest src->dst with at most K intermediate stops. Stdin: n m src dst K, edges. Use Bellman-Ford or state-augmented Dijkstra.'
sandbox_path: solution.go
oracle: go-hard-01-shortest-path-k-stops-oracle.py
---

# go-hard-01-shortest-path-k-stops

Given a weighted directed graph of flights, find the **cheapest
price** from `src` to `dst` using at most `K` intermediate stops.

A "stop" is a node visited between `src` and `dst` (not counting
`src` or `dst` themselves). So `K = 0` means you can only fly
directly from `src` to `dst`; `K = 1` means you can fly with one
layover; etc.

## I/O contract

Stdin:

```
<n_nodes> <n_edges> <src> <dst> <K>
<from_0> <to_0> <cost_0>
<from_1> <to_1> <cost_1>
...
<from_{m-1}> <to_{m-1}> <cost_{m-1}>
```

Stdout: the minimum total cost on a single line, or `-1` if no
route within `K` stops exists.

## Constraints

- `1 <= n_nodes <= 100`
- `0 <= n_edges <= 10_000`
- `0 <= K <= n_nodes - 1`
- `0 <= cost <= 10^4`
- Multiple edges between the same pair are allowed; the program
  must select the cheapest path overall.
- Single file `solution.go`. `go build -o sol solution.go`.
  Stdlib only.

## Algorithm requirements

A standard Dijkstra `(node, cost)`-only doesn't work here — you
have to track state `(node, stops_used)` because a more
expensive path that uses fewer stops may dominate a cheaper one
that's exhausted its budget. The two canonical approaches:

- **Bellman-Ford** variant: relax `K + 1` times, one per
  allowed edge in the path.
- **State-augmented Dijkstra**: priority queue over
  `(cost, node, stops_used)`; pop the cheapest unexplored state.

Either passes the oracle. The oracle does NOT inspect the
algorithm — it only checks correctness against adversarial
inputs that BREAK plain Dijkstra.

## Examples

```
$ echo "3 3 0 2 1\n0 1 100\n1 2 100\n0 2 500" | ./sol
200
```

(Direct 0→2 costs 500. 0→1→2 costs 200 and uses 1 stop. K=1 allows it.)

```
$ echo "3 3 0 2 0\n0 1 100\n1 2 100\n0 2 500" | ./sol
500
```

(Same graph, K=0: no stops, so only direct 0→2 is legal.)

```
$ echo "4 4 0 3 1\n0 1 100\n0 2 500\n1 2 100\n2 3 100" | ./sol
700
```

(K=1, only one stop allowed. Best is 0→2→3 = 600. Wait, let me
recheck: 0→2→3 uses 1 stop (node 2), total cost 500+100=600.
0→1→2→3 uses 2 stops which exceeds K=1. So answer is 600.)
Corrected:

```
$ echo "4 4 0 3 1\n0 1 100\n0 2 500\n1 2 100\n2 3 100" | ./sol
600
```

## Adversarial cases

- A path with fewer stops costs more but is the only one within
  budget.
- Multiple edges between the same `(from, to)` pair — pick the
  cheapest.
- Cycles in the graph; the K-stops cap prevents looping forever.
- `src == dst` → 0 (regardless of K).
- No path exists within K stops → -1.
- A path that uses all K stops AND a path that uses fewer; the
  cheaper of the two wins.
