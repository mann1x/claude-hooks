# python-medium-01-flatten-nested

Write a function `flatten(items)` that takes an arbitrarily-nested
iterable of integers (lists, tuples, generators — any iterable)
and returns a **list** of integers in the original left-to-right
order.

## Requirements

- Accept arbitrary nesting depth.
- Accept any iterable (`list`, `tuple`, generator, `range`, …) at
  every level.
- Preserve order: `flatten([1, [2, 3], [[4], [5, [6]]]])` →
  `[1, 2, 3, 4, 5, 6]`.
- Treat **strings as atomic**, NOT as iterables of characters —
  `flatten([1, "ab", 2])` raises `TypeError` because `"ab"` is
  not an integer and not an iterable of integers.
- Reject any other non-integer leaf with `TypeError`.
- The function must work on `flatten([])` returning `[]`.

## Constraints

- Pure function, no side effects.
- Single file `solution.py` exposing `flatten` at module top.
- No imports outside the stdlib.
- O(N) time in the total number of leaves.

## Hint

`isinstance(x, str)` distinguishes strings from other iterables
even though strings ARE iterable. Use `collections.abc.Iterable`
to detect non-string iterables.
