"""Tests for Tier 3 code_graph features: mermaid, trace, clustering, mcp_server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks.code_graph.builder import build_graph
from claude_hooks.code_graph.impact import load_graph


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
def base():
    return 1

def mid():
    return base()

def top():
    return mid()
""")
    _write(tmp_path / "pkg" / "main.py", """
from pkg.core import top

def main():
    \"\"\"CLI entrypoint.\"\"\"
    return top()

def helper():
    return 0
""")
    _write(tmp_path / "pkg" / "alt.py", """
from pkg.core import base

def alt():
    return base()
""")
    build_graph(tmp_path)
    return tmp_path


# ===========================================================================
# Mermaid
# ===========================================================================

class TestMermaid:
    def test_module_map_emits_flowchart(self, repo):
        from claude_hooks.code_graph.mermaid import render_module_map
        out = render_module_map(load_graph(repo))
        assert out.startswith("flowchart LR")
        # Modules show up
        assert "pkg.core" in out or "pkg_core" in out

    def test_module_map_empty_graph(self):
        from claude_hooks.code_graph.mermaid import render_module_map
        out = render_module_map({})
        assert out.startswith("flowchart LR")
        assert "no modules" in out

    def test_module_map_caps_edges(self, repo):
        from claude_hooks.code_graph.mermaid import render_module_map
        out = render_module_map(load_graph(repo), max_edges=0)
        # No -->|n|--> edges
        assert "-->" not in out or "more import edges" in out

    def test_subgraph_around_node(self, repo):
        from claude_hooks.code_graph.mermaid import render_subgraph
        out = render_subgraph(load_graph(repo), "func:pkg.core.base", depth=2)
        assert out.startswith("flowchart TD")
        # The root gets its highlight class
        assert "classDef root" in out
        assert ":::root" in out

    def test_subgraph_unknown_node(self, repo):
        from claude_hooks.code_graph.mermaid import render_subgraph
        out = render_subgraph(load_graph(repo), "func:nope")
        assert "unknown" in out

    def test_safe_id_handles_dots_and_colons(self):
        from claude_hooks.code_graph.mermaid import _safe_id
        assert _safe_id("module:pkg.core.thing") == "module_pkg_core_thing"
        # Caps length
        assert len(_safe_id("a" * 100)) <= 48
        # Forces alpha leading char
        assert _safe_id("123foo").startswith("n_")


# ===========================================================================
# Trace
# ===========================================================================

class TestTrace:
    def test_enumerate_finds_main(self, repo):
        from claude_hooks.code_graph.trace import enumerate_entrypoints
        eps = enumerate_entrypoints(load_graph(repo))
        names = {e["name"] for e in eps}
        assert "main" in names
        # `top` is called by main → not an entrypoint
        assert "top" not in names

    def test_trace_walks_forward(self, repo):
        from claude_hooks.code_graph.trace import trace
        t = trace(load_graph(repo), "func:pkg.main.main", max_depth=10)
        # main → top → mid → base
        ids = set(t.nodes)
        assert "func:pkg.main.main" in ids  # root
        assert "func:pkg.core.top" in ids
        assert "func:pkg.core.mid" in ids
        assert "func:pkg.core.base" in ids

    def test_trace_respects_max_depth(self, repo):
        from claude_hooks.code_graph.trace import trace
        t = trace(load_graph(repo), "func:pkg.main.main", max_depth=1)
        ids = set(t.nodes)
        assert "func:pkg.core.top" in ids
        assert "func:pkg.core.mid" not in ids  # depth 2

    def test_trace_truncates_at_node_cap(self, repo):
        from claude_hooks.code_graph.trace import trace
        t = trace(load_graph(repo), "func:pkg.main.main",
                  max_depth=10, max_nodes=2)
        assert t.truncated is True
        assert len(t.nodes) <= 2

    def test_trace_unknown_root(self, repo):
        from claude_hooks.code_graph.trace import trace
        t = trace(load_graph(repo), "func:nope.nope")
        assert t.total == 0

    def test_trace_empty_root_returns_empty_trace(self, repo):
        from claude_hooks.code_graph.trace import trace
        t = trace(load_graph(repo), "")
        assert t.total == 0

    def test_format_trace_groups_by_depth(self, repo):
        from claude_hooks.code_graph.trace import format_trace_report, trace
        t = trace(load_graph(repo), "func:pkg.main.main", max_depth=10)
        rep = format_trace_report(load_graph(repo), t)
        assert "# Process trace:" in rep
        assert "Depth 1" in rep
        assert "Depth 2" in rep

    def test_format_trace_handles_leaf(self, repo):
        from claude_hooks.code_graph.trace import format_trace_report, trace
        t = trace(load_graph(repo), "func:pkg.core.base", max_depth=10)
        rep = format_trace_report(load_graph(repo), t)
        assert "No outgoing calls" in rep


