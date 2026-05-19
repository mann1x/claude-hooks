---
id: python-hard-01-lru-cache
tier: hard
source: leetcode-146
task: 'Fixed-capacity LRU cache class with O(1) get/put. __contains__ must NOT promote.'
sandbox_path: solution.py
oracle: python-hard-01-lru-cache-oracle.py
---

# python-hard-01-lru-cache

Implement a fixed-capacity **LRU (Least Recently Used) cache**
class with O(1) average-case `get` and `put`.

## API

```python
class LRUCache:
    def __init__(self, capacity: int): ...
    def get(self, key) -> Any:
        """Return the value or raise KeyError if absent.
        Marks key as most-recently-used on hit."""
    def put(self, key, value) -> None:
        """Insert / update. If the cache is full, evict the
        least-recently-used entry. Both get and put count as a
        'use' for the touched key."""
    def __len__(self) -> int: ...
    def __contains__(self, key) -> bool:
        """O(1) presence check; must NOT count as a 'use'."""
```

## Constraints

- `capacity >= 1` (constructor raises `ValueError` on
  `capacity <= 0`).
- Average-case O(1) for `get` / `put` / `__contains__` /
  `__len__`. Use a doubly-linked list keyed by hashable key.
- `__contains__` must NOT mark the key as used (this is what
  separates a real LRU from a naive implementation that uses
  `dict.get(...) is not None`).
- A `put` on an existing key updates the value AND marks it as
  most-recently-used; no eviction in that path.
- Capacity is strict: once `len(cache) == capacity`, the next
  `put(new_key, ...)` evicts the LRU entry before insertion.

## Constraints (cont'd)

- Single file `solution.py` exposing `LRUCache` at module top.
- No imports outside the stdlib.
- No `functools.lru_cache`, no `collections.OrderedDict` — the
  point is to demonstrate understanding of the underlying data
  structure. The oracle's `test_no_ordereddict_used` checks the
  source for these patterns.

