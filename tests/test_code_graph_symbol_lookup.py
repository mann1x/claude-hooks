"""Tests for claude_hooks.code_graph.symbol_lookup — PreToolUse hint stage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_hooks.code_graph import symbol_lookup as sl
from claude_hooks.code_graph.builder import build_graph
from claude_hooks.code_graph.detect import graph_json_path


# Reuse the small_repo fixture shape from test_code_graph.py without
# importing it (pytest fixtures don't cross files unless re-declared).
def _git_init(d: Path) -> None:
    (d / ".git").mkdir()


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git_init(tmp_path)
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "config.py", """
def load_config():
    return _read()

def _read():
    return {}

class Settings:
    def get(self, key):
        return load_config().get(key)
""")
    _write(tmp_path / "pkg" / "main.py", """
from pkg.config import load_config, Settings

def cli_entrypoint():
    return load_config()

def helper():
    return cli_entrypoint()
""")
    # Ambiguous: two `helper` defs in different modules
    _write(tmp_path / "pkg" / "util.py", """
def helper():
    return 42
""")
    _write(tmp_path / "pkg" / "io_glue.py", "x = 1\n")
    _write(tmp_path / "pkg" / "extra.py", "y = 1\n")
    build_graph(tmp_path)
    sl.clear_cache()
    return tmp_path


# ---------------------------------------------------------------------------
# looks_like_symbol — the heuristic gate
# ---------------------------------------------------------------------------

class TestLooksLikeSymbol:
    @pytest.mark.parametrize("p", [
        "load_config", "Settings", "MyClass.method",
        "cli_entrypoint", "_read",
    ])
    def test_accepts_real_identifiers(self, p):
        assert sl.looks_like_symbol(p) is True

    @pytest.mark.parametrize("p", [
        "",                 # empty
        "foo",              # too short
        "id",               # too short + stopword
        "error",            # stopword
        "main",             # stopword
        "foo|bar",          # alternation
        "foo.*",            # regex
        "[abc]",            # char class
        "\\bword\\b",       # word boundary
        "(?:x)",            # non-capturing group
        "load config",      # space → not an ident
        "config.py",        # ext suffix not allowed by ident regex
        "TODO",             # stopword (uppercase)
    ])
    def test_rejects_non_symbols(self, p):
        assert sl.looks_like_symbol(p) is False

    def test_dotted_path_accepted_when_leaf_is_real(self):
        assert sl.looks_like_symbol("pkg.config.load_config") is True

    def test_dotted_path_rejected_when_leaf_is_stopword(self):
        assert sl.looks_like_symbol("pkg.config.error") is False


# ---------------------------------------------------------------------------
# inject_for_grep — full path
# ---------------------------------------------------------------------------

class TestInject:
    def test_one_hit_returns_one_liner(self, repo):
        out = sl.inject_for_grep("load_config", repo)
        assert out is not None
        assert "## Symbol lookup" in out
        assert "load_config" in out
        assert "pkg/config.py" in out
        # Has degree info
        assert "callers" in out and "callees" in out

    def test_multi_hit_returns_list(self, repo):
        # `helper` is defined twice (pkg.main and pkg.util)
        out = sl.inject_for_grep("helper", repo)
        assert out is not None
        assert "(2 matches)" in out
        assert "pkg.main.helper" in out
        assert "pkg.util.helper" in out

    def test_zero_hits_returns_none(self, repo):
        assert sl.inject_for_grep("nonexistent_symbol_xyz", repo) is None

    def test_too_many_hits_returns_none(self, tmp_path):
        # Build a repo where `dispatch` exists in 10 modules
        _git_init(tmp_path)
        for i in range(10):
            _write(tmp_path / f"mod{i}.py", "def dispatch():\n    pass\n")
        # Plus enough other files to clear thresholds
        for i in range(5):
            _write(tmp_path / f"x{i}.py", "z = 1\n")
        build_graph(tmp_path)
        sl.clear_cache()
        assert sl.inject_for_grep("dispatch", tmp_path, max_hits=5) is None

    def test_qualified_pattern_narrows(self, repo):
        # `helper` has 2 hits; `pkg.util.helper` should narrow to 1
        out = sl.inject_for_grep("pkg.util.helper", repo)
        assert out is not None
        assert "pkg/util.py" in out
        assert "pkg/main.py" not in out

    def test_regex_pattern_returns_none(self, repo):
        assert sl.inject_for_grep("load_.*", repo) is None
        assert sl.inject_for_grep("(load|read)_config", repo) is None

    def test_stopword_returns_none(self, repo):
        # "error" exists nowhere here, but the gate should fire first
        # — never even hit the index.
        assert sl.inject_for_grep("error", repo) is None

    def test_no_graph_returns_none(self, tmp_path):
        # No build was run → graph.json doesn't exist
        _git_init(tmp_path)
        sl.clear_cache()
        assert sl.inject_for_grep("anything", tmp_path) is None

    def test_corrupt_graph_returns_none(self, repo):
        graph_json_path(repo).write_text("not json")
        sl.clear_cache()
        assert sl.inject_for_grep("load_config", repo) is None

    def test_never_raises(self, repo):
        # Pass garbage that would crash naive code
        for bad in [None, 0, [], {"x": 1}]:
            try:
                sl.inject_for_grep(bad, repo)  # type: ignore[arg-type]
            except Exception as e:
                pytest.fail(f"inject_for_grep raised on {bad!r}: {e}")

    def test_class_lookup(self, repo):
        out = sl.inject_for_grep("Settings", repo)
        assert out is not None
        assert "(class)" in out


# ---------------------------------------------------------------------------
# Caching — index reused while mtime is stable, refreshed on bump
# ---------------------------------------------------------------------------

class TestIndexCache:
    def test_cache_hit_skips_rebuild(self, repo, monkeypatch):
        sl.clear_cache()
        # First call populates the cache
        sl.inject_for_grep("load_config", repo)
        # Sabotage json.loads — second call must NOT touch it
        import json as _json
        monkeypatch.setattr(_json, "loads",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("rebuild!")))
        out = sl.inject_for_grep("load_config", repo)
        assert out is not None  # came from cache, didn't rebuild

    def test_mtime_change_triggers_rebuild(self, repo):
        sl.clear_cache()
        sl.inject_for_grep("load_config", repo)
        # Force the cached entry to look stale by rewriting its mtime
        # tuple. (Time-based deltas are flaky on fast filesystems where
        # two consecutive writes can land in the same float.)
        gj_str = str(repo)
        old_mtime, old_index = sl._INDEX_CACHE[gj_str]
        sl._INDEX_CACHE[gj_str] = (old_mtime - 100, old_index)

        # Add a new symbol and rebuild the graph.
        _write(repo / "pkg" / "newsym.py", "def brand_new_symbol():\n    pass\n")
        build_graph(repo)

        out = sl.inject_for_grep("brand_new_symbol", repo)
        assert out is not None
        assert "newsym.py" in out


# ---------------------------------------------------------------------------
# Hook integration — pre_tool_use stage
# ---------------------------------------------------------------------------

class TestHookIntegration:
    def _event(self, pattern, cwd):
        return {
            "tool_name": "Grep",
            "tool_input": {"pattern": pattern, "path": str(cwd)},
            "cwd": str(cwd),
        }

    def test_disabled_by_default(self, repo):
        from claude_hooks.config import DEFAULT_CONFIG
        from claude_hooks.hooks.pre_tool_use import handle
        out = handle(event=self._event("load_config", repo),
                     config=DEFAULT_CONFIG, providers=[])
        # Default config has code_graph_lookup_enabled=False → no inject
        assert out is None

    def test_enabled_emits_inject(self, repo):
        from copy import deepcopy
        from claude_hooks.config import DEFAULT_CONFIG
        from claude_hooks.hooks.pre_tool_use import handle

        sl.clear_cache()
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["code_graph_lookup_enabled"] = True
        out = handle(event=self._event("load_config", repo),
                     config=cfg, providers=[])
        assert out is not None
        block = out["hookSpecificOutput"]["additionalContext"]
        assert "load_config" in block
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_enabled_skips_non_grep(self, repo):
        from copy import deepcopy
        from claude_hooks.config import DEFAULT_CONFIG
        from claude_hooks.hooks.pre_tool_use import handle

        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["code_graph_lookup_enabled"] = True
        # Glob → no inject
        out = handle(
            event={"tool_name": "Glob",
                   "tool_input": {"pattern": "load_config"},
                   "cwd": str(repo)},
            config=cfg, providers=[],
        )
        assert out is None

    def test_enabled_silent_on_regex_pattern(self, repo):
        from copy import deepcopy
        from claude_hooks.config import DEFAULT_CONFIG
        from claude_hooks.hooks.pre_tool_use import handle

        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["code_graph_lookup_enabled"] = True
        out = handle(event=self._event("load_(config|cfg)", repo),
                     config=cfg, providers=[])
        assert out is None

    def test_budget_zero_returns_none(self, repo):
        from copy import deepcopy
        from claude_hooks.config import DEFAULT_CONFIG
        from claude_hooks.hooks.pre_tool_use import handle

        sl.clear_cache()
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["code_graph_lookup_enabled"] = True
        # Tiny budget → discard whatever came back. Use a negative number
        # so the deadline is already past by the time we check.
        cfg["hooks"]["pre_tool_use"]["code_graph_lookup_budget_ms"] = -1
        out = handle(event=self._event("load_config", repo),
                     config=cfg, providers=[])
        assert out is None
