"""Oracle for python-medium-01-flatten-nested.

Imports ``solution.py`` from the sandbox dir (set via the
``CODER_SANDBOX`` env var) and exercises ``solution.flatten``.
Pytest convention: each ``test_*`` function asserts one behavior.
"""

import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

import solution  # noqa: E402  — import depends on sys.path edit above


def test_flatten_callable():
    assert callable(getattr(solution, "flatten", None)), \
        "solution.flatten must be a callable"


def test_empty_input():
    assert solution.flatten([]) == []


def test_flat_list():
    assert solution.flatten([1, 2, 3]) == [1, 2, 3]


def test_one_level_nesting():
    assert solution.flatten([1, [2, 3], 4]) == [1, 2, 3, 4]


def test_deep_nesting():
    assert solution.flatten([1, [2, [3, [4, [5]]]]]) == [1, 2, 3, 4, 5]


def test_mixed_iterables_tuple_and_list():
    assert solution.flatten([(1, 2), [3, (4, 5)]]) == [1, 2, 3, 4, 5]


def test_generator_input():
    gen = ([x * 2] for x in range(3))
    assert solution.flatten(gen) == [0, 2, 4]


def test_preserves_order():
    src = [[5, 4, 3], [2, [1, 0]]]
    assert solution.flatten(src) == [5, 4, 3, 2, 1, 0]


def test_strings_are_atomic_not_iterable_of_chars():
    # Strings are iterable in Python but the spec says they must
    # be rejected as non-integer leaves. ``"ab"`` is not an int,
    # so flatten must raise TypeError.
    import pytest
    with pytest.raises(TypeError):
        solution.flatten([1, "ab", 2])


def test_rejects_float_leaves():
    import pytest
    with pytest.raises(TypeError):
        solution.flatten([1, 2.5, 3])
