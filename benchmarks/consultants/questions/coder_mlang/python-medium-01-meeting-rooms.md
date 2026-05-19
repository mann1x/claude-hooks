---
id: python-medium-01-meeting-rooms
tier: medium
source: leetcode-253
task: 'Given N (start,end) intervals, return the minimum number of conference rooms required (half-open). O(N log N).'
sandbox_path: solution.py
oracle: python-medium-01-meeting-rooms-oracle.py
---

# python-medium-01-meeting-rooms

Given a list of meeting time intervals where `intervals[i] =
(start_i, end_i)`, return the **minimum number of conference
rooms** required to schedule all meetings.

Intervals are half-open `[start, end)` — a meeting that ends at
time `t` does **not** conflict with a meeting that starts at
time `t`.

## API

```python
def min_meeting_rooms(intervals: list[tuple[int, int]]) -> int:
    ...
```

## Constraints

- `0 <= len(intervals) <= 10_000`
- `0 <= start < end <= 10**9`
- Must run in `O(N log N)` worst case. A naive `O(N^2)` overlap
  scan times out on the adversarial test (5000 intervals all
  pairwise-overlapping by 1).
- Single file `solution.py`. Stdlib only.

## Examples

```python
min_meeting_rooms([(0, 30), (5, 10), (15, 20)])  # → 2
min_meeting_rooms([(7, 10), (2, 4)])             # → 1
min_meeting_rooms([])                            # → 0
min_meeting_rooms([(1, 5), (5, 10)])             # → 1  (half-open: no conflict)
min_meeting_rooms([(1, 5), (4, 10), (3, 8)])     # → 3  (all three overlap at t=4)
```

## Adversarial cases the oracle exercises

- Empty input → 0
- Single interval → 1
- Two intervals touching at a boundary → 1 (half-open semantics)
- 5000 intervals all overlapping at a single instant → 5000
- Mixed start/end ordering across the list (the input is NOT
  pre-sorted)
- Two intervals where one fully contains the other
