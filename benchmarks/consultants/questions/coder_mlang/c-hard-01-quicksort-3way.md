# c-hard-01-quicksort-3way

Implement an in-place **3-way quicksort** (Dutch National Flag
partition) on an array of `int`s.

## I/O contract

Stdin:

```
<n>
<x_0> <x_1> ... <x_{n-1}>
```

`0 ≤ n ≤ 100_000`. Each `x_i` is a signed 32-bit int (allow
negatives + zeros + duplicates).

Stdout: the sorted array, space-separated, with a trailing
newline.

For `n == 0`, output an empty line.

## Required idiom

Implement a function with this signature:

```c
void quicksort3(int *a, int n);
```

It must:

- Sort `a[0..n)` in non-decreasing order **in place**.
- Use **3-way partitioning** (Dutch flag): on each recursive
  call, pick a pivot, partition into three regions `< pivot`,
  `== pivot`, `> pivot`, and recurse on only the outer two.
  This is what makes the algorithm `O(N)` on inputs with many
  duplicates (vs the classical Lomuto/Hoare partition which is
  `O(N log N)` at best, `O(N²)` on duplicate-heavy inputs).
- Choose the pivot via **median-of-three** for robustness on
  partly-sorted inputs.

`main` reads stdin, calls `quicksort3`, prints space-separated.

## Constraints

- Single file `solution.c`. Compiled via
  `gcc -O2 -Wall -o sol solution.c -lm`.
- Stdlib only — `<stdio.h>`, `<stdlib.h>`, `<string.h>` if
  needed for parsing. NO `qsort` (the oracle greps for it).
- Recursive is fine; iterative is fine. Stack depth must be
  acceptable for `n = 100_000` — that's where the 3-way
  partition matters (small partitions don't recurse deep).

