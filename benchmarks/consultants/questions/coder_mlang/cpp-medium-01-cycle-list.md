---
id: cpp-medium-01-cycle-list
tier: medium
source: leetcode-142
task: "Build a singly-linked list from stdin; detect cycle and return the entry-node value (or 'none'). Use Floyd's tortoise+hare (O(N) time, O(1) space)."
sandbox_path: solution.cpp
oracle: cpp-medium-01-cycle-list-oracle.py
---

# cpp-medium-01-cycle-list

Build a singly-linked list from stdin, then determine whether it
contains a cycle. If yes, return the **value at the node where
the cycle begins**. If no, return a sentinel.

## I/O contract

Stdin:

```
<n>
<v_0> <v_1> ... <v_{n-1}>
<cycle_to>
```

- `n` is the count of nodes (`0 <= n <= 10_000`).
- Values are `int32`, possibly duplicated.
- `cycle_to`: the index into the value list where the tail's
  `next` should point, OR `-1` to leave the list acyclic.

Stdout:

- If `cycle_to == -1`: print `none`.
- Otherwise: print the value at the cycle-entry node (the node
  at index `cycle_to`).

The cycle-entry node is **not** necessarily where the tortoise
and hare meet — it's the node where the cycle begins (i.e. the
node at index `cycle_to` in the original list).

## Constraints

- Must use Floyd's "tortoise and hare" algorithm — the program
  must run in `O(N)` time and `O(1)` extra space (not counting
  the input list itself). Hash-set approaches are `O(N)` space
  and **fail** the structure check.
- Single file `solution.cpp`. Compiled via
  `g++ -O2 -std=c++17 -Wall -o sol solution.cpp -lpthread`.
- The oracle greps the source for two distinct pointer-walk
  variables (typical names: `slow` + `fast`, or `tortoise` +
  `hare`); a solution using `std::unordered_set` / `std::set`
  for cycle detection fails the structure check.

## Examples

```
$ echo "5\n1 2 3 4 5\n2" | ./sol
3
```

(List `1→2→3→4→5→3` cycles back to index 2 which holds value 3.)

```
$ echo "5\n1 2 3 4 5\n-1" | ./sol
none
```

```
$ echo "3\n7 7 7\n0" | ./sol
7
```

```
$ echo "0\n\n-1" | ./sol
none
```

## Adversarial cases

- Empty list → `none`
- Single node, no cycle → `none`
- Single node pointing to itself → its own value
- Long list with cycle entering at the very last node
- Cycle covers entire list (cycle_to = 0)
- Cycle covers only the final node
