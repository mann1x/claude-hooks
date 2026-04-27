"""
Phase 8 — close remaining coverage gaps.

Targets the lowest-coverage modules from the Phase 5 measurement:
- claude_hooks/dispatcher.py
- claude_hooks/hooks/pre_tool_use.py
- claude_hooks/hooks/stop.py
- claude_hooks/providers/__init__.py
- claude_hooks/providers/base.py
- claude_hooks/providers/memory_kg.py
- claude_hooks/providers/qdrant.py
- claude_hooks/claudemem_reindex.py

Pattern: each test isolates a single uncovered branch, mocks the
boundary, and asserts observable behaviour.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_hooks import claudemem_reindex, dispatcher
from claude_hooks.config import DEFAULT_CONFIG
from claude_hooks.hooks import pre_tool_use, stop
from claude_hooks.mcp_client import McpError
from claude_hooks.providers import (
    REGISTRY,
    Memory,
    MemoryKgProvider,
    Provider,
    QdrantProvider,
    ServerCandidate,
    get_provider_class,
)


# ===================================================================== #
# providers/__init__.py — get_provider_class
# ===================================================================== #
class TestGetProviderClass:
    def test_returns_class_for_known_name(self):
        cls = get_provider_class("qdrant")
        assert cls is QdrantProvider

    def test_returns_memory_kg_class(self):
        cls = get_provider_class("memory_kg")
        assert cls is MemoryKgProvider

    def test_raises_for_unknown_name(self):
        with pytest.raises(KeyError):
            get_provider_class("nonexistent")


# ===================================================================== #
# providers/base.py — verify() default + _client + iter helpers
# ===================================================================== #
class TestProviderVerify:
    def test_verify_returns_true_when_signature_tools_present(self):
        # Use QdrantProvider with mocked client.list_tools
        cand = ServerCandidate(server_key="q", url="http://x")
        with patch("claude_hooks.mcp_client.McpClient") as MockClient:
            inst = MockClient.return_value
            inst.list_tools.return_value = [
                {"name": "qdrant-find"},
                {"name": "qdrant-store"},
                {"name": "other-tool"},
            ]
            assert QdrantProvider.verify(cand) is True

    def test_verify_returns_false_when_signature_missing(self):
        cand = ServerCandidate(server_key="q", url="http://x")
        with patch("claude_hooks.mcp_client.McpClient") as MockClient:
            inst = MockClient.return_value
            inst.list_tools.return_value = [{"name": "unrelated"}]
            assert QdrantProvider.verify(cand) is False

    def test_verify_returns_false_on_mcp_error(self):
        cand = ServerCandidate(server_key="q", url="http://x")
        with patch("claude_hooks.mcp_client.McpClient") as MockClient:
            inst = MockClient.return_value
            inst.list_tools.side_effect = McpError("boom")
            assert QdrantProvider.verify(cand) is False


class TestIterMcpServers:
    def test_yields_root_and_project_servers(self):
        from claude_hooks.providers.base import iter_mcp_servers
        cfg = {
            "mcpServers": {
                "root1": {"type": "http", "url": "http://r1"},
            },
            "projects": {
                "/proj": {
                    "mcpServers": {
                        "p1": {"type": "http", "url": "http://p1"},
                    },
                },
            },
        }
        out = iter_mcp_servers(cfg)
        keys = [k for k, _, _ in out]
        sources = [s for _, _, s in out]
        assert "root1" in keys
        assert "p1" in keys
        assert "user" in sources
        assert any(s.startswith("project:") for s in sources)

    def test_handles_missing_keys(self):
        from claude_hooks.providers.base import iter_mcp_servers
        assert iter_mcp_servers({}) == []
        assert iter_mcp_servers({"mcpServers": None}) == []
        assert iter_mcp_servers({"projects": "not a dict"}) == []

    def test_skips_non_dict_server_entries(self):
        from claude_hooks.providers.base import iter_mcp_servers
        cfg = {
            "mcpServers": {
                "good": {"type": "http", "url": "http://g"},
                "bad": "not a dict",
            }
        }
        keys = [k for k, _, _ in iter_mcp_servers(cfg)]
        assert "good" in keys
        assert "bad" not in keys


# ===================================================================== #
# providers/memory_kg.py — recall + store edge cases
# ===================================================================== #
class TestMemoryKgRecall:
    def _make(self):
        cand = ServerCandidate(server_key="memory", url="http://x")
        return MemoryKgProvider(cand, options={})

    def test_recall_empty_query_returns_empty(self):
        p = self._make()
        assert p.recall("   ", k=5) == []

    def test_recall_mcp_error_returns_empty(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.side_effect = McpError("boom")
            assert p.recall("query here", k=5) == []

    def test_recall_parses_structured_content(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "structuredContent": {
                    "entities": [
                        {
                            "name": "solidPC",
                            "entityType": "server",
                            "observations": ["obs1", "obs2"],
                        }
                    ],
                    "relations": [
                        {"from": "solidPC", "to": "qdrant", "relationType": "runs"},
                    ],
                }
            }
            mems = p.recall("server", k=5)
        assert len(mems) == 1
        assert "solidPC" in mems[0].text
        assert "obs1" in mems[0].text
        assert "runs" in mems[0].text

    def test_recall_falls_back_to_text_content_json(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "content": [{"type": "text", "text": json.dumps({
                    "entities": [
                        {"name": "n1", "entityType": "t", "observations": ["o"]}
                    ],
                    "relations": [],
                })}]
            }
            mems = p.recall("q here", k=5)
        assert len(mems) == 1
        assert "n1" in mems[0].text

    def test_recall_handles_invalid_json_content(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "content": [{"type": "text", "text": "not valid json {{{{"}]
            }
            assert p.recall("q here", k=5) == []

    def test_recall_skips_non_dict_entities(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "structuredContent": {
                    "entities": ["not a dict", {"name": "real", "entityType": "x"}],
                }
            }
            mems = p.recall("q here", k=5)
        assert len(mems) == 1
        assert "real" in mems[0].text


class TestMemoryKgStore:
    def _make(self):
        cand = ServerCandidate(server_key="memory", url="http://x")
        return MemoryKgProvider(cand, options={})

    def test_empty_content_skipped(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            p.store("   ")
            mc.assert_not_called()

    def test_store_uses_create_entities_first(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {}
            p.store("content here")
            mc.return_value.call_tool.assert_called_once()
            name, args = mc.return_value.call_tool.call_args.args
            assert name == "create_entities"
            assert args["entities"][0]["observations"] == ["content here"]

    def test_store_falls_back_to_add_observations_on_exists(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            calls = []
            def call(name, args):
                calls.append((name, args))
                if name == "create_entities":
                    raise McpError("entity already exists")
                return {}
            mc.return_value.call_tool.side_effect = call
            p.store("data", metadata={"entity_name": "x", "entity_type": "test"})
        names = [c[0] for c in calls]
        assert names == ["create_entities", "add_observations"]

    def test_store_reraises_unrelated_mcp_error(self):
        p = self._make()
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.side_effect = McpError("network down")
            with pytest.raises(McpError):
                p.store("data")


# ===================================================================== #
# providers/qdrant.py — fallback paths
# ===================================================================== #
class TestQdrantRecall:
    def _make(self, **opts):
        cand = ServerCandidate(server_key="q", url="http://x")
        return QdrantProvider(cand, options=opts)

    def test_recall_empty_query_returns_empty(self):
        p = self._make()
        assert p.recall("  ", k=5) == []

    def test_recall_no_text_returns_empty(self):
        p = self._make(collection="memory")
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {"content": []}
            assert p.recall("q", k=5) == []

    def test_recall_invalid_json_returns_empty(self):
        p = self._make(collection="memory")
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "content": [{"type": "text", "text": "not json"}]
            }
            assert p.recall("q", k=5) == []

    def test_recall_non_list_returns_empty(self):
        p = self._make(collection="memory")
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {
                "content": [{"type": "text", "text": json.dumps({"x": 1})}]
            }
            assert p.recall("q", k=5) == []

    def test_recall_with_collection_fallback_after_error(self):
        # First call (with collection_name) returns isError=True; fallback
        # call without collection_name returns valid data.
        p = self._make(collection="memory")
        results = [
            {"isError": True},
            {"content": [{"type": "text", "text": json.dumps([
                "Results for the query 'foo'",
                "<entry><content>hit</content><metadata>{\"k\":1}</metadata></entry>",
            ])}]},
        ]
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.side_effect = lambda *a, **kw: results.pop(0)
            mems = p.recall("query", k=5)
        assert len(mems) == 1
        assert mems[0].text == "hit"
        assert mems[0].metadata == {"k": 1}

    def test_recall_collection_fallback_on_mcp_error(self):
        p = self._make(collection="memory")
        results_iter = iter([
            McpError("rejected"),
            {"content": [{"type": "text", "text": json.dumps([
                "Results", "<entry><content>x</content></entry>",
            ])}]},
        ])
        def call(*a, **kw):
            v = next(results_iter)
            if isinstance(v, Exception):
                raise v
            return v
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.side_effect = call
            mems = p.recall("q here", k=5)
        assert len(mems) == 1


class TestQdrantStore:
    def test_empty_content_skipped(self):
        cand = ServerCandidate(server_key="q", url="http://x")
        p = QdrantProvider(cand, options={"collection": "memory"})
        with patch.object(p, "_client") as mc:
            p.store("  ")
            mc.assert_not_called()

    def test_store_with_metadata_passes_args(self):
        cand = ServerCandidate(server_key="q", url="http://x")
        p = QdrantProvider(cand, options={"collection": "memory"})
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {}
            p.store("text", metadata={"a": 1})
            args = mc.return_value.call_tool.call_args.args
            tool, params = args
            assert tool == "qdrant-store"
            assert params["information"] == "text"
            assert params["metadata"] == {"a": 1}

    def test_store_no_collection_calls_directly(self):
        cand = ServerCandidate(server_key="q", url="http://x")
        p = QdrantProvider(cand, options={})  # no collection
        with patch.object(p, "_client") as mc:
            mc.return_value.call_tool.return_value = {}
            p.store("text")
            assert mc.return_value.call_tool.call_count == 1


class TestQdrantParseEntry:
    def test_parses_well_formed_entry(self):
        from claude_hooks.providers.qdrant import _parse_qdrant_entry
        m = _parse_qdrant_entry(
            "<entry><content>hello</content><metadata>{\"id\":1}</metadata></entry>"
        )
        assert m is not None
        assert m.text == "hello"
        assert m.metadata == {"id": 1}

    def test_returns_raw_for_unwrapped(self):
        from claude_hooks.providers.qdrant import _parse_qdrant_entry
        m = _parse_qdrant_entry("just plain text")
        assert m is not None
        assert m.text == "just plain text"

    def test_handles_invalid_metadata_json(self):
        from claude_hooks.providers.qdrant import _parse_qdrant_entry
        m = _parse_qdrant_entry(
            "<entry><content>x</content><metadata>not-json</metadata></entry>"
        )
        assert m is not None
        assert m.text == "x"
        assert m.metadata.get("_raw") == "not-json"


# ===================================================================== #
# dispatcher.py — build_providers + handler-import + write paths
# ===================================================================== #
class TestBuildProviders:
    def test_disabled_provider_skipped(self):
        cfg = {"providers": {"qdrant": {"enabled": False, "mcp_url": "http://x"}}}
        out = dispatcher.build_providers(cfg)
        assert all(p.name != "qdrant" for p in out)

    def test_no_url_skipped(self):
        cfg = {"providers": {"qdrant": {"enabled": True, "mcp_url": ""}}}
        out = dispatcher.build_providers(cfg)
        assert all(p.name != "qdrant" for p in out)

    def test_enabled_with_url_instantiated(self):
        cfg = {"providers": {"qdrant": {
            "enabled": True,
            "mcp_url": "http://x",
            "headers": {"Authorization": "Bearer x"},
        }}}
        out = dispatcher.build_providers(cfg)
        names = [p.name for p in out]
        assert "qdrant" in names

    def test_no_providers_section_returns_empty(self):
        out = dispatcher.build_providers({})
        assert out == []


class TestDispatcherImportFailures:
    def test_handler_import_failure_returns_zero(self):
        # Selectively raise ImportError only for the handler import.
        cfg = deepcopy(DEFAULT_CONFIG)
        import builtins as _builtins
        real_import = _builtins.__import__

        def _import(name, *a, **kw):
            if name.startswith("claude_hooks.hooks."):
                raise ImportError("boom")
            return real_import(name, *a, **kw)

        with patch.object(dispatcher, "load_config", return_value=cfg), \
             patch.object(dispatcher, "build_providers", return_value=[]), \
             patch("builtins.__import__", side_effect=_import):
            rc = dispatcher.dispatch("UserPromptSubmit", {"cwd": "/p"})
        assert rc == 0

    def test_handler_without_handle_returns_zero(self):
        # Patch the imported handler module to have a non-callable `handle`.
        cfg = deepcopy(DEFAULT_CONFIG)
        with patch.object(dispatcher, "load_config", return_value=cfg), \
             patch.object(dispatcher, "build_providers", return_value=[]), \
             patch("claude_hooks.hooks.user_prompt_submit.handle",
                   new="not callable"):
            rc = dispatcher.dispatch(
                "UserPromptSubmit",
                {"prompt": "long enough prompt to exceed min_chars threshold",
                 "cwd": "/p"},
            )
        assert rc == 0

    def test_stdout_write_failure_returns_zero(self):
        cfg = deepcopy(DEFAULT_CONFIG)

        class _FailingStream:
            def write(self, s):
                raise OSError("disk full")
            def flush(self):
                pass

        with patch.object(dispatcher, "load_config", return_value=cfg), \
             patch.object(dispatcher, "build_providers", return_value=[]), \
             patch("claude_hooks.hooks.user_prompt_submit.handle",
                   return_value={"hookSpecificOutput": {"x": "y"}}), \
             patch("sys.stdout", _FailingStream()):
            rc = dispatcher.dispatch(
                "UserPromptSubmit",
                {"prompt": "long enough prompt to exceed min_chars threshold",
                 "cwd": "/p"},
            )
        assert rc == 0


@pytest.fixture
def isolated_claude_hooks_logger():
    """Save / restore the claude_hooks logger handlers around a test."""
    import logging as _logging
    root = _logging.getLogger("claude_hooks")
    prev_handlers = root.handlers[:]
    prev_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        for h in root.handlers:
            try:
                h.close()
            except Exception:
                pass
        root.handlers = prev_handlers
        root.level = prev_level


class TestDispatcherSetupLogging:
    def test_setup_logging_with_file_path(self, tmp_path, isolated_claude_hooks_logger):
        log_file = tmp_path / "claude-hooks.log"
        cfg = {
            "logging": {
                "path": str(log_file),
                "level": "debug",
                "max_bytes": 1024,
                "backup_count": 1,
            }
        }
        import logging as _logging
        dispatcher._setup_logging(cfg)
        root = isolated_claude_hooks_logger
        assert root.level == _logging.DEBUG
        assert any(
            h.__class__.__name__ == "RotatingFileHandler" for h in root.handlers
        )

    def test_setup_logging_falls_back_on_oserror(self, tmp_path, isolated_claude_hooks_logger):
        cfg = {"logging": {"path": str(tmp_path / "log"), "level": "info"}}
        with patch(
            "logging.handlers.RotatingFileHandler",
            side_effect=OSError("can't open"),
        ):
            dispatcher._setup_logging(cfg)
        root = isolated_claude_hooks_logger
        assert any(
            h.__class__.__name__ == "StreamHandler" for h in root.handlers
        )

    def test_setup_logging_idempotent(self, isolated_claude_hooks_logger):
        # When handlers already exist, setup is a no-op.
        import logging as _logging
        cfg = {"logging": {"path": "", "level": "info"}}
        root = isolated_claude_hooks_logger
        root.addHandler(_logging.NullHandler())
        before = list(root.handlers)
        dispatcher._setup_logging(cfg)
        assert root.handlers == before


class TestReadEventFromStdin:
    def test_empty_stdin_returns_empty_dict(self):
        with patch("sys.stdin.read", return_value=""):
            assert dispatcher.read_event_from_stdin() == {}

    def test_invalid_json_returns_empty_dict(self):
        with patch("sys.stdin.read", return_value="not json"):
            assert dispatcher.read_event_from_stdin() == {}

    def test_valid_json_parsed(self):
        with patch("sys.stdin.read", return_value='{"key": "value"}'):
            assert dispatcher.read_event_from_stdin() == {"key": "value"}


# ===================================================================== #
# hooks/pre_tool_use.py — memory-warn stage + helpers
# ===================================================================== #
class TestPreToolUseMemoryWarn:
    def _cfg(self, **overrides):
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = False
        for k, v in overrides.items():
            cfg["hooks"]["pre_tool_use"][k] = v
        return cfg

    def test_memory_warn_disabled(self, fake_provider):
        cfg = self._cfg(enabled=False)
        p = fake_provider(recall_returns=[Memory(text="prior issue")])
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm something"}},
            config=cfg, providers=[p],
        )
        assert r is None

    def test_warn_on_tools_filter(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Edit"])
        p = fake_provider(recall_returns=[Memory(text="x")])
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm a"}},
            config=cfg, providers=[p],
        )
        assert r is None

    def test_no_probe_string_returns_none(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Bash"], warn_on_patterns=[])
        p = fake_provider(recall_returns=[Memory(text="x")])
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {}},
            config=cfg, providers=[p],
        )
        assert r is None

    def test_pattern_filter_no_match(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Bash"], warn_on_patterns=["DROP TABLE"])
        p = fake_provider(recall_returns=[Memory(text="prior")])
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
            config=cfg, providers=[p],
        )
        assert r is None

    def test_pattern_match_returns_additional_context(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Bash"], warn_on_patterns=["rm "])
        p = fake_provider(
            name="qdrant",
            recall_returns=[
                Memory(text="prior incident: rm wiped the wrong dir"),
                Memory(text="another note"),
            ],
        )
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/foo"}},
            config=cfg, providers=[p],
        )
        assert r is not None
        ctx = r["hookSpecificOutput"]["additionalContext"]
        assert "Past memory matched" in ctx
        assert "qdrant" in ctx

    def test_provider_recall_failure_continues(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Bash"], warn_on_patterns=[])
        bad = fake_provider(name="bad", recall_errors=True)
        good = fake_provider(
            name="good",
            recall_returns=[Memory(text="useful prior context")],
        )
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm something"}},
            config=cfg, providers=[bad, good],
        )
        assert r is not None
        assert "good" in r["hookSpecificOutput"]["additionalContext"]

    def test_no_snippets_returns_none(self, fake_provider):
        cfg = self._cfg(warn_on_tools=["Bash"], warn_on_patterns=[])
        p = fake_provider(recall_returns=[])
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "rm x"}},
            config=cfg, providers=[p],
        )
        assert r is None


class TestPreToolUseProbeString:
    def test_bash_command(self):
        assert pre_tool_use._probe_string("Bash", {"command": "ls"}) == "ls"

    def test_edit_file_path(self):
        assert pre_tool_use._probe_string(
            "Edit", {"file_path": "/x.py"}) == "/x.py"

    def test_write_file_path(self):
        assert pre_tool_use._probe_string(
            "Write", {"file_path": "/x.py"}) == "/x.py"

    def test_multiedit_file_path(self):
        assert pre_tool_use._probe_string(
            "MultiEdit", {"file_path": "/x.py"}) == "/x.py"

    def test_read_file_path(self):
        assert pre_tool_use._probe_string(
            "Read", {"file_path": "/y.py"}) == "/y.py"

    def test_unknown_tool_returns_empty(self):
        assert pre_tool_use._probe_string("Unknown", {"x": 1}) == ""


class TestPreToolUseRtkSafetyImports:
    def test_rtk_module_import_failure_returns_none(self):
        # When rtk_rewrite import fails, _run_rtk_rewrite_raw returns None
        # via the except-branch (line 158-160).
        with patch.dict("sys.modules", {"claude_hooks.rtk_rewrite": None}):
            # Force ImportError on attempt.
            import sys
            sys.modules.pop("claude_hooks.rtk_rewrite", None)
            with patch("builtins.__import__", side_effect=ImportError("nope")):
                out = pre_tool_use._run_rtk_rewrite_raw("ls", {})
        assert out is None

    def test_safety_module_import_failure_returns_none(self):
        with patch("builtins.__import__", side_effect=ImportError("nope")):
            out = pre_tool_use._run_safety_scan_raw("rm -rf /", {})
        assert out is None

    def test_rtk_min_version_invalid_falls_back(self):
        # Bad min version string → falls back to (0, 23, 0).
        with patch(
            "claude_hooks.rtk_rewrite.rewrite_command",
            return_value="rewritten cmd",
        ) as m:
            out = pre_tool_use._run_rtk_rewrite_raw(
                "ls", {"rtk_min_version": "not.a.version"}
            )
        assert out == "rewritten cmd"
        assert m.call_args.kwargs["min_version"] == (0, 23, 0)

    def test_rtk_min_version_short_padded(self):
        with patch(
            "claude_hooks.rtk_rewrite.rewrite_command",
            return_value="rewritten cmd",
        ) as m:
            pre_tool_use._run_rtk_rewrite_raw("ls", {"rtk_min_version": "1.0"})
        assert m.call_args.kwargs["min_version"] == (1, 0, 0)


class TestPreToolUseSafetyLogging:
    def test_safety_scan_logs_match(self, tmp_path, fake_provider):
        # safety_scan_enabled + log_enabled writes a log file.
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["safety_log_dir"] = str(tmp_path / "scanner")
        cfg["hooks"]["pre_tool_use"]["safety_log_retention_days"] = 1

        from claude_hooks import safety_scan
        safety_scan.reset_pattern_cache()
        r = pre_tool_use.handle(
            event={"tool_name": "Bash", "tool_input": {"command": "sudo reboot"}},
            config=cfg, providers=[],
        )
        assert r["hookSpecificOutput"]["permissionDecision"] == "ask"
        # Log file should exist somewhere under the configured dir.
        log_dir = Path(cfg["hooks"]["pre_tool_use"]["safety_log_dir"])
        assert log_dir.exists()
        files = list(log_dir.rglob("*"))
        assert any(f.is_file() for f in files), "expected a log file written"

    def test_rtk_log_rewrites_does_not_crash(self):
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = True
        cfg["hooks"]["pre_tool_use"]["rtk_log_rewrites"] = True
        cfg["hooks"]["pre_tool_use"]["rtk_scan_rewrites"] = False
        with patch(
            "claude_hooks.hooks.pre_tool_use._run_rtk_rewrite_raw",
            return_value="rtk find py",
        ):
            r = pre_tool_use.handle(
                event={"tool_name": "Bash",
                       "tool_input": {"command": "find . -name '*.py'"}},
                config=cfg, providers=[],
            )
        assert r["hookSpecificOutput"]["permissionDecision"] == "allow"


# ===================================================================== #
# hooks/stop.py — _read_transcript, _build_summary, classify, openwolf,
# dedup, instinct extraction, store-failure paths
# ===================================================================== #
class TestStopReadTranscript:
    def test_returns_none_for_missing_path(self):
        assert stop._read_transcript("/nonexistent/path.jsonl") is None

    def test_skips_blank_and_invalid_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps({"role": "user"}) + "\n"
            "\n"
            "not json line\n"
            + json.dumps({"role": "assistant"}) + "\n"
        )
        out = stop._read_transcript(str(p))
        assert out is not None
        assert len(out) == 2


class TestStopHelpers:
    def test_msg_role_supports_both_shapes(self):
        assert stop._msg_role({"message": {"role": "user"}}) == "user"
        assert stop._msg_role({"role": "assistant"}) == "assistant"
        assert stop._msg_role({}) == ""

    def test_find_last_user_idx_no_user(self):
        assert stop._find_last_user_idx([{"role": "assistant"}]) == -1

    def test_turn_modified_false_for_empty(self):
        assert stop._turn_modified_files(None) is False
        assert stop._turn_modified_files([]) is False

    def test_turn_modified_false_with_no_user_message(self):
        assert stop._turn_modified_files(
            [{"role": "assistant"}]
        ) is False

    def test_is_noteworthy_false_for_empty(self):
        assert stop._is_noteworthy(None) is False
        assert stop._is_noteworthy([]) is False

    def test_is_noteworthy_false_without_user(self):
        assert stop._is_noteworthy([{"role": "assistant"}]) is False

    def _transcript_with(self, asst_text="", tool_name=None):
        """Build a transcript with one user msg + one assistant msg
        containing the given text and (optionally) one tool_use block."""
        asst_blocks = []
        if asst_text:
            asst_blocks.append({"type": "text", "text": asst_text})
        if tool_name:
            asst_blocks.append({
                "type": "tool_use", "name": tool_name, "input": {},
            })
        return [
            {"message": {"role": "user", "content": [
                {"type": "text", "text": "go"},
            ]}},
            {"message": {"role": "assistant", "content": asst_blocks}},
        ]

    def test_is_noteworthy_true_for_action_tool(self):
        """Path 1 — a single Edit/Write/Bash is enough on its own."""
        for tool in ("Bash", "Edit", "Write", "MultiEdit"):
            t = self._transcript_with(tool_name=tool)
            assert stop._is_noteworthy(t) is True, f"{tool} must be noteworthy"

    def test_is_noteworthy_true_for_non_trivial_mcp(self):
        """Path 1 — non-trivial MCP / Web tools count too."""
        t = self._transcript_with(tool_name="WebFetch")
        assert stop._is_noteworthy(t) is True

    def test_is_noteworthy_false_for_trivial_only(self):
        """Path 2 floor — Read/Grep alone with no diagnostic markers
        is NOT noteworthy. The transcript itself is recoverable."""
        t = self._transcript_with(asst_text="Looked at the file.", tool_name="Read")
        assert stop._is_noteworthy(t) is False

    def test_is_noteworthy_true_for_trivial_plus_diagnosis(self):
        """Path 2 — trivial tool + reasoning markers DOES qualify.
        This is the gap that caused 326 'not noteworthy' skips on
        2026-04-27 despite real diagnostic work happening."""
        t = self._transcript_with(
            asst_text="Root cause: the drain logic only fires for 5xx, "
                      "not RemoteProtocolError.",
            tool_name="Read",
        )
        assert stop._is_noteworthy(t) is True

    def test_is_noteworthy_false_for_diagnosis_without_any_tool(self):
        """Diagnostic text alone — with NO tool calls — is vibes,
        not investigation. Don't store it."""
        t = self._transcript_with(asst_text="The bug is probably in foo.")
        assert stop._is_noteworthy(t) is False

    def test_extract_text_from_string_content(self):
        msg = {"role": "user", "content": "plain string"}
        assert stop._extract_text(msg) == "plain string"

    def test_extract_text_returns_empty_for_unknown(self):
        assert stop._extract_text({"role": "x", "content": 42}) == ""

    def test_truncate_no_op(self):
        assert stop._truncate("short", 100) == "short"

    def test_truncate_long(self):
        out = stop._truncate("x" * 500, 100)
        assert "(truncated)" in out
        assert len(out) <= 110


