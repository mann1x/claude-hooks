"""Tests for claude_hooks.code_graph.enclosing — the file:line →
function/class lookup layer used by consultants' citation_linter.

The fixture builds a tiny project, calls
:func:`claude_hooks.code_graph.builder.build_graph` against it, and
then asserts :func:`enclosing_symbol_at` answers correctly for known
lines. A second test exercises the legacy / missing-graph fallback
paths to confirm the helper returns ``None`` instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hooks.code_graph import enclosing
from claude_hooks.code_graph.builder import build_graph, EXTRACTOR_VERSION
from claude_hooks.code_graph.detect import graph_json_path


def _git_init(d: Path) -> None:
    (d / ".git").mkdir()


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Tiny project with known line ranges so the assertions stay
    grounded — explicit numbers below match the file layout."""
    _git_init(tmp_path)
    _write(tmp_path / "pkg" / "__init__.py", "")
    # NOTE: line numbers below count from 1 INCLUDING the leading
    # blank line that triple-quoted raw strings produce when started
    # on the next visual line. Comments mark expected lines next to
    # their defs to keep the test fixture self-documenting.
    body = (
        "def top_level():\n"          # line 1
        "    return 1\n"               # line 2
        "\n"                            # line 3
        "\n"                            # line 4
        "class Container:\n"           # line 5
        "    def inner_method(self):\n"  # line 6
        "        return 2\n"           # line 7
        "\n"                            # line 8
        "    def other_method(self):\n"  # line 9
        "        x = 3\n"              # line 10
        "        return x\n"           # line 11
        "\n"                            # line 12
        "\n"                            # line 13
        "def trailing():\n"            # line 14
        "    return 4\n"               # line 15
    )
    _write(tmp_path / "pkg" / "code.py", body)
    enclosing.clear_cache()
    build_graph(tmp_path)
    yield tmp_path
    enclosing.clear_cache()


def test_graph_built_includes_end_line(repo: Path) -> None:
    """Builder must stamp end_line on extracted nodes."""
    payload = json.loads(graph_json_path(repo).read_text())
    nodes = [
        n for n in payload["nodes"]
        if n.get("file") == "pkg/code.py"
        and n.get("type") in ("function", "method", "class")
    ]
    assert nodes, "expected function/class nodes for pkg/code.py"
    for n in nodes:
        assert "end_line" in n, n
        assert n["end_line"] >= n["line"], n


def test_top_level_function_line(repo: Path) -> None:
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", 1) == "top_level"
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", 2) == "top_level"


def test_inside_method_picks_method_over_class(repo: Path) -> None:
    """Innermost-span wins: line 7 is inside inner_method (6-7) which
    is inside class Container (5-11). Method must beat class."""
    assert (
        enclosing.enclosing_symbol_at(repo, "pkg/code.py", 7)
        == "inner_method"
    )


def test_inside_class_but_between_methods(repo: Path) -> None:
    """Blank line 8 is in class Container but not in any method —
    class enclosure should still win."""
    assert (
        enclosing.enclosing_symbol_at(repo, "pkg/code.py", 8)
        == "Container"
    )


def test_module_scope_returns_none(repo: Path) -> None:
    """Line 13 is between defs (no enclosure) → None."""
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", 13) is None


def test_line_beyond_eof_returns_none(repo: Path) -> None:
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", 9999) is None


def test_line_zero_or_negative_returns_none(repo: Path) -> None:
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", 0) is None
    assert enclosing.enclosing_symbol_at(repo, "pkg/code.py", -1) is None


def test_graph_covers_file_true(repo: Path) -> None:
    assert enclosing.graph_covers_file(repo, "pkg/code.py") is True


def test_graph_covers_file_false_for_unknown(repo: Path) -> None:
    assert enclosing.graph_covers_file(repo, "pkg/not_here.py") is False


def test_no_graph_returns_none_and_empty_cover(tmp_path: Path) -> None:
    """When graphify-out/ doesn't exist, both APIs degrade gracefully
    instead of raising — the caller falls back to ast.parse."""
    enclosing.clear_cache()
    assert (
        enclosing.enclosing_symbol_at(tmp_path, "foo.py", 1) is None
    )
    assert enclosing.graph_covers_file(tmp_path, "foo.py") is False


def test_legacy_graph_without_end_line_returns_none(
    tmp_path: Path,
) -> None:
    """Simulate a pre-extractor-v2 graph.json (no end_line on any
    function/class/method node). The index drops those rows so the
    file appears uncovered — the linter would then fall back to
    ast.parse, which is the correct behavior."""
    enclosing.clear_cache()
    out_dir = tmp_path / "graphify-out"
    out_dir.mkdir()
    legacy_payload = {
        "graph": {"generated_at": "2026-05-01T00:00:00Z"},
        "nodes": [
            {
                "id": "func:pkg.code.top_level",
                "type": "function",
                "name": "top_level",
                "file": "pkg/code.py",
                "line": 1,
                # NOTE: no end_line — the v1 schema didn't have it.
            },
        ],
        "links": [],
    }
    (out_dir / "graph.json").write_text(json.dumps(legacy_payload))
    assert enclosing.graph_covers_file(tmp_path, "pkg/code.py") is False
    assert (
        enclosing.enclosing_symbol_at(tmp_path, "pkg/code.py", 1) is None
    )


def test_cache_invalidated_on_mtime_change(repo: Path) -> None:
    """Touching graph.json with a different mtime forces a re-read."""
    enclosing.clear_cache()
    # Warm cache.
    enclosing.enclosing_symbol_at(repo, "pkg/code.py", 1)
    # Mutate the on-disk graph: re-write with a single different node.
    gj = graph_json_path(repo)
    payload = json.loads(gj.read_text())
    payload["nodes"].append({
        "id": "func:pkg.code.synthetic",
        "type": "function",
        "name": "synthetic",
        "file": "pkg/code.py",
        "line": 100,
        "end_line": 200,
    })
    gj.write_text(json.dumps(payload))
    # Bump mtime forward so the cache notices.
    import os
    st = gj.stat()
    os.utime(gj, (st.st_atime, st.st_mtime + 5))
    # New lookup must reflect the synthetic node.
    assert (
        enclosing.enclosing_symbol_at(repo, "pkg/code.py", 150)
        == "synthetic"
    )


def test_extractor_version_constant_is_2() -> None:
    """Lock the constant so a future bump triggers a deliberate
    review of cache-invalidation behavior in this test file."""
    assert EXTRACTOR_VERSION == 2
