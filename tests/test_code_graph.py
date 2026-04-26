"""Tests for claude_hooks.code_graph — Python ast backend (MVP)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_hooks.code_graph import (
    GRAPH_DIRNAME,
    GRAPH_JSON_FILENAME,
    GRAPH_REPORT_FILENAME,
    build_session_block,
    is_code_repo,
    is_graph_stale,
    project_root,
)
from claude_hooks.code_graph.builder import build_graph, render_report
from claude_hooks.code_graph.detect import (
    EXTRACTABLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    graph_json_path,
    graph_report_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _git_init(d: Path) -> None:
    """Mark a directory as a project root for project_root()."""
    (d / ".git").mkdir()


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def small_repo(tmp_path: Path) -> Path:
    """A fake project with 6 Python files, classes, functions, calls."""
    _git_init(tmp_path)
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "config.py", """
def load_config():
    \"\"\"Load the config from disk.\"\"\"
    return _read()

def _read():
    return {}

class Settings:
    def get(self, key):
        return load_config().get(key)
""")
    _write(tmp_path / "pkg" / "main.py", """
from pkg.config import load_config, Settings

def main():
    \"\"\"CLI entrypoint.\"\"\"
    cfg = load_config()
    s = Settings()
    return s.get("k")

def helper():
    return main()
""")
    _write(tmp_path / "pkg" / "util.py", """
import os
import json

def safe_json(s):
    try:
        return json.loads(s)
    except ValueError:
        return None
""")
    _write(tmp_path / "pkg" / "empty.py", "# nothing here\n")
    _write(tmp_path / "tests" / "test_main.py", """
from pkg.main import main

def test_main():
    assert main() is None
