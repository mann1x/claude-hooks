"""Tests for the v1.9 SessionStart hook's LSP-engine branch.

The branch is gated on ``hooks.lsp_engine.enabled``. When off, the
hook must be byte-identical to v1.8 behavior (regression-safe). When
on, the hook calls ``lsp_integration.spawn_engine_safely`` for the
project, asks for a status row via ``format_session_start_status``,
appends the row to the additionalContext output, and closes the
client cleanly.

Every dependency is mocked at the ``lsp_integration`` boundary so
tests don't spawn a real daemon. Tests for the engine module live
alongside it under ``tests/test_lsp_engine_*.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.hooks import session_start  # noqa: E402


def _cfg(lsp_engine: dict | None = None) -> dict:
    """Minimal config exposing the LSP engine knobs only.

    The now-block is forced off so tests can use ``out is None`` as a
    proxy for "no parts were appended". Tests that DO want the
    status_line or now-block flow either override this here or build
    their own config dict inline.
    """
    return {
        "system": {"now_block": {"enabled": False}},
        "hooks": {
            "session_start": {"enabled": True, "show_status_line": False},
            # Disable code_graph so we don't have to mock its imports
            # in every test.
            "code_graph": {"enabled": False},
            "claudemem_reindex": {"enabled": False},
            "lsp_engine": lsp_engine or {"enabled": False},
        },
    }


def _event(session_id: str | None = "sess-abc", cwd: str = "/tmp") -> dict:
    e: dict = {"cwd": cwd}
    if session_id is not None:
        e["session_id"] = session_id
    return e


# --------------------------------------------------------------------- #
# Default-off (v1.8 regression-safe)
# --------------------------------------------------------------------- #

class TestDisabledByDefault:
    def test_engine_block_absent_when_disabled(self):
        with patch("claude_hooks.lsp_integration.spawn_engine_safely") as spawn:
            out = session_start.handle(
                event=_event(),
                config=_cfg(),
                providers=[],
            )
        spawn.assert_not_called()
        # No engine -> no row. Status_line off, code_graph off, providers
        # empty -> nothing to return at all.
        assert out is None

    def test_engine_explicit_false_does_not_spawn(self):
        with patch("claude_hooks.lsp_integration.spawn_engine_safely") as spawn:
            session_start.handle(
                event=_event(),
                config=_cfg({"enabled": False}),
                providers=[],
            )
        spawn.assert_not_called()


# --------------------------------------------------------------------- #
# Enabled — happy path
# --------------------------------------------------------------------- #

class TestEnabledHappyPath:
    def test_spawn_and_status_row_added(self):
        fake_client = MagicMock(name="LspEngineClient")
        fake_client.status.return_value = {
            "active_servers": [["pyright-langserver", "--stdio"], ["gopls"]],
            "sessions": ["sess-abc"],
        }
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=fake_client,
        ) as spawn:
            out = session_start.handle(
                event=_event(),
                config=_cfg({
                    "enabled": True,
                    "spawn_on_session_start": True,
                }),
                providers=[MagicMock(name="p", display_name="qdrant")],
            )

        spawn.assert_called_once()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "LSP engine: running" in ctx
        assert "pyright-langserver" in ctx
        assert "gopls" in ctx
        assert "1 session" in ctx
        fake_client.close.assert_called_once()

    def test_session_id_pulled_from_event(self):
        fake_client = MagicMock()
        fake_client.status.return_value = {
            "active_servers": ["pyright"],
            "sessions": ["sess-xyz"],
        }
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=fake_client,
        ) as spawn:
            session_start.handle(
                event=_event(session_id="sess-xyz"),
                config=_cfg({"enabled": True}),
                providers=[MagicMock(display_name="x")],
            )
        kwargs = spawn.call_args.kwargs
        assert kwargs["session_id"] == "sess-xyz"

    def test_session_id_fallback_when_event_missing_field(self):
        fake_client = MagicMock()
        fake_client.status.return_value = {
            "active_servers": ["pyright"],
            "sessions": [],
        }
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=fake_client,
        ) as spawn:
            session_start.handle(
                event=_event(session_id=None),
                config=_cfg({"enabled": True}),
                providers=[MagicMock(display_name="x")],
            )
        kwargs = spawn.call_args.kwargs
        # The fallback shape is ``hook-<pid>-<ms>``.
        assert kwargs["session_id"].startswith("hook-")


# --------------------------------------------------------------------- #
# Soft-fail posture
# --------------------------------------------------------------------- #

class TestSoftFail:
    def test_spawn_failure_returns_none_block(self):
        """spawn_engine_safely already returns None on failure; the
        hook must accept that and produce no LSP row, not raise."""
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=None,
        ) as spawn:
            out = session_start.handle(
                event=_event(),
                config=_cfg({"enabled": True}),
                providers=[MagicMock(display_name="qdrant")],
            )
        spawn.assert_called_once()
        # With providers + show_status_line off, no parts to return.
        assert out is None

    def test_zero_servers_suppresses_row(self):
        """When the engine spawns but no LSPs are configured (empty
        cclsp.json), we suppress the row instead of printing
        '(0 servers)' which would read like a problem."""
        fake_client = MagicMock()
        fake_client.status.return_value = {"active_servers": [], "sessions": []}
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=fake_client,
        ):
            out = session_start.handle(
                event=_event(),
                config=_cfg({"enabled": True}),
                providers=[MagicMock(display_name="qdrant")],
            )
        # show_status_line was off in the fixture, so with no LSP row
        # and no other parts, the hook returns None.
        assert out is None

    def test_status_row_block_unaffected_when_status_rpc_raises(self):
        """If status() raises after spawn, format helper returns None
        and the hook keeps the rest of its output intact."""
        fake_client = MagicMock()
        fake_client.status.side_effect = RuntimeError("boom")
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=fake_client,
        ):
            out = session_start.handle(
                event=_event(),
                config={
                    "system": {"now_block": {"enabled": False}},
                    "hooks": {
                        "session_start": {"enabled": True, "show_status_line": True},
                        "code_graph": {"enabled": False},
                        "claudemem_reindex": {"enabled": False},
                        "lsp_engine": {"enabled": True},
                    },
                },
                providers=[MagicMock(display_name="qdrant")],
            )
        # show_status_line on => status line should still flow even
        # when LSP fails.
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "qdrant" in ctx
        assert "LSP engine" not in ctx
        fake_client.close.assert_called_once()


# --------------------------------------------------------------------- #
# spawn_on_session_start toggle
# --------------------------------------------------------------------- #

class TestSpawnOnSessionStart:
    def test_disabled_skips_spawn(self):
        """When spawn_on_session_start is False, SessionStart skips
        the spawn entirely (PostToolUse handles lazy-spawn)."""
        with patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
        ) as spawn:
            session_start.handle(
                event=_event(),
                config=_cfg({"enabled": True, "spawn_on_session_start": False}),
                providers=[MagicMock(display_name="x")],
            )
        spawn.assert_not_called()