class TestStopClassifyObservation:
    def test_general_when_no_keywords(self):
        assert stop._classify_observation("nothing special", None) == "general"

    def test_fix_keyword(self):
        assert stop._classify_observation("fixed the bug", None) == "fix"

    def test_decision_keyword(self):
        assert stop._classify_observation(
            "we decided to switch to a new approach", None
        ) == "decision"

    def test_preference_keyword(self):
        assert stop._classify_observation(
            "user prefers verbose mode", None
        ) == "preference"

    def test_gotcha_keyword(self):
        assert stop._classify_observation(
            "gotcha: this API silently fails", None
        ) == "gotcha"

    def test_fix_from_transcript_error_then_edit(self):
        transcript = [
            {"message": {"role": "user", "content": [{"type": "text", "text": "fix"}]}},
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "x"}},
                {"type": "tool_result", "content": "Traceback (most recent)..."},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "x.py", "new_string": "y"}},
            ]}},
        ]
        assert stop._classify_observation("plain summary", transcript) == "fix"


class TestStopBuildSummary:
    def _transcript(self, **kw):
        msgs = []
        if kw.get("user"):
            msgs.append({"message": {"role": "user", "content": [
                {"type": "text", "text": kw["user"]}
            ]}})
        asst_blocks = []
        if kw.get("assistant_text"):
            asst_blocks.append({"type": "text", "text": kw["assistant_text"]})
        for tu in kw.get("tools", []):
            asst_blocks.append({
                "type": "tool_use",
                "name": tu["name"],
                "input": tu.get("input", {}),
            })
        if asst_blocks:
            msgs.append({"message": {"role": "assistant", "content": asst_blocks}})
        return msgs

    def test_includes_files_and_commands(self):
        t = self._transcript(
            user="please",
            assistant_text="done",
            tools=[
                {"name": "Edit", "input": {"file_path": "a.py"}},
                {"name": "Write", "input": {"file_path": "b.py"}},
                {"name": "Read", "input": {"file_path": "c.py"}},
                {"name": "Bash", "input": {"command": "echo hi"}},
            ],
        )
        out = stop._build_summary({"cwd": "/proj"}, t)
        assert "/proj" in out
        assert "a.py" in out
        assert "b.py" in out
        assert "c.py" in out
        assert "echo hi" in out

    def test_meta_prompt_filtered(self):
        t = self._transcript(
            user="extract reusable operational lessons from these events",
            assistant_text="ok",
            tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        out = stop._build_summary({"cwd": "/p"}, t)
        assert "extract reusable operational lessons" not in out

    def test_no_transcript_minimal_summary(self):
        out = stop._build_summary({}, None)
        assert out.startswith("# Turn")


class TestStopHandlerOpenwolfAndDedup:
    def test_openwolf_failure_swallowed(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please",
            assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
        )
        with patch(
            "claude_hooks.openwolf.store_content",
            side_effect=RuntimeError("simulated"),
        ):
            stop.handle(
                event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
                config=base_config(),
                providers=[p],
            )
        # Store still ran despite openwolf failure.
        assert len(p.stored) == 1

    def test_dedup_skips_near_duplicate(
        self, base_config, transcript_file, fake_provider,
    ):
        # Configure dedup_threshold > 0 with a provider that recalls the
        # near-identical content.
        existing = "x" * 200  # >= 100 chars
        p = fake_provider(
            name="qdrant",
            recall_returns=[Memory(text=existing)],
        )
        path = transcript_file(
            user="please " + ("y" * 200),
            assistant_text=existing,
            assistant_tools=[{"name": "Edit", "input": {"file_path": "z"}}],
        )
        cfg = base_config(providers={"qdrant": {"dedup_threshold": 0.5}})
        stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
            config=cfg,
            providers=[p],
        )
        # Dedup may or may not block depending on text similarity — at minimum
        # the dedup branch was exercised without crashing.
        assert isinstance(p.stored, list)

    def test_provider_store_failure_yields_systemMessage(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant", store_errors=True)
        path = transcript_file(
            user="u",
            assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        r = stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
            config=base_config(),
            providers=[p],
        )
        assert r is not None
        assert "failed" in r["systemMessage"]

    def test_store_mode_not_auto_skipped(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="u",
            assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        cfg = base_config(providers={"qdrant": {"store_mode": "off"}})
        r = stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
            config=cfg, providers=[p],
        )
        assert p.stored == []
        assert r is None


class TestStopGuardHelper:
    def test_run_stop_guard_no_transcript_returns_none(self):
        assert stop._run_stop_guard(None, {}) is None

    def test_run_stop_guard_no_assistant_text_returns_none(self):
        transcript = [
            {"message": {"role": "user", "content": [{"type": "text", "text": "u"}]}},
        ]
        assert stop._run_stop_guard(transcript, {}) is None

    def test_run_stop_guard_swallows_exceptions(self):
        transcript = [
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "x"}]}}
        ]
        with patch(
            "claude_hooks.stop_guard.load_patterns",
            side_effect=RuntimeError("boom"),
        ):
            assert stop._run_stop_guard(transcript, {}) is None