""")
    return tmp_path


# ---------------------------------------------------------------------------
# detect.py
# ---------------------------------------------------------------------------

class TestDetect:
    def test_project_root_finds_git(self, small_repo):
        nested = small_repo / "pkg" / "deep"
        nested.mkdir()
        assert project_root(str(nested)) == small_repo

    def test_project_root_no_git_returns_none(self, tmp_path):
        assert project_root(str(tmp_path)) is None

    def test_project_root_empty_cwd(self):
        assert project_root("") is None

    def test_is_code_repo_threshold(self, small_repo):
        assert is_code_repo(small_repo, min_source_files=5) is True
        assert is_code_repo(small_repo, min_source_files=100) is False

    def test_is_code_repo_skips_ignored_dirs(self, tmp_path):
        _git_init(tmp_path)
        # Plant 10 .py files inside node_modules — must not count.
        for i in range(10):
            _write(tmp_path / "node_modules" / f"f{i}.py", "x = 1")
        assert is_code_repo(tmp_path, min_source_files=5) is False

    def test_is_graph_stale_when_missing(self, small_repo):
        assert is_graph_stale(small_repo) is True

    def test_is_graph_stale_within_cooldown(self, small_repo):
        # Build once, then check immediately — cooldown should win.
        build_graph(small_repo)
        assert is_graph_stale(small_repo, cooldown_minutes=10) is False

    def test_is_graph_stale_when_source_newer(self, small_repo):
        build_graph(small_repo)
        # Force the graph to look old, then touch a source file.
        gj = graph_json_path(small_repo)
        old = time.time() - 3600
        import os as _os
        _os.utime(gj, (old, old))
        # And touch a source file to be newer than the graph
        new_src = small_repo / "pkg" / "config.py"
        _os.utime(new_src, (time.time(), time.time()))
        assert is_graph_stale(small_repo, cooldown_minutes=0) is True

    def test_extractable_subset_of_supported(self):
        assert EXTRACTABLE_EXTENSIONS.issubset(SUPPORTED_EXTENSIONS)


# ---------------------------------------------------------------------------
# builder.py
# ---------------------------------------------------------------------------

class TestBuilder:
    def test_build_writes_outputs(self, small_repo):
        stats = build_graph(small_repo)
        assert (small_repo / GRAPH_DIRNAME / GRAPH_JSON_FILENAME).exists()
        assert (small_repo / GRAPH_DIRNAME / GRAPH_REPORT_FILENAME).exists()
        assert (small_repo / GRAPH_DIRNAME / "_meta.json").exists()
        assert stats["files_parsed"] >= 5
        assert stats["nodes"] > 5
        assert stats["edges"] > 0

    def test_graph_json_is_networkx_node_link(self, small_repo):
        build_graph(small_repo)
        payload = json.loads(graph_json_path(small_repo).read_text())
        assert payload["directed"] is True
        assert payload["multigraph"] is False
        assert "nodes" in payload and isinstance(payload["nodes"], list)
        assert "links" in payload and isinstance(payload["links"], list)
        # Every node has the fields graphify expects
        for n in payload["nodes"]:
            assert "id" in n and "type" in n and "tag" in n

    def test_meta_marks_managed_by_claude_hooks(self, small_repo):
        build_graph(small_repo)
        meta = json.loads((small_repo / GRAPH_DIRNAME / "_meta.json").read_text())
        assert meta["managed_by"] == "claude-hooks"

    def test_extracts_classes_functions_methods(self, small_repo):
        build_graph(small_repo)
        payload = json.loads(graph_json_path(small_repo).read_text())
        node_types = {n["type"] for n in payload["nodes"]}
        assert {"module", "class", "function", "method"}.issubset(node_types)

        names = {n["name"] for n in payload["nodes"]}
        assert {"load_config", "Settings", "get", "main", "helper"}.issubset(names)

    def test_resolves_intra_project_calls(self, small_repo):
        build_graph(small_repo)
        payload = json.loads(graph_json_path(small_repo).read_text())
        call_edges = [e for e in payload["links"] if e["type"] == "calls"]
        # main() calls load_config() — must be resolved end-to-end
        sources = {e["source"] for e in call_edges}
        targets = {e["target"] for e in call_edges}
        assert any("main" in s for s in sources)
        assert any("load_config" in t for t in targets)
        # No leftover unresolved markers
        assert not any(e["target"].startswith("unresolved:") for e in call_edges)

    def test_drops_external_imports(self, small_repo):
        build_graph(small_repo)
        payload = json.loads(graph_json_path(small_repo).read_text())
        # util.py imports os + json (stdlib) — those edges are pruned
        import_edges = [e for e in payload["links"] if e["type"] == "imports"]
        targets = {e["target"] for e in import_edges}
        assert "module:os" not in targets
        assert "module:json" not in targets
        # but the intra-project import survives
        assert "module:pkg.config" in targets

    def test_incremental_uses_cache(self, small_repo):
        s1 = build_graph(small_repo)
        s2 = build_graph(small_repo, incremental=True)
        # Second build should hit cache for every parsed file
        assert s2["files_cached"] == s2["files_parsed"]
        assert s2["files_parsed"] == s1["files_parsed"]

    def test_full_rebuild_ignores_cache(self, small_repo):
        build_graph(small_repo)
        s = build_graph(small_repo, incremental=False)
        assert s["files_cached"] == 0

    def test_handles_syntax_error_gracefully(self, small_repo):
        _write(small_repo / "pkg" / "broken.py", "def foo( :\n")
        stats = build_graph(small_repo)
        assert stats["parse_errors"] >= 1
        # Other files still parsed
        assert stats["files_parsed"] >= 5

    def test_render_report_no_crash_on_empty(self):
        empty_payload = {
            "graph": {"generated_at": "2026-01-01T00:00:00Z",
                      "stats": {"files_parsed": 0, "files_cached": 0,
                                "nodes": 0, "edges": 0, "by_language": {}}},
            "nodes": [],
            "links": [],
        }
        rep = render_report(empty_payload)
        assert "# Project graph report" in rep
        assert "no functions" in rep.lower() or "no modules" in rep.lower()


# ---------------------------------------------------------------------------
# inject.py
# ---------------------------------------------------------------------------

class TestInject:
    def test_returns_none_when_missing(self, tmp_path):
        assert build_session_block(tmp_path) is None

    def test_wraps_report_with_header(self, small_repo):
        build_graph(small_repo)
        block = build_session_block(small_repo)
        assert block is not None
        assert block.startswith("## Project code graph")
        assert "graphify-out/graph.json" in block
        assert "# Project graph report" in block

    def test_truncates_long_reports(self, small_repo):
        # Write a giant fake report
        gp = graph_report_path(small_repo)
        gp.parent.mkdir(parents=True, exist_ok=True)
        big = "## Section A\n" + ("x " * 5000) + "\n## Section B\n" + ("y " * 5000)
        gp.write_text(big, encoding="utf-8")
        block = build_session_block(small_repo, max_chars=2000)
        assert block is not None
        assert "truncated" in block
        assert len(block) <= 2200  # header adds a few hundred chars

    def test_truncates_at_heading_boundary(self, small_repo):
        gp = graph_report_path(small_repo)
        gp.parent.mkdir(parents=True, exist_ok=True)
        body = ("a" * 1500) + "\n## later\n" + ("b" * 1500)
        gp.write_text(body, encoding="utf-8")
        block = build_session_block(small_repo, max_chars=1700)
        # Should cut at the "## later" boundary
        assert "## later" not in block


# ---------------------------------------------------------------------------
# CLI / __main__
# ---------------------------------------------------------------------------

class TestCli:
    def _run(self, *args, cwd):
        import os
        repo = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return subprocess.run(
            [sys.executable, "-m", "claude_hooks.code_graph", *args],
            capture_output=True, text=True, timeout=30,
            cwd=str(cwd), env=env,
        )

    def test_build_writes_graph(self, small_repo):
        out = self._run("build", "--root", str(small_repo), "--quiet",
                        cwd=small_repo)
        assert out.returncode == 0, out.stderr
        assert graph_json_path(small_repo).exists()

    def test_info_prints_stats(self, small_repo):
        build_graph(small_repo)
        out = self._run("info", "--root", str(small_repo),
                        cwd=small_repo)
        assert out.returncode == 0, out.stderr
        parsed = json.loads(out.stdout)
        assert "stats" in parsed

    def test_info_fails_without_graph(self, small_repo):
        out = self._run("info", "--root", str(small_repo),
                        cwd=small_repo)
        assert out.returncode == 1
        assert "no graph" in out.stderr

    def test_build_fails_outside_git(self, tmp_path):
        # No .git/, so project_root returns None
        out = self._run("build", "--root", str(tmp_path),
                        cwd=tmp_path)
        assert out.returncode == 2
        assert "not inside a git repo" in out.stderr


# ---------------------------------------------------------------------------
# build_async — never raises, never blocks, respects locks
# ---------------------------------------------------------------------------

ts_skip = pytest.mark.skipif(
    True,  # set lazily below
    reason="tree-sitter-language-pack not installed",
)


def _ts_available() -> bool:
    from claude_hooks.code_graph import tree_sitter_backend as _ts
    return _ts.is_available()


class TestTreeSitterBackend:
    def setup_method(self):
        if not _ts_available():
            pytest.skip("tree-sitter backend not installed")

    def test_supported_extensions_nonempty(self):
        from claude_hooks.code_graph import tree_sitter_backend as _ts
        exts = _ts.supported_extensions()
        assert ".js" in exts
        assert ".ts" in exts
        assert ".go" in exts

    def test_extracts_javascript_functions(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "src" / "lib.js", """
