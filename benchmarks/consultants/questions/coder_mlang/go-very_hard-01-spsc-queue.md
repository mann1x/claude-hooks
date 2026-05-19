---
id: go-very_hard-01-spsc-queue
tier: very_hard
source: concurrency
task: 'Lock-free SPSC ring buffer using only sync/atomic (no Mutex). Producer + consumer goroutines must conserve all items.'
sandbox_path: solution.go
oracle: go-very_hard-01-spsc-queue-oracle.py
---

# go-very_hard-01-spsc-queue

## ⚠️ CRITICAL CONSTRAINTS — the oracle greps the source

Your solution **must satisfy these literally**:

1. Define `type SPSCQueue struct { ... }` with the **exact
   API** in the Required API section below: `NewSPSCQueue`,
   `Push`, `Pop`.
2. **No `sync.Mutex`** and no `sync.RWMutex` anywhere — the
   oracle greps for both and rejects the file if either
   appears. Use `sync/atomic` (e.g. `atomic.LoadInt64`,
   `atomic.StoreInt64`, `atomic.AddInt64`).
3. **Output exactly one line**: `<sum> <count>` — two integers,
   space-separated, no prefix, no labels. **NO** `OK:`, **NO**
   `all items conserved`, **NO** verbose summary. Example
   acceptable line: `500000500000 1000000\n`. Anything else
   fails `out.strip().split() != [sum, count]`.
4. The program must **build cleanly** under `go build`.
   Common gotchas: int vs int64 (use `int64` for atomics),
   the "done" signal needs a sync.Once or atomic.Bool that
   the consumer reads atomically, padding to avoid false
   sharing on head/tail.

Implement a **lock-free single-producer single-consumer (SPSC)
ring buffer** in Go using **only `sync/atomic`** — no
`sync.Mutex`, no `sync.RWMutex`, no channels (in the data
structure itself), no `sync.Map`. A single producer goroutine
must be safe to concurrently `Push` while a single consumer
goroutine concurrently `Pop`s.

## Required API

```go
type SPSCQueue struct { /* fields */ }

// NewSPSCQueue creates a queue with the given fixed capacity.
// Capacity is rounded up to the next power of two internally
// (so masking can replace modulo) — or kept as-is, your choice.
func NewSPSCQueue(capacity int) *SPSCQueue

// Push attempts to enqueue v. Returns true on success, false
// if the buffer is full. Non-blocking.
// Only one producer goroutine may call Push at a time.
func (q *SPSCQueue) Push(v int64) bool

// Pop attempts to dequeue. Returns (value, true) on success,
// (0, false) if the buffer is empty. Non-blocking.
// Only one consumer goroutine may call Pop at a time.
func (q *SPSCQueue) Pop() (int64, bool)
```

## I/O contract (driver in `main`)

The program reads from stdin:

```
<capacity> <n_items>
```

Then it:
1. Constructs the queue with the given capacity.
2. Spawns ONE producer goroutine that pushes the integers
   `1, 2, 3, ..., n_items` (in that order). When a push fails
   (buffer full), the producer spins (`runtime.Gosched()`) and
   retries. After all items are pushed, the producer signals
   "done" (any mechanism — atomic bool, channel, sync.Once).
3. Spawns ONE consumer goroutine that pops as fast as it can.
   On `false`, the consumer checks the "done" signal — if set
   AND the queue is empty, it exits. Otherwise it spins +
   retries.
4. The consumer accumulates the **sum of values popped** and
   counts the number of items. After it exits, `main` waits for
   it and prints:

```
<sum_of_popped_values> <count_of_popped_values>
```

(space-separated, single newline).

## Correctness requirements

For `n_items = N`:

- All N values must be popped exactly once (no loss, no
  duplication).
- `count_of_popped_values == N`.
- `sum_of_popped_values == N * (N + 1) / 2`.
- The program must terminate cleanly (no goroutine leaks).
- It must not deadlock (oracle returns rc=124 if it does).
- Implementations using `sync.Mutex` / `sync.RWMutex` fail the
  source-grep check.

## Examples

```
$ echo "4 1000000" | ./sol
500000500000 1000000
```

(Sum of 1..1_000_000 = 500_000_500_000.)

```
$ echo "64 100000" | ./sol
5000050000 100000
```

## Adversarial cases

- Very small capacity (4) with high item count (1_000_000) —
  the producer must spin frequently waiting for slots; the
  consumer must drain efficiently.
- Capacity that is NOT a power of two (e.g. 7). Either round up
  internally or use modulo; both pass.
- High contention: the producer + consumer goroutines run
  concurrently on multi-core. The race detector inside `go
  build` will spot data races at build time — implementations
  that use plain `int` reads/writes for shared head/tail
  positions get rejected by the compiler with `-race` (the
  oracle compiles WITHOUT `-race` for speed; the source grep
  catches Mutex shortcuts).
