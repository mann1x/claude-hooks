"""Tests for code_graph.impact + code_graph.changes (Tier 1)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks.code_graph.builder import build_graph
from claude_hooks.code_graph.changes import (
    BlastEntry,
    blast_radius,
    format_blast_radius_report,
    git_changed_files,
    run_for_root,
    symbols_in_files,
)
from claude_hooks.code_graph.impact import (
    callees_of,
    callers_of,
    files_touched,
    format_disambig,
    format_impact_report,
    load_graph,
    name_candidates,
    resolve_target,
)


def _git_init(d: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(d), check=True)


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git_init(tmp_path)
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "core.py", """
def base_helper():
    \"\"\"Lowest-level fn — many callers.\"\"\"
    return 1

def mid():
    return base_helper()

def top():
    return mid()
""")
    _write(tmp_path / "pkg" / "alt.py", """
from pkg.core import base_helper

def alt_caller():
    return base_helper()

def isolated():
    \"\"\"No callers anywhere — leaf entrypoint or dead code.\"\"\"
    return 0
""")
    _write(tmp_path / "pkg" / "main.py", """
from pkg.core import top
from pkg.alt import alt_caller

def cli():
    top()
    alt_caller()
""")
    build_graph(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------

class TestResolve:
    def test_resolve_by_id(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "func:pkg.core.base_helper") == "func:pkg.core.base_helper"

    def test_resolve_by_qualname(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "pkg.core.base_helper") == "func:pkg.core.base_helper"

    def test_resolve_by_bare_name_when_unique(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "base_helper") == "func:pkg.core.base_helper"

    def test_resolve_ambiguous_returns_none(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "a.py", "def helper(): pass\n")
        _write(tmp_path / "b.py", "def helper(): pass\n")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        build_graph(tmp_path)
        graph = load_graph(tmp_path)
        assert resolve_target(graph, "helper") is None
        cands = name_candidates(graph, "helper")
        assert len(cands) == 2

    def test_resolve_module_by_file(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "pkg/core.py") == "module:pkg.core"

    def test_unknown_returns_none(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "no_such_thing_xyz") is None

    def test_empty_returns_none(self, repo):
        graph = load_graph(repo)
        assert resolve_target(graph, "") is None


# ---------------------------------------------------------------------------
# callers_of / callees_of
# ---------------------------------------------------------------------------

class TestBfs:
    def test_callers_includes_transitive(self, repo):
        graph = load_graph(repo)
        callers = callers_of(graph, "func:pkg.core.base_helper", max_depth=10)
        ids = {c[0] for c in callers}
        # Direct: mid, alt_caller. Transitive: top, cli.
        assert "func:pkg.core.mid" in ids
        assert "func:pkg.alt.alt_caller" in ids
        assert "func:pkg.core.top" in ids
        assert "func:pkg.main.cli" in ids

    def test_callers_respects_max_depth(self, repo):
        graph = load_graph(repo)
        d1 = callers_of(graph, "func:pkg.core.base_helper", max_depth=1)
        ids_d1 = {c[0] for c in d1}
        # Depth 1: mid + alt_caller. NOT top (which calls mid which calls base_helper).
        assert "func:pkg.core.mid" in ids_d1
        assert "func:pkg.alt.alt_caller" in ids_d1
        assert "func:pkg.core.top" not in ids_d1

    def test_callers_unbounded_when_max_depth_none(self, repo):
        graph = load_graph(repo)
        unb = callers_of(graph, "func:pkg.core.base_helper", max_depth=None)
        assert any(c[0] == "func:pkg.main.cli" for c in unb)

    def test_callers_excludes_seed(self, repo):
        graph = load_graph(repo)
        for cid, _ in callers_of(graph, "func:pkg.core.base_helper"):
            assert cid != "func:pkg.core.base_helper"

    def test_callees_walks_down(self, repo):
        graph = load_graph(repo)
        callees = callees_of(graph, "func:pkg.main.cli", max_depth=10)
        ids = {c[0] for c in callees}
        # cli → top → mid → base_helper, plus cli → alt_caller → base_helper
        assert "func:pkg.core.top" in ids
        assert "func:pkg.core.mid" in ids
        assert "func:pkg.core.base_helper" in ids
        assert "func:pkg.alt.alt_caller" in ids

    def test_isolated_has_no_callers(self, repo):
        graph = load_graph(repo)
        assert callers_of(graph, "func:pkg.alt.isolated") == []

    def test_unknown_node_returns_empty(self, repo):
        graph = load_graph(repo)
        assert callers_of(graph, "func:nope.nope") == []
        assert callees_of(graph, "func:nope.nope") == []

    def test_empty_node_id_returns_empty(self, repo):
        graph = load_graph(repo)
        assert callers_of(graph, "") == []


# ---------------------------------------------------------------------------
# Aggregation + report rendering
# ---------------------------------------------------------------------------

class TestReport:
    def test_files_touched_groups_by_file(self, repo):
        graph = load_graph(repo)
        callers = callers_of(graph, "func:pkg.core.base_helper", max_depth=10)
        grouped = files_touched(graph, [c[0] for c in callers])
        assert "pkg/core.py" in grouped
        assert "pkg/alt.py" in grouped
        assert "pkg/main.py" in grouped

    def test_format_impact_report_smoke(self, repo):
        graph = load_graph(repo)
        callers = callers_of(graph, "func:pkg.core.base_helper", max_depth=10)
        callees = callees_of(graph, "func:pkg.core.base_helper", max_depth=10)
        rep = format_impact_report(graph, "func:pkg.core.base_helper", callers, callees)
        assert "# Impact:" in rep
        assert "base_helper" in rep
        assert "Upstream callers" in rep
        assert "Downstream callees" in rep
        assert "pkg/main.py" in rep

    def test_isolated_report_handles_no_callers(self, repo):
        graph = load_graph(repo)
        rep = format_impact_report(graph, "func:pkg.alt.isolated", [], [])
        assert "No transitive callers" in rep

    def test_disambig_lists_candidates(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "a.py", "def helper(): pass\n")
        _write(tmp_path / "b.py", "def helper(): pass\n")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        build_graph(tmp_path)
        graph = load_graph(tmp_path)
        msg = format_disambig(name_candidates(graph, "helper"))
        assert "Ambiguous" in msg
        assert "a.py" in msg and "b.py" in msg


# ---------------------------------------------------------------------------
# Loader edge cases
# ---------------------------------------------------------------------------

class TestLoadGraph:
    def test_missing_returns_empty(self, tmp_path):
        assert load_graph(tmp_path) == {}

    def test_corrupt_returns_empty(self, repo):
        from claude_hooks.code_graph.detect import graph_json_path
        graph_json_path(repo).write_text("not json", encoding="utf-8")
        assert load_graph(repo) == {}


# ---------------------------------------------------------------------------
# git_changed_files
# ---------------------------------------------------------------------------

class TestGitChanges:
    def test_no_changes_empty(self, repo):
        assert git_changed_files(repo) == []

    def test_modified_file_shows_up(self, repo):
        (repo / "pkg" / "core.py").write_text("# touched\n", encoding="utf-8")
        files = git_changed_files(repo)
        assert "pkg/core.py" in files

    def test_staged_file_shows_up(self, repo):
        (repo / "pkg" / "core.py").write_text("# touched\n", encoding="utf-8")
        subprocess.run(["git", "add", "pkg/core.py"], cwd=str(repo), check=True)
        files = git_changed_files(repo)
        assert "pkg/core.py" in files

    def test_untracked_only_when_flag_set(self, repo):
        _write(repo / "pkg" / "newfile.py", "x = 1\n")
        assert "pkg/newfile.py" not in git_changed_files(repo)
        assert "pkg/newfile.py" in git_changed_files(repo, include_untracked=True)

    def test_non_repo_returns_empty(self, tmp_path):
        # No .git
        assert git_changed_files(tmp_path) == []


# ---------------------------------------------------------------------------
# blast_radius
# ---------------------------------------------------------------------------

class TestBlastRadius:
    def test_symbols_in_files(self, repo):
        graph = load_graph(repo)
        syms = symbols_in_files(graph, ["pkg/core.py"])
        names = {s["name"] for s in syms}
        assert names == {"base_helper", "mid", "top"}

    def test_blast_radius_sorted_by_caller_count(self, repo):
        graph = load_graph(repo)
        entries = blast_radius(graph, ["pkg/core.py"])
        # base_helper has the most callers (mid, alt_caller, top, cli)
        assert entries[0].node["name"] == "base_helper"
        # Sorted desc
        for a, b in zip(entries, entries[1:]):
            assert a.caller_count >= b.caller_count

    def test_blast_radius_empty_for_unknown_files(self, repo):
        graph = load_graph(repo)
        assert blast_radius(graph, ["pkg/nonexistent.py"]) == []

    def test_format_report_smoke(self, repo):
        graph = load_graph(repo)
        entries = blast_radius(graph, ["pkg/core.py"])
        rep = format_blast_radius_report(graph, entries, base="HEAD")
        assert "# Blast radius" in rep
        assert "base_helper" in rep
        assert "transitive caller" in rep

    def test_format_report_no_entries_still_renders(self, repo):
        graph = load_graph(repo)
        rep = format_blast_radius_report(graph, [], base="HEAD")
        assert "No tracked symbols" in rep


# ---------------------------------------------------------------------------
# run_for_root — top-level integration
# ---------------------------------------------------------------------------

class TestRunForRoot:
    def test_no_changes_short_message(self, repo):
        out = run_for_root(repo)
        assert "No changes vs `HEAD`" in out

    def test_with_changes_full_report(self, repo):
        (repo / "pkg" / "core.py").write_text(
            "def base_helper():\n    return 99\n", encoding="utf-8")
        out = run_for_root(repo)
        assert "Blast radius" in out
        assert "base_helper" in out

    def test_no_graph_returns_advice(self, tmp_path):
        _git_init(tmp_path)
        out = run_for_root(tmp_path)
        assert "No code graph" in out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def _run(self, *args, cwd):
        import os
        repo_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        return subprocess.run(
            [sys.executable, "-m", "claude_hooks.code_graph", *args],
            capture_output=True, text=True, timeout=30,
            cwd=str(cwd), env=env,
        )

    def test_impact_renders(self, repo):
        out = self._run("impact", "base_helper", "--root", str(repo), cwd=repo)
        assert out.returncode == 0, out.stderr
        assert "# Impact:" in out.stdout
        assert "base_helper" in out.stdout

    def test_impact_unknown_symbol(self, repo):
        out = self._run("impact", "no_such_xyz", "--root", str(repo), cwd=repo)
        assert out.returncode == 1
        assert "no symbol matching" in out.stderr

    def test_impact_ambiguous_prints_disambig(self, tmp_path):
        _git_init(tmp_path)
        _write(tmp_path / "a.py", "def helper(): pass\n")
        _write(tmp_path / "b.py", "def helper(): pass\n")
        for i in range(5):
            _write(tmp_path / f"f{i}.py", "x = 1\n")
        build_graph(tmp_path)
        out = self._run("impact", "helper", "--root", str(tmp_path), cwd=tmp_path)
        assert out.returncode == 1
        assert "Ambiguous" in out.stderr

    def test_changes_no_diff(self, repo):
        out = self._run("changes", "--root", str(repo), cwd=repo)
        assert out.returncode == 0
        assert "No changes" in out.stdout

    def test_changes_with_diff(self, repo):
        (repo / "pkg" / "core.py").write_text(
            "def base_helper():\n    return 99\n", encoding="utf-8")
        out = self._run("changes", "--root", str(repo), cwd=repo)
        assert out.returncode == 0
        assert "Blast radius" in out.stdout
        assert "base_helper" in out.stdout
