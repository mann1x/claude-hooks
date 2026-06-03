"""Regression guard: every ``HAVE_LANGGRAPH`` probe must import a concrete
langgraph *leaf* (``from langgraph.<sub> import <name>``), never a bare
``import langgraph`` / ``import langgraph.<pkg>``.

Why this exists
---------------
``pip uninstall langgraph`` removes the package files + dist metadata but
leaves the now-empty ``site-packages/langgraph/{cache,checkpoint,store}``
directories behind. Python 3 resolves those as a PEP-420 *namespace*
package, so a bare ``import langgraph`` (or ``import langgraph.checkpoint``)
*succeeds* against the ghost while every real submodule is gone. A guard
keyed on the bare import then reports the stack as present, so the dependent
tests run and fail at import time instead of skipping.

Observed on pandorum 2026-06-03: the base ``claude-hooks`` env carried such
a ghost (``find_spec('langgraph')`` non-None, ``pip show langgraph`` empty,
``listdir`` == ``['cache', 'checkpoint', 'store']``), which turned ~60
consultants tests from clean skips into failures.

A ``from langgraph.graph import StateGraph`` cannot be satisfied by an empty
namespace dir, so it is the correct probe. This test enforces that contract
across the whole suite so a lazy guard can never reappear.
"""
from __future__ import annotations

import pathlib
import re
import unittest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
# A leaf probe imports a NAME from a real submodule file; an empty namespace
# dir cannot satisfy it. ``import langgraph`` / ``import langgraph.types`` can.
_LEAF_IMPORT = re.compile(r"^\s*from\s+langgraph\.\w+.*\bimport\b", re.M)


def _weak_langgraph_guards() -> list[str]:
    """Return ``file:line`` for every HAVE_LANGGRAPH=True whose enclosing
    ``try:`` block lacks a concrete ``from langgraph.<sub> import`` probe."""
    offenders: list[str] = []
    self_name = pathlib.Path(__file__).name
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        if path.name == self_name:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.strip() != "HAVE_LANGGRAPH = True":
                continue
            j = i
            while j >= 0 and lines[j].strip() != "try:":
                j -= 1
            block = "\n".join(lines[j:i + 1]) if j >= 0 else line
            if not _LEAF_IMPORT.search(block):
                offenders.append(f"{path.name}:{i + 1}")
    return offenders


_BARE_IMPORTORSKIP = re.compile(r"""importorskip\(\s*["']langgraph["']\s*\)""")


def _bare_importorskip_langgraph() -> list[str]:
    """Return ``file:line`` for every ``pytest.importorskip("langgraph")``. The
    bare form is satisfied by an empty ghost namespace dir, so it fails to skip
    and the test then dies on the real submodule import. Use a concrete leaf,
    e.g. ``pytest.importorskip("langgraph.graph")``."""
    offenders: list[str] = []
    self_name = pathlib.Path(__file__).name
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        if path.name == self_name:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if _BARE_IMPORTORSKIP.search(line):
                offenders.append(f"{path.name}:{i + 1}")
    return offenders


class TestLanggraphGuardsAreGhostProof(unittest.TestCase):
    def test_no_guard_probes_a_bare_langgraph_namespace(self):
        offenders = _weak_langgraph_guards()
        self.assertEqual(
            offenders, [],
            "These HAVE_LANGGRAPH guards probe a bare `import langgraph` and "
            "would be fooled by the empty ghost namespace dirs a pip-uninstall "
            "leaves behind. Probe a concrete leaf instead, e.g. "
            "`from langgraph.graph import StateGraph`:\n  " + "\n  ".join(offenders),
        )

    def test_no_bare_importorskip_langgraph(self):
        offenders = _bare_importorskip_langgraph()
        self.assertEqual(
            offenders, [],
            "`pytest.importorskip('langgraph')` is fooled by empty ghost namespace "
            "dirs (the bare package imports, then the test dies on the real "
            "submodule). Use a concrete leaf, `pytest.importorskip('langgraph.graph')`"
            ":\n  " + "\n  ".join(offenders),
        )

    def test_probe_at_least_one_guard_exists(self):
        # Sanity: the suite really does carry langgraph guards, so the check
        # above isn't vacuously green because nothing matched.
        have = [
            p.name for p in _TESTS_DIR.glob("test_*.py")
            if "HAVE_LANGGRAPH = True" in p.read_text(encoding="utf-8")
        ]
        self.assertGreater(len(have), 5, f"expected many langgraph guards, found {have}")


if __name__ == "__main__":
    unittest.main()