function alpha() { return 1; }
function beta() { return alpha(); }
class Widget { render() { return beta(); } }
import { thing } from "./helpers";
""")
        # Plus enough Python to clear the threshold (tests should always
        # be runnable even when only Python or only JS is dominant).
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")

        stats = build_graph(tmp_path)
        payload = json.loads(graph_json_path(tmp_path).read_text())

        names = {n["name"] for n in payload["nodes"]}
        assert {"alpha", "beta", "Widget"}.issubset(names)
        # Method "render" should show up as a method node.
        method_names = {n["name"] for n in payload["nodes"] if n["type"] == "method"}
        assert "render" in method_names
        # The intra-file call beta() inside alpha-or-Widget should
        # resolve to either the function or via bare-name match.
        call_targets = {e["target"] for e in payload["links"] if e["type"] == "calls"}
        assert any("beta" in t or "alpha" in t for t in call_targets)
        assert ".js" in stats["by_language"]

    def test_extracts_typescript(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "src" / "api.ts", """
export interface User { id: number; }
export class UserStore {
    fetch(id: number): User { return { id }; }
}
function bootstrap() {
    const s = new UserStore();
    return s.fetch(1);
}
""")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        build_graph(tmp_path)
        payload = json.loads(graph_json_path(tmp_path).read_text())
        names = {n["name"] for n in payload["nodes"]}
        assert {"UserStore", "User", "bootstrap"}.issubset(names)

    def test_extracts_go(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "main.go", """
