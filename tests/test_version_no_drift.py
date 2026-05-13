"""Pin ``claude_hooks.__version__`` to ``pyproject.toml::[project].version``.

Bug history motivating this guard: between v1.0.3 (when the
``__version__`` constant landed as a hard-coded string) and v1.3.1,
every cut bumped ``pyproject.toml`` but forgot the constant. The
update-check banner emitted by the Stop hook consequently
misreported every install as "current 1.0.3" for months. v1.3.2
made the constant self-resolving (importlib.metadata → pyproject
walk → fallback). This test makes sure the contract holds: the
two sources of truth must always agree, on every release.

If this test fails, **do not bump the test** — fix whichever source
drifted. The right move is almost always to update
``pyproject.toml`` (the authoritative version) and let the
self-resolver pick it up; the only time you'd touch
``claude_hooks/__init__.py`` is to fix the resolver itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _pyproject_version() -> str:
    """Parse ``[project].version`` out of ``pyproject.toml``.

    Hand-rolled to avoid a ``tomllib`` / ``tomli`` import — this test
    must run on the same 3.9+ stdlib floor the package targets.
    """
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project and line.startswith("version"):
            _, _, rhs = line.partition("=")
            return rhs.strip().strip('"').strip("'")
    raise AssertionError("pyproject.toml has no [project].version")


def test_module_version_matches_pyproject():
    import claude_hooks
    expected = _pyproject_version()
    actual = claude_hooks.__version__
    assert actual == expected, (
        f"claude_hooks.__version__ ({actual!r}) drifted from "
        f"pyproject.toml::[project].version ({expected!r}). "
        "The constant is self-resolving as of v1.3.2 — if you see "
        "this, the resolver itself is broken; do not patch the test."
    )


def test_module_version_is_not_the_legacy_static_string():
    """Belt-and-braces: catch a regression where someone re-hardcodes
    the constant to a stale value (which is exactly the bug v1.3.2
    fixed). The current value is whatever pyproject says — but it is
    *not* the legacy 1.0.3 frozen string, unless someone reverts both
    files simultaneously, in which case the prior test catches it."""
    import claude_hooks
    assert claude_hooks.__version__ != "1.0.3" or _pyproject_version() == "1.0.3", (
        "claude_hooks.__version__ is 1.0.3 but pyproject.toml disagrees. "
        "This is the exact pre-v1.3.2 drift bug — the resolver must "
        "follow pyproject, not a baked-in constant."
    )