# ===========================================================================
# Clustering
# ===========================================================================

class TestClustering:
    def test_compute_returns_one_per_node(self, repo):
        from claude_hooks.code_graph.clustering import compute_clusters
        clusters = compute_clusters(load_graph(repo))
        # 7 functions across 3 modules (base/mid/top, main/helper, alt)
        # All should get a cluster id
        assert len(clusters) >= 6
        for cid in clusters.values():
            assert isinstance(cid, int)

    def test_summary_sorted_by_size(self, repo):
        from claude_hooks.code_graph.clustering import (
            cluster_summary, compute_clusters,
        )
        clusters = compute_clusters(load_graph(repo))
        sums = cluster_summary(load_graph(repo), clusters)
        for a, b in zip(sums, sums[1:]):
            assert a.size >= b.size
        # Cohesion is in [0,1]
        for s in sums:
            assert 0.0 <= s.cohesion <= 1.0

    def test_summary_label_picks_common_prefix(self, tmp_path):
        from claude_hooks.code_graph.clustering import _most_common_label
        assert _most_common_label({"pkg/a/b.py", "pkg/a/c.py"}) == "pkg/a/"
        assert _most_common_label({"only.py"}) == "only.py"
        assert _most_common_label(set()) == ""

    def test_empty_graph_returns_empty(self):
        from claude_hooks.code_graph.clustering import compute_clusters
        assert compute_clusters({}) == {}

    def test_format_report_smoke(self, repo):
        from claude_hooks.code_graph.clustering import (
            cluster_summary, compute_clusters, format_cluster_report,
        )
        clusters = compute_clusters(load_graph(repo))
        sums = cluster_summary(load_graph(repo), clusters)
        rep = format_cluster_report(sums)
        assert "# Cluster summary" in rep

    def test_file_fallback_runs_without_louvain(self, repo, monkeypatch):
        from claude_hooks.code_graph import clustering as cl
        # Force the fallback path even if louvain is installed
        monkeypatch.setattr(cl, "is_louvain_available", lambda: False)
        clusters = cl.compute_clusters(load_graph(repo))
        assert len(clusters) > 0


# ===========================================================================
# MCP server (tool registrations only — no event-loop start)
# ===========================================================================

class TestMcpServer:
    def test_build_server_registers_tools(self):
        try:
            from claude_hooks.code_graph.mcp_server import build_server
        except ImportError:
            pytest.skip("mcp SDK not installed")
        server = build_server("test-server")
        # FastMCP exposes tools via async helpers; we check that at
        # least our six tools landed.
        import asyncio
        tool_list = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            server.list_tools()
        )
        names = {t.name for t in tool_list}
        expected = {
            "code_graph_lookup", "code_graph_impact",
            "code_graph_changes", "code_graph_trace",
            "code_graph_mermaid", "code_graph_companions",
        }
        assert expected.issubset(names)


# ===========================================================================
# CLI
# ===========================================================================

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

    def test_trace_cli(self, repo):
        out = self._run("trace", "main", "--root", str(repo), cwd=repo)
        assert out.returncode == 0, out.stderr
        assert "Process trace" in out.stdout

    def test_mermaid_cli_module_map(self, repo):
        out = self._run("mermaid", "--root", str(repo), cwd=repo)
        assert out.returncode == 0, out.stderr
        assert "flowchart" in out.stdout

    def test_mermaid_cli_centered(self, repo):
        out = self._run("mermaid", "--center", "base",
                        "--root", str(repo), cwd=repo)
        assert out.returncode == 0
        assert "flowchart" in out.stdout

    def test_clusters_cli(self, repo):
        out = self._run("clusters", "--root", str(repo), cwd=repo)
        assert out.returncode == 0, out.stderr
        assert "Cluster summary" in out.stdout
