"""Tests for the v1.9 PostToolUse hook's LSP-engine branch.

The branch lives behind ``hooks.lsp_engine.enabled``. When off, the
hook behaves exactly like v1.8 — only ruff + the TOML advisor run.
When on, for every edited file PostToolUse:

1. Resolves the project's daemon socket via
   ``lsp_integration.open_client_safely``.
2. If the socket is gone, spawns the daemon lazily via
   ``spawn_engine_safely`` (PostToolUse's fallback path — slower
   but correct).
3. Sends ``did_open`` + ``did_change`` + ``diagnostics``.
4. Builds a markdown block via ``format_diagnostics_block`` and
   appends it to the existing ``additionalContext`` alongside any
   ruff output.

All engine calls are mocked at the ``lsp_integration`` boundary to
keep the tests deterministic and fast.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.hooks import post_tool_use  # noqa: E402


def _event(*, tool_name: str = "Edit", file_path: str = "x.py",
           cwd: str = "", session_id: str = "sess-A") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "cwd": cwd,
        "session_id": session_id,
    }


def _cfg(*, lsp: dict | None = None, ruff: bool = False, **post_overrides) -> dict:
    """Build a config with LSP engine knobs and ruff off by default so
    a single block in additionalContext is unambiguously the LSP one."""
    post_hook = {
        "enabled": True,
        "ruff_enabled": ruff,
        "toml_comment_advisor_enabled": False,
    }
    post_hook.update(post_overrides)
    return {
        "hooks": {
            "post_tool_use": post_hook,
            "lsp_engine": lsp or {"enabled": False},
        },
    }


def _fake_client(
    diagnostics: list[dict] | None = None,
    stale: bool = False,
) -> MagicMock:
    """Build an LspEngineClient mock that returns ``diagnostics`` from
    its ``diagnostics()`` call. ``did_open`` returns True, ``did_change``
    returns ``(True, None)``."""
    c = MagicMock(name="LspEngineClient")
    c.did_open.return_value = True
    c.did_change.return_value = (True, None)
    c.diagnostics.return_value = (diagnostics or [], stale)
    return c


# --------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------- #

class TestGating:
    def test_disabled_no_engine_call(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc, \
             patch("claude_hooks.lsp_integration.spawn_engine_safely") as sp:
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(),
                providers=[],
            )
        oc.assert_not_called()
        sp.assert_not_called()
        assert out is None

    def test_non_editing_tool_skipped(self, tmp_path: Path):
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc:
            post_tool_use.handle(
                event=_event(tool_name="Bash", file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        oc.assert_not_called()

    def test_blacklisted_extension_skipped(self, tmp_path: Path):
        (tmp_path / "x.md").write_text("# hi\n")
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc:
            post_tool_use.handle(
                event=_event(file_path="x.md", cwd=str(tmp_path)),
                config=_cfg(lsp={
                    "enabled": True,
                    "extensions_blacklist": ["md", "toml"],
                }),
                providers=[],
            )
        oc.assert_not_called()


# --------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------- #

class TestHappyPath:
    def test_python_edit_fires_did_change_and_diagnostics(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client(diagnostics=[{
            "line": 0, "character": 0, "severity": 2,
            "message": "Type of \"x\" is partially unknown",
            "source": "pyright", "code": "reportUnknown",
        }])
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )

        client.did_open.assert_called_once()
        client.did_change.assert_called_once()
        client.diagnostics.assert_called_once()
        client.close.assert_called_once()
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "LSP diagnostics" in ctx
        assert "x.py" in ctx
        assert "pyright" in ctx
        assert "warning" in ctx  # severity 2

    def test_empty_diagnostics_block_omitted(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client(diagnostics=[])
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        # No diagnostics → no block; ruff off → no other block.
        assert out is None
        # Still detached
        client.close.assert_called_once()

    def test_stale_annotation_appended(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client(
            diagnostics=[{
                "line": 0, "character": 0, "severity": 1,
                "message": "boom", "source": "pyright",
            }],
            stale=True,
        )
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "(stale: another session holds the affinity lock" in ctx

    def test_session_id_pulled_from_event(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client()
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ) as oc:
            post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path),
                             session_id="my-session"),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        kwargs = oc.call_args.kwargs
        assert kwargs["session_id"] == "my-session"


# --------------------------------------------------------------------- #
# Lazy spawn fallback
# --------------------------------------------------------------------- #

class TestLazySpawn:
    def test_daemon_down_triggers_spawn(self, tmp_path: Path):
        """When open_client_safely returns None (no socket), the hook
        falls back to spawn_engine_safely so PostToolUse still works
        on a fresh project where SessionStart didn't pre-spawn."""
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client(diagnostics=[{
            "line": 5, "character": 0, "severity": 1,
            "message": "err", "source": "pyright",
        }])
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=None,
        ), patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=client,
        ) as sp:
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        sp.assert_called_once()
        assert out is not None
        assert "LSP diagnostics" in out["hookSpecificOutput"]["additionalContext"]

    def test_both_open_and_spawn_fail_block_omitted(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=None,
        ), patch(
            "claude_hooks.lsp_integration.spawn_engine_safely",
            return_value=None,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        assert out is None


# --------------------------------------------------------------------- #
# Errors during IPC
# --------------------------------------------------------------------- #

class TestIpcErrors:
    def test_diagnostics_raises_block_omitted_client_still_closed(
            self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = MagicMock()
        client.did_open.return_value = True
        client.did_change.return_value = (True, None)
        client.diagnostics.side_effect = RuntimeError("ipc timeout")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        assert out is None
        client.close.assert_called_once()

    def test_did_change_raises_block_omitted(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        client = MagicMock()
        client.did_open.return_value = True
        client.did_change.side_effect = OSError("broken pipe")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        assert out is None
        client.close.assert_called_once()

    def test_unreadable_file_block_omitted(self, tmp_path: Path):
        """If the edited file disappears before PostToolUse runs, the
        hook should not crash. ``isfile`` check above already filters
        most cases — this guards against TOCTOU between that check
        and the open() inside _run_lsp_engine."""
        (tmp_path / "x.py").write_text("x = 1\n")
        client = _fake_client()
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ), patch("builtins.open", side_effect=OSError("vanished")):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}),
                providers=[],
            )
        # Block omitted but no exception thrown.
        assert out is None
        # IPC was never called because we never got past file read.
        client.did_open.assert_not_called()
        client.close.assert_called_once()


# --------------------------------------------------------------------- #
# Co-existence with ruff
# --------------------------------------------------------------------- #

class TestMergesWithRuff:
    def test_both_blocks_appear_when_both_fire(self, tmp_path: Path):
        """The two layers stack: ruff produces its block, then the LSP
        engine produces its own, joined by a blank line. Important
        regression guard — neither layer's existence should hide the
        other."""
        py = tmp_path / "x.py"
        py.write_text("import os\nx=1\n")  # ruff E225 / F401
        client = _fake_client(diagnostics=[{
            "line": 1, "character": 0, "severity": 1,
            "message": "type error", "source": "pyright",
        }])

        # Mock ruff to produce a known block instead of running the
        # real binary.
        with patch(
            "claude_hooks.hooks.post_tool_use._run_ruff",
            return_value="## Ruff diagnostics — `x.py`\n\n```\nx.py:2:2: E225\n```",
        ), patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(lsp={"enabled": True}, ruff=True),
                providers=[],
            )
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Ruff diagnostics" in ctx
        assert "LSP diagnostics" in ctx
        # Order: ruff block first, then LSP block (matches handler order).
        assert ctx.index("Ruff diagnostics") < ctx.index("LSP diagnostics")
