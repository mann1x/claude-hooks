# rust-medium-01-rotate-vec

Write a standalone Rust program that reads a sequence of
whitespace-separated `i64` values plus a rotation count `k` from
stdin and prints the **left-rotated** sequence to stdout.

## Input format

Stdin has exactly two lines:

```
<n> <k>
<x_1> <x_2> ... <x_n>
```

- `n` is the count of values (0 ≤ n ≤ 10_000).
- `k` is the rotation count (may be 0, may be ≥ n; only `k % n`
  is significant when n > 0).

## Output format

A single line: the rotated sequence, space-separated, **no
trailing space, with a trailing newline**.

Example:

```
$ ./sol <<< $'5 2\n1 2 3 4 5'
3 4 5 1 2
```

When `n == 0`, output is just an empty line.

## Requirements

- Single source file: `solution.rs`. Compiled via
  `rustc -O -o sol solution.rs`.
- `main()` is the entry point; the program must read stdin and
  write stdout exactly per the format above.
- **Rotate in place** using slice operations — do NOT allocate a
  second Vec for the result. Use `Vec::rotate_left` or equivalent
  slice-based logic. (The oracle doesn't enforce this with
  inspection; it's a style hint that the judge rubric considers.)
- Handle `k > n` by taking `k % n`.
- No external crates — stdlib only.

## Hint

`io::stdin().read_line(...)` returns the line including the
newline. Split on whitespace, parse to `i64`. `Vec::rotate_left`
is the idiomatic single-call rotation.
