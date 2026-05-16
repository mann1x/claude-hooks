---
id: rust-hard-01-iter-window-pairs
tier: hard
source: rust-idiom
task: "Build a custom Iterator adapter WindowPairs<I> + extension trait WindowPairsExt; emit consecutive pairs from stdin's int sequence."
sandbox_path: solution.rs
oracle: rust-hard-01-iter-window-pairs-oracle.py
---

# rust-hard-01-iter-window-pairs

Write a Rust program that reads a sequence of integers from
stdin and prints **consecutive pairs** to stdout.

## Input format

Stdin has one line with whitespace-separated `i64` values
(possibly empty).

## Output format

For input `x_1 x_2 x_3 … x_n` (`n >= 2`), output `n - 1` lines:

```
x_1 x_2
x_2 x_3
...
x_{n-1} x_n
```

For `n < 2`, output nothing (empty stdout, return 0).

Example:

```
$ ./sol <<< "10 20 30 40"
10 20
20 30
30 40
```

## Required structure

Implement the pair-yielding logic as a **custom iterator
adapter**, not an indexed for-loop. The implementation must
include:

- A struct named `WindowPairs<I: Iterator>` (oracle greps for it).
- An `impl<I: Iterator> Iterator for WindowPairs<I>`
  implementation where `Self::Item = (I::Item, I::Item)`.
- An extension trait `WindowPairsExt` (oracle greps for it) that
  adds a `.window_pairs()` method to any `Iterator` whose `Item`
  is `Copy`.

Wire these into `main` so the program reads `Vec<i64>` from
stdin, calls `.iter().copied().window_pairs()`, and prints each
pair.

## Constraints

- Single file `solution.rs`. Compiled via
  `rustc -O -o sol solution.rs`.
- Stdlib only.
- A solution that produces correct stdout via index loops without
  defining `WindowPairs` + `WindowPairsExt` fails the oracle's
  structure check.