package main

import "fmt"

type Server struct{}

func (s *Server) Start() error { return nil }

func main() {
    s := &Server{}
    s.Start()
    fmt.Println("ok")
}
""")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        build_graph(tmp_path)
        payload = json.loads(graph_json_path(tmp_path).read_text())
        names = {n["name"] for n in payload["nodes"]}
        assert {"main", "Server", "Start"}.issubset(names)

    def test_normalize_import_target(self):
        from claude_hooks.code_graph.tree_sitter_backend import _normalize_import_target
        assert _normalize_import_target('"./foo/bar"') == "foo.bar"
        assert _normalize_import_target("'../helpers/util.js'") == "helpers.util"
        assert _normalize_import_target('"@scope/pkg"') == "@scope.pkg"
        assert _normalize_import_target("std::collections::HashMap") == "std.collections.HashMap"
        assert _normalize_import_target('"github.com/x/y"') == "github.com.x.y"
        assert _normalize_import_target('""') is None

    def test_handles_unparsable_gracefully(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "broken.js", "function foo( {{{")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        # Must not raise
        stats = build_graph(tmp_path)
        # File was attempted; either parsed or recorded as error
        assert stats["files_parsed"] >= 5  # the python files at minimum


class TestBackendOptional:
    def test_missing_backend_does_not_break_python_build(self, small_repo, monkeypatch):
        """Even if tree_sitter_backend.is_available() returns False the
        Python-only build path continues to work."""
        from claude_hooks.code_graph import tree_sitter_backend as _ts
        monkeypatch.setattr(_ts, "is_available", lambda: False)
        monkeypatch.setattr(_ts, "supported_extensions", lambda: frozenset())
        stats = build_graph(small_repo, incremental=False)
        assert stats["files_parsed"] >= 5
        payload = json.loads(graph_json_path(small_repo).read_text())
        # Python defs still extracted
        names = {n["name"] for n in payload["nodes"]}
        assert {"main", "load_config", "Settings"}.issubset(names)


class TestBuildAsync:
    def test_no_op_outside_git(self, tmp_path):
        from claude_hooks.code_graph.__main__ import build_async
        # Should silently do nothing — no exception, no graph file
        build_async(cwd=str(tmp_path))
        assert not (tmp_path / GRAPH_DIRNAME).exists()

    def test_no_op_when_too_few_files(self, tmp_path):
        from claude_hooks.code_graph.__main__ import build_async
        _git_init(tmp_path)
        _write(tmp_path / "only.py", "x = 1")
        build_async(cwd=str(tmp_path), min_source_files=5)
        # graph_dir might exist (lock dir created lazily) but no graph.json
        assert not graph_json_path(tmp_path).exists()

    def test_no_op_when_fresh(self, small_repo):
        from claude_hooks.code_graph.__main__ import build_async
        build_graph(small_repo)
        before_mtime = graph_json_path(small_repo).stat().st_mtime
        time.sleep(0.05)
        build_async(cwd=str(small_repo), cooldown_minutes=10)
        # Cooldown short-circuits before subprocess spawn
        time.sleep(0.5)
        after_mtime = graph_json_path(small_repo).stat().st_mtime
        assert after_mtime == before_mtime
