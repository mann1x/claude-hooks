"""Oracle for python-hard-01-lru-cache."""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

import solution  # noqa: E402


def test_class_present():
    assert hasattr(solution, "LRUCache"), "solution.LRUCache missing"


def test_zero_capacity_rejected():
    import pytest
    with pytest.raises(ValueError):
        solution.LRUCache(0)
    with pytest.raises(ValueError):
        solution.LRUCache(-1)


def test_basic_put_get():
    c = solution.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1
    assert c.get("b") == 2


def test_missing_raises_keyerror():
    import pytest
    c = solution.LRUCache(2)
    with pytest.raises(KeyError):
        c.get("nope")


def test_eviction_at_capacity():
    c = solution.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)  # should evict "a"
    assert "a" not in c
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_marks_as_used():
    c = solution.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.get("a")           # promotes a; b becomes LRU
    c.put("c", 3)        # should evict b, not a
    assert "a" in c
    assert "b" not in c
    assert c.get("c") == 3


def test_put_existing_updates_and_promotes():
    c = solution.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 99)       # update a + promote
    c.put("c", 3)        # should evict b, not a
    assert c.get("a") == 99
    assert "b" not in c


def test_contains_does_not_promote():
    c = solution.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    # Probe `a` with __contains__ (must NOT count as use).
    assert "a" in c
    c.put("c", 3)        # should evict a (LRU since contains didn't promote)
    assert "a" not in c
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_len_tracks_size():
    c = solution.LRUCache(3)
    assert len(c) == 0
    c.put("a", 1); assert len(c) == 1
    c.put("b", 2); assert len(c) == 2
    c.put("a", 99); assert len(c) == 2  # update doesn't grow
    c.put("c", 3); assert len(c) == 3
    c.put("d", 4); assert len(c) == 3   # eviction keeps len pinned


def test_no_ordereddict_or_functools_lru():
    src = (SANDBOX / "solution.py").read_text()
    # Permit comments mentioning the names but not imports.
    import re
    bad = re.search(
        r"^\s*(from\s+collections\s+import\s+OrderedDict|"
        r"from\s+functools\s+import\s+lru_cache)\b",
        src, re.MULTILINE,
    )
    assert bad is None, (
        f"shortcut import detected: {bad.group(0)!r}. "
        "The point is to implement the LRU mechanism."
    )
