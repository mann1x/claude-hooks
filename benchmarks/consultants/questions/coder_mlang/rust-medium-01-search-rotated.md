# rust-medium-01-search-rotated

Given a sorted-then-rotated array of distinct `i64` values and a
target, find the **index** of the target in `O(log N)` time.

## I/O contract

Stdin:

```
<n>
<x_0> <x_1> ... <x_{n-1}>
<target>
```

The array `x` was originally sorted ascending, then rotated
around some unknown pivot (so it consists of two ascending
subsequences joined back-to-back). The pivot may be 0 (no
rotation) or N (full rotation = original sorted).

Stdout: a single integer on its own line — the index of the
target if present, or `-1` if absent.

## Constraints

- `0 <= n <= 10^5`
- All elements are distinct
- Must run in `O(log N)`. A linear scan times out on the
  100_000-element adversarial test.
- Single file `solution.rs`. Compiled via
  `rustc -O -o sol solution.rs`. Stdlib only.

## Examples

```
$ echo "7\n4 5 6 7 0 1 2\n0" | ./sol
4

$ echo "7\n4 5 6 7 0 1 2\n3" | ./sol
-1

$ echo "1\n1\n0" | ./sol
-1

$ echo "0\n\n5" | ./sol
-1
```

## Adversarial cases

- Empty array → -1 regardless of target
- Single-element array, target matches → 0
- Single-element array, target misses → -1
- Target equals the array's pivot point (smallest element)
- Target equals the largest element (right before the pivot)
- Array with no rotation (pivot = 0)
- 100_000-element array with target near the rotation seam
