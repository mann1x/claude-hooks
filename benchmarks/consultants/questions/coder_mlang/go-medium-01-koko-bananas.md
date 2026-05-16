# go-medium-01-koko-bananas

Koko has `n` piles of bananas, where `pile[i]` is the number of
bananas in pile `i`. She has `H` hours to eat them all. Each
hour she picks a pile and eats up to `K` bananas from it (if
the pile has fewer than `K`, she finishes it and moves on but
**still uses the full hour** — she eats no other pile that hour).

Find the **minimum integer K** such that Koko can finish all
piles within `H` hours.

## I/O contract

Stdin:

```
<n> <H>
<pile_0> <pile_1> ... <pile_{n-1}>
```

Stdout: the minimum eating speed, on a single line.

## Constraints

- `1 <= n <= 10^4`
- `n <= H <= 10^9`
- `1 <= pile[i] <= 10^9`
- Must run in `O((sum of piles or max pile) log)` — i.e. binary
  search on the answer. A linear search on speed from 1 upward
  is `O(max_pile * n)` and TLEs on the adversarial test.
- Single file `solution.go`. `go build -o sol solution.go`.
  Stdlib only.

## Examples

```
$ echo "4 8\n3 6 7 11" | ./sol
4

$ echo "5 5\n30 11 23 4 20" | ./sol
30

$ echo "5 6\n30 11 23 4 20" | ./sol
23
```

## Adversarial cases

- Single pile, `H == 1` → must eat the whole pile in one hour:
  K = pile size.
- Single pile, `H == pile_size`: K = 1.
- Very large pile values (up to `10^9`) — binary search range
  is `[1, max_pile]`, so a linear-scan implementation TLEs.
- `n == H` (Koko has exactly one hour per pile): K = max pile.
- The hours-per-pile calculation must use **ceiling division**:
  `ceil(pile / K)`, NOT integer division.
