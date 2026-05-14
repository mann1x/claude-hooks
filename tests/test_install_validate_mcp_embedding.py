"""Tests for the v1.4 validate-only helpers for qdrant + memory_kg.

Both providers embed server-side (FastEmbed inside the MCP
container for Qdrant; bundled embedder for memory_kg). claude-hooks
doesn't override their embedders from the client — these helpers
only probe connectivity + report that fact.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# --------------------------------------------------------------------- #
# qdrant
# --------------------------------------------------------------------- #

class TestValidateQdrant:
    def test_no_provider_block_is_noop(self, capsys):
        cfg: dict = {}
        install._validate_qdrant_embedding(
            cfg, non_interactive=True, dry_run=False,
        )
        assert capsys.readouterr().out == ""

    def test_disabled_is_noop(self, capsys):
        cfg = {"providers": {"qdrant": {"enabled": False, "mcp_url": "http://x"}}}
        install._validate_qdrant_embedding(
            cfg, non_interactive=True, dry_run=False,
        )
        assert capsys.readouterr().out == ""

    def test_missing_url_is_noop(self, capsys):
        cfg = {"providers": {"qdrant": {"enabled": True, "mcp_url": ""}}}
        install._validate_qdrant_embedding(
            cfg, non_interactive=True, dry_run=False,
        )
        assert capsys.readouterr().out == ""

    def test_reachable_reports_ok(self, capsys):
        cfg = {"providers": {"qdrant": {
            "enabled": True, "mcp_url": "http://h:32775/mcp",
        }}}
        with patch("claude_hooks.providers.qdrant.QdrantProvider.verify",
                   return_value=True):
            install._validate_qdrant_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "Probing http://h:32775/mcp" in out
        assert "OK" in out
        assert "FastEmbed" in out

    def test_unreachable_reports_failure(self, capsys):
        cfg = {"providers": {"qdrant": {
            "enabled": True, "mcp_url": "http://h:32775/mcp",
        }}}
        with patch("claude_hooks.providers.qdrant.QdrantProvider.verify",
                   side_effect=ConnectionRefusedError("nope")):
            install._validate_qdrant_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "FAILED" in out
        # The follow-up FastEmbed note should not appear on failure
        # (no point talking about model config when the server is
        # unreachable).
        assert "FastEmbed" not in out

    def test_no_signature_tools_reports_warning(self, capsys):
        cfg = {"providers": {"qdrant": {
            "enabled": True, "mcp_url": "http://h:32775/mcp",
        }}}
        with patch("claude_hooks.providers.qdrant.QdrantProvider.verify",
                   return_value=False):
            install._validate_qdrant_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "no signature tools" in out
        # FastEmbed note still printed — connectivity worked, just
        # the toolset didn't match (could be a non-qdrant MCP).
        assert "FastEmbed" in out

    def test_does_not_mutate_config(self):
        cfg = {"providers": {"qdrant": {
            "enabled": True, "mcp_url": "http://h:32775/mcp",
            "collection": "memory",
        }}}
        snapshot = {k: dict(v) for k, v in cfg["providers"].items()}
        with patch("claude_hooks.providers.qdrant.QdrantProvider.verify",
                   return_value=True):
            install._validate_qdrant_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        assert cfg["providers"]["qdrant"] == snapshot["qdrant"]
        # No new top-level keys either.
        assert set(cfg.keys()) == {"providers"}


# --------------------------------------------------------------------- #
# memory_kg
# --------------------------------------------------------------------- #

class TestValidateMemoryKg:
    def test_no_provider_block_is_noop(self, capsys):
        cfg: dict = {}
        install._validate_memory_kg_embedding(
            cfg, non_interactive=True, dry_run=False,
        )
        assert capsys.readouterr().out == ""

    def test_disabled_is_noop(self, capsys):
        cfg = {"providers": {"memory_kg": {"enabled": False, "mcp_url": "http://x"}}}
        install._validate_memory_kg_embedding(
            cfg, non_interactive=True, dry_run=False,
        )
        assert capsys.readouterr().out == ""

    def test_reachable_reports_ok(self, capsys):
        cfg = {"providers": {"memory_kg": {
            "enabled": True, "mcp_url": "http://h:32776/mcp",
        }}}
        with patch("claude_hooks.providers.memory_kg.MemoryKgProvider.verify",
                   return_value=True):
            install._validate_memory_kg_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "OK" in out
        assert "MCP server" in out  # note about server-side embedding

    def test_unreachable(self, capsys):
        cfg = {"providers": {"memory_kg": {
            "enabled": True, "mcp_url": "http://h:32776/mcp",
        }}}
        with patch("claude_hooks.providers.memory_kg.MemoryKgProvider.verify",
                   side_effect=OSError("network")):
            install._validate_memory_kg_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "FAILED" in out

    def test_does_not_mutate_config(self):
        cfg = {"providers": {"memory_kg": {
            "enabled": True, "mcp_url": "http://h:32776/mcp",
        }}}
        snapshot = {k: dict(v) for k, v in cfg["providers"].items()}
        with patch("claude_hooks.providers.memory_kg.MemoryKgProvider.verify",
                   return_value=True):
            install._validate_memory_kg_embedding(
                cfg, non_interactive=True, dry_run=False,
            )
        assert cfg["providers"]["memory_kg"] == snapshot["memory_kg"]


# --------------------------------------------------------------------- #
# main() wiring
# --------------------------------------------------------------------- #

class TestMainWiring:
    def test_validate_helpers_run_after_sqlite_vec(self):
        """Regression guard: validate-only helpers must run AFTER
        sqlite_vec setup (so the user has finished configuring the
        client-embed providers before we report on the MCP-backed
        ones) and BEFORE the proxy orchestrator (so the dialog
        section is contiguous)."""
        src = (REPO / "install.py").read_text(encoding="utf-8")
        i_sv = src.find("_setup_sqlite_vec_mcp(\n        cfg,")
        i_q = src.find("_validate_qdrant_embedding(\n        cfg,")
        i_kg = src.find("_validate_memory_kg_embedding(\n        cfg,")
        i_proxy = src.find("_setup_proxy_orchestrator(\n        cfg,")
        for n, v in (("sqlite_vec", i_sv), ("qdrant", i_q),
                     ("memory_kg", i_kg), ("proxy", i_proxy)):
            assert v > 0, f"main() doesn't call {n}"
        assert i_sv < i_q < i_kg < i_proxy