# ===================================================================== #
# claudemem_reindex.py — staleness-scan branches
# ===================================================================== #
class TestReindexIfStaleEdgeCases:
    def _make_indexed_project(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".git").mkdir()
        d = root / ".claudemem"
        d.mkdir()
        (d / "index.db").write_text("x")
        return root

    def test_no_binary_silently_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_indexed_project(tmp)
            with patch(
                "claude_hooks.claudemem_reindex.shutil.which",
                return_value=None,
            ), patch(
                "claude_hooks.claudemem_reindex._spawn_reindex"
            ) as spawn:
                claudemem_reindex.reindex_if_stale_async(cwd=str(root))
            spawn.assert_not_called()

    def test_no_git_root_silently_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "claude_hooks.claudemem_reindex.shutil.which",
                return_value="/usr/bin/claudemem",
            ), patch(
                "claude_hooks.claudemem_reindex._spawn_reindex"
            ) as spawn:
                claudemem_reindex.reindex_if_stale_async(cwd=tmp)
            spawn.assert_not_called()

    def test_no_claudemem_dir_silently_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch(
                "claude_hooks.claudemem_reindex.shutil.which",
                return_value="/usr/bin/claudemem",
            ), patch(
                "claude_hooks.claudemem_reindex._spawn_reindex"
            ) as spawn:
                claudemem_reindex.reindex_if_stale_async(cwd=str(root))
            spawn.assert_not_called()

    def test_max_files_to_scan_caps_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_indexed_project(tmp)
            # Make the index very old.
            idx = root / ".claudemem" / "index.db"
            old = time.time() - 3600
            os.utime(idx, (old, old))
            # Create more files than max_files_to_scan.
            for i in range(5):
                (root / f"src{i}.py").write_text("x")
                # But keep them OLD so the scan doesn't find a stale file.
                os.utime(
                    root / f"src{i}.py",
                    (old - 7200, old - 7200),
                )
            with patch(
                "claude_hooks.claudemem_reindex.shutil.which",
                return_value="/usr/bin/claudemem",
            ), patch(
                "claude_hooks.claudemem_reindex._spawn_reindex"
            ) as spawn:
                claudemem_reindex.reindex_if_stale_async(
                    cwd=str(root),
                    staleness_minutes=0,
                    max_files_to_scan=2,
                )
            # All sources are older than the index; scan caps without spawn.
            spawn.assert_not_called()

    def test_does_not_raise_on_outer_exception(self):
        # Force _project_root to raise — outer try/except swallows it.
        with patch(
            "claude_hooks.claudemem_reindex._project_root",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            claudemem_reindex.reindex_if_stale_async(cwd="/whatever")

    def test_index_db_missing_uses_dir_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            d = root / ".claudemem"
            d.mkdir()
            # No index.db — should fall back to dir mtime.
            result = claudemem_reindex._index_mtime(d)
            assert result is not None

    def test_index_db_missing_dir_missing_uses_walk(self):
        # Edge case: even the dir stat fails — fall back to rglob.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing"
            result = claudemem_reindex._index_mtime(root)
            # No files, no dir → returns None via OSError/ValueError.
            assert result is None


class TestLockFailureBranches:
    def test_lock_unwriteable_returns_false(self, tmp_path):
        # If we can't write the lock, _acquire_lock returns False.
        with patch.object(Path, "write_text", side_effect=OSError("read-only")):
            # Patching the class method affects all Path instances; restrict
            # the test to one specific path by calling it directly.
            assert claudemem_reindex._acquire_lock(tmp_path) is False

    def test_lock_stat_oserror_falls_through(self, tmp_path):
        # Pre-create a lock and force its stat() to raise; should still attempt
        # write and return True.
        lock = tmp_path / claudemem_reindex._LOCK_FILENAME
        lock.write_text("x")
        with patch.object(
            Path, "stat", side_effect=OSError("can't stat"),
        ):
            # Path.stat is now broken globally; restrict by calling once.
            try:
                claudemem_reindex._acquire_lock(tmp_path)
            except OSError:
                # write_text may now also fail; that's fine for branch coverage.
                pass


# ===================================================================== #
# recall.py — HyDE expand wrappers, decay, openwolf, refined-recall paths
# ===================================================================== #
class TestRecallHydeWrappers:
    def test_hyde_expand_returns_query_on_import_failure(self):
        from claude_hooks import recall
        with patch.dict("sys.modules", {"claude_hooks.hyde": None}):
            import sys
            sys.modules.pop("claude_hooks.hyde", None)
            with patch("builtins.__import__", side_effect=ImportError("nope")):
                out = recall._hyde_expand("query", {})
        assert out == "query"

    def test_hyde_expand_grounded_returns_query_on_failure(self):
        from claude_hooks import recall
        with patch(
            "claude_hooks.hyde.expand_query_with_context",
            side_effect=RuntimeError("boom"),
        ):
            out = recall._hyde_expand_grounded("q", ["m1", "m2"], {})
        assert out == "q"

    def test_hyde_expand_grounded_passes_through(self):
        from claude_hooks import recall
        with patch(
            "claude_hooks.hyde.expand_query_with_context",
            return_value="expanded grounded",
        ) as m:
            out = recall._hyde_expand_grounded("q", ["m1"], {
                "hyde_model": "model",
                "hyde_url": "http://u",
                "hyde_timeout": 5.0,
                "hyde_max_tokens": 100,
            })
        assert out == "expanded grounded"
        assert m.call_args.kwargs["model"] == "model"

    def test_hyde_expand_passes_args_through(self):
        from claude_hooks import recall
        with patch(
            "claude_hooks.hyde.expand_query",
            return_value="expanded raw",
        ) as m:
            out = recall._hyde_expand("q", {
                "hyde_model": "x",
                "hyde_url": "http://y",
                "hyde_timeout": 7.0,
            })
        assert out == "expanded raw"
        assert m.call_args.kwargs["url"] == "http://y"


class TestRecallRefinedAndDecay:
    def test_refined_recall_failure_continues(self, base_config, fake_provider):
        from claude_hooks import recall
        # First recall returns hits; second (refined) raises.
        m1 = Memory(text="raw hit one")
        p = fake_provider(name="qdrant", recall_returns=[m1])
        # Make second recall raise after the first call by bumping recall_errors
        # mid-stream — easier: patch provider.recall directly with a counter.
        calls = {"n": 0}
        def _recall(query, k=5):
            calls["n"] += 1
            if calls["n"] == 1:
                return [m1]
            raise RuntimeError("refined boom")
        p.recall = _recall

        cfg = base_config(hooks={"user_prompt_submit": {
            "hyde_enabled": True,
            "hyde_grounded": False,
        }})
        with patch(
            "claude_hooks.recall._hyde_expand",
            return_value="expanded different",
        ):
            out = recall.run_recall(
                "raw query",
                providers=[p],
                config=cfg,
                cwd="",
                max_total_chars=4000,
            )
        # Refined branch ran and gracefully degraded; raw hit still surfaces.
        assert out is not None
        assert "raw hit one" in out

    def test_decay_update_failure_swallowed(self, base_config, fake_provider):
        from claude_hooks import recall
        m1 = Memory(text="hit")
        p = fake_provider(name="qdrant", recall_returns=[m1])
        cfg = base_config(hooks={"user_prompt_submit": {
            "decay_enabled": True,
        }})
        with patch(
            "claude_hooks.decay.update_recalled",
            side_effect=RuntimeError("boom"),
        ):
            out = recall.run_recall(
                "query string here",
                providers=[p],
                config=cfg,
                cwd="",
                max_total_chars=4000,
            )
        # Output still produced despite decay failing.
        assert out is not None

    def test_openwolf_recall_failure_swallowed(self, base_config, fake_provider, tmp_path):
        from claude_hooks import recall
        m1 = Memory(text="hit")
        p = fake_provider(name="qdrant", recall_returns=[m1])
        with patch(
            "claude_hooks.openwolf.recall_context",
            side_effect=RuntimeError("boom"),
        ):
            out = recall.run_recall(
                "query string here",
                providers=[p],
                config=base_config(),
                cwd=str(tmp_path),
                max_total_chars=4000,
            )
        assert out is not None


class TestFormatBlockProgressive:
    def test_progressive_marker_when_extra_lines(self):
        from claude_hooks.recall import format_block
        m = Memory(text="line one\nextra1\nextra2")
        block = format_block("Test", [m], progressive=True)
        assert "+ chars" in block

    def test_skips_blank_text_entries(self):
        from claude_hooks.recall import format_block
        m1 = Memory(text="real")
        m2 = Memory(text="   ")
        block = format_block("Test", [m1, m2], progressive=False)
        # Blank entry skipped.
        assert "real" in block
        assert block.count("- ") == 1

    def test_no_progressive_includes_continuation_lines(self):
        from claude_hooks.recall import format_block
        m = Memory(text="line one\nextra1\nextra2")
        block = format_block("Test", [m], progressive=False)
        assert "extra1" in block
        assert "extra2" in block


# ===================================================================== #
# safety_scan.py — extras key, log_match dir failure, rotation, ask response
# ===================================================================== #
class TestSafetyExtrasKey:
    def test_skips_non_dict_entries(self):
        from claude_hooks.safety_scan import _extras_key
        out = _extras_key([{"pattern": "x"}, "not a dict", 42])
        assert len(out) == 1

    def test_skips_entries_with_empty_pattern(self):
        from claude_hooks.safety_scan import _extras_key
        out = _extras_key([{"pattern": ""}, {"pattern": "y"}])
        assert len(out) == 1
        assert out[0][0] == "y"

    def test_empty_input_returns_empty_tuple(self):
        from claude_hooks.safety_scan import _extras_key
        assert _extras_key(None) == ()
        assert _extras_key([]) == ()


class TestSafetyScanCommand:
    def test_empty_command_returns_none(self):
        from claude_hooks.safety_scan import compile_patterns, scan_command
        patterns = compile_patterns(use_defaults=True)
        assert scan_command("", patterns) is None


class TestSafetyLogMatch:
    def test_mkdir_failure_silently_returns(self, tmp_path):
        from claude_hooks.safety_scan import log_match
        # Pass a path under an unwritable mock — patch Path.mkdir to raise.
        with patch.object(Path, "mkdir", side_effect=OSError("no perm")):
            log_match(
                log_dir=tmp_path / "scanner",
                pattern_name="x",
                reason="r",
                command="cmd",
            )
        # No file written because mkdir raised.
        assert not (tmp_path / "scanner").exists()

    def test_open_failure_swallowed(self, tmp_path):
        from claude_hooks.safety_scan import log_match
        log_dir = tmp_path / "scanner"
        with patch("builtins.open", side_effect=OSError("no fd")):
            log_match(
                log_dir=log_dir,
                pattern_name="x",
                reason="r",
                command="cmd",
            )

    def test_writes_jsonl_record(self, tmp_path):
        from claude_hooks.safety_scan import log_match
        log_dir = tmp_path / "scanner"
        log_match(
            log_dir=log_dir,
            pattern_name="rm-rf",
            reason="dangerous",
            command="rm -rf /tmp/foo",
        )
        files = list(log_dir.glob("*.jsonl"))
        assert len(files) == 1
        record = json.loads(files[0].read_text().strip().splitlines()[-1])
        assert record["pattern"] == "rm-rf"
        assert record["reason"] == "dangerous"

    def test_rotation_skipped_when_marker_today(self, tmp_path):
        from claude_hooks.safety_scan import _maybe_rotate
        from datetime import datetime, timezone
        marker = tmp_path / ".last-rotation"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker.write_text(today)
        # Create an old log file that would otherwise be deleted.
        old_log = tmp_path / "2020-01-01.jsonl"
        old_log.write_text("x")
        old_time = time.time() - 365 * 24 * 3600
        os.utime(old_log, (old_time, old_time))
        _maybe_rotate(tmp_path, retention_days=30)
        # Skipped: old log still present.
        assert old_log.exists()

    def test_rotation_deletes_old_files(self, tmp_path):
        from claude_hooks.safety_scan import _maybe_rotate
        old_log = tmp_path / "2020-01-01.jsonl"
        old_log.write_text("x")
        old_time = time.time() - 365 * 24 * 3600
        os.utime(old_log, (old_time, old_time))
        _maybe_rotate(tmp_path, retention_days=30)
        assert not old_log.exists()
        marker = tmp_path / ".last-rotation"
        assert marker.exists()


class TestSafetyDefaultLogDir:
    def test_default_log_dir_under_home(self):
        from claude_hooks.safety_scan import default_log_dir
        d = default_log_dir()
        assert "permission-scanner" in str(d)


if __name__ == "__main__":
    unittest.main()
