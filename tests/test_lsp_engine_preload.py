"""Tests for the adaptive preload step.

Covers the two layers separately:

- ``rank_files_by_in_degree`` — pure JSON parsing + counting against
  synthetic graph.json fixtures.
- ``preload_engine`` — drives a stub engine, verifies the right
  files are opened in rank order, capped at ``max_files``, filtered
  by extension, and that missing-graph / missing-file paths
  soft-fail without raising.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.preload import (
    GRAPH_JSON_REL_PATH,
    preload_engine,
    rank_files_by_in_degree,
)


def _write_graph(path: Path, *, modules: list[tuple[str, str]],
                 imports: list[tuple[str, str]]) -> None:
    """Write a minimal node-link graph.json. ``modules`` are
    ``(id, file_rel)`` pairs; ``imports`` are ``(source_id,
    target_id)`` pairs.
    """
    nodes = [
        {"id": mid, "type": "module", "file": file_rel}
        for mid, file_rel in modules
    ]
    edges = [
        {"source": src, "target": dst, "type": "imports"}
        for src, dst in imports
    ]
    payload = {"graph": {"nodes": nodes, "edges": edges}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestRankFilesByInDegree(unittest.TestCase):
    def test_missing_graph_returns_empty(self) -> None:
        self.assertEqual(rank_files_by_in_degree("/no/such/graph.json"), [])

    def test_invalid_json_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "graph.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(rank_files_by_in_degree(p), [])

    def test_orders_by_in_degree_desc_then_path(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "graph.json"
            _write_graph(
                p,
                modules=[
                    ("module:pkg.utils", "pkg/utils.py"),
                    ("module:pkg.api",   "pkg/api.py"),
                    ("module:pkg.cli",   "pkg/cli.py"),
                    ("module:pkg.solo",  "pkg/solo.py"),
                ],
                imports=[
                    ("module:pkg.api",  "module:pkg.utils"),
                    ("module:pkg.cli",  "module:pkg.utils"),
                    ("module:pkg.cli",  "module:pkg.api"),
                ],
            )
            ranked = rank_files_by_in_degree(p)
            # utils has 2 incoming, api has 1, cli/solo have 0.
            self.assertEqual(ranked[0], ("pkg/utils.py", 2))
            self.assertEqual(ranked[1], ("pkg/api.py", 1))
            # cli and solo tie at 0; alphabetic by file path resolves
            # ties (cli before solo).
            self.assertEqual(ranked[2], ("pkg/cli.py", 0))
            self.assertEqual(ranked[3], ("pkg/solo.py", 0))

    def test_ignores_non_module_nodes(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "graph.json"
            payload = {
                "graph": {
                    "nodes": [
                        {"id": "module:a", "type": "module", "file": "a.py"},
                        {"id": "function:f", "type": "function", "file": "a.py"},
                    ],
                    "edges": [],
                }
            }
            p.write_text(json.dumps(payload), encoding="utf-8")
            ranked = rank_files_by_in_degree(p)
            self.assertEqual(ranked, [("a.py", 0)])

    def test_ignores_non_imports_edges(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "graph.json"
            _write_graph(
                p,
                modules=[
                    ("module:a", "a.py"),
                    ("module:b", "b.py"),
                ],
                imports=[],
            )
            payload = json.loads(p.read_text())
            # Add a non-imports edge — shouldn't bump in-degree.
            payload["graph"]["edges"].append({
                "source": "module:a", "target": "module:b", "type": "calls",
            })
            p.write_text(json.dumps(payload), encoding="utf-8")
            ranked = rank_files_by_in_degree(p)
            self.assertEqual({r[0]: r[1] for r in ranked}, {"a.py": 0, "b.py": 0})


class _StubEngine:
    """Captures did_open calls. Returns False for unknown extensions
    so we can verify ``preload_engine`` doesn't double-count rejected
    opens.
    """

    def __init__(self, accepted_exts: set[str]) -> None:
        self._accepted = accepted_exts
        self.opened: list[tuple[Path, str]] = []

    def did_open(self, path, content: str) -> bool:
        ext = Path(path).suffix.lower().lstrip(".")
        if ext not in self._accepted:
            return False
        self.opened.append((Path(path), content))
        return True

    def configured_extensions(self) -> set[str]:
        return set(self._accepted)


class TestPreloadEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _seed(self, files: dict[str, str], *,
              imports: list[tuple[str, str]] | None = None) -> None:
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        modules = [(f"module:{rel}", rel) for rel in files]
        _write_graph(
            self.root / GRAPH_JSON_REL_PATH,
            modules=modules,
            imports=[
                (f"module:{s}", f"module:{t}")
                for s, t in (imports or [])
            ],
        )

    def test_preloads_top_n_in_rank_order(self) -> None:
        self._seed(
            {
                "pkg/utils.py": "U",
                "pkg/api.py":   "A",
                "pkg/cli.py":   "C",
            },
            imports=[
                ("pkg/api.py",  "pkg/utils.py"),
                ("pkg/cli.py",  "pkg/utils.py"),
                ("pkg/cli.py",  "pkg/api.py"),
            ],
        )
        engine = _StubEngine({"py"})
        opened = preload_engine(engine, self.root, max_files=2)
        # 2 files opened: utils (in-deg=2) and api (in-deg=1).
        self.assertEqual(opened, 2)
        opened_names = [p.name for p, _ in engine.opened]
        self.assertEqual(opened_names, ["utils.py", "api.py"])
        # cli (in-deg=0) was below the cap and should NOT be opened.
        self.assertNotIn("cli.py", opened_names)

    def test_extension_filter_skips_unknown_languages(self) -> None:
        self._seed(
            {
                "pkg/utils.py": "U",
                "pkg/lib.rs":   "fn main() {}",
                "pkg/notes.md": "doc",
            },
        )
        engine = _StubEngine({"py"})
        opened = preload_engine(
            engine, self.root, max_files=10,
            extension_filter={"py"},
        )
        self.assertEqual(opened, 1)
        self.assertEqual(engine.opened[0][0].name, "utils.py")

    def test_missing_disk_file_skipped_silently(self) -> None:
        # Graph references a file that doesn't actually exist on disk.
        _write_graph(
            self.root / GRAPH_JSON_REL_PATH,
            modules=[("module:ghost", "pkg/ghost.py")],
            imports=[],
        )
        engine = _StubEngine({"py"})
        opened = preload_engine(engine, self.root, max_files=10)
        self.assertEqual(opened, 0)

    def test_missing_graph_returns_zero_no_raise(self) -> None:
        # No graph.json at all.
        engine = _StubEngine({"py"})
        opened = preload_engine(engine, self.root, max_files=10)
        self.assertEqual(opened, 0)
        self.assertEqual(engine.opened, [])

    def test_engine_rejection_does_not_count(self) -> None:
        """If the stub engine returns False (unknown extension), the
        attempt shouldn't push toward the ``max_files`` cap. With 5
        rejected files and 1 accepted, the loop should still cap at
        max_files=1 on the accepted one.
        """
        self._seed(
            {
                "a.weird":    "",
                "b.weird":    "",
                "c.weird":    "",
                "d.weird":    "",
                "e.weird":    "",
                "real.py":    "x = 1",
            },
        )
        engine = _StubEngine({"py"})
        opened = preload_engine(engine, self.root, max_files=1)
        self.assertEqual(opened, 1)
        self.assertEqual(engine.opened[0][0].name, "real.py")


if __name__ == "__main__":
    unittest.main()
