# csharp-medium-01-trapped-rainwater

Given `n` non-negative integers representing an elevation map
where the width of each bar is 1, compute how much rainwater can
be **trapped** after rain.

A cell `i` traps `max(0, min(left_max, right_max) - height[i])`
where `left_max` and `right_max` are the maximum bar heights on
the left and right (inclusive) of cell `i`.

## I/O contract

Stdin:

```
<n>
<h_0> <h_1> ... <h_{n-1}>
```

Stdout: total trapped water (a single integer + newline).

## Constraints

- `0 <= n <= 100_000`
- `0 <= h_i <= 10_000`
- Must run in `O(N)` time. The classic `O(N^2)` per-cell scan
  TLEs on the 100_000-cell adversarial test.
- Single file `solution.cs`. Built via `dotnet publish -c
  Release` (oracle generates the csproj).

## Examples

```
$ echo "12\n0 1 0 2 1 0 1 3 2 1 2 1" | ./sol
6

$ echo "6\n4 2 0 3 2 5" | ./sol
9

$ echo "0\n" | ./sol
0

$ echo "1\n7" | ./sol
0

$ echo "3\n3 3 3" | ./sol
0
```

## Adversarial cases

- Empty array → 0
- Monotonically increasing or decreasing → 0 (no trap)
- Flat (all same value) → 0
- Two tall walls with a deep valley between them
- 100_000-cell saw-tooth pattern (alternating high/low) — the
  total trapped should match the analytical answer
- A 100_000-cell input where the C# solution needs to NOT
  allocate a per-cell `int[]` left/right max if a two-pointer
  approach is used (any correct `O(N)` solution passes)
