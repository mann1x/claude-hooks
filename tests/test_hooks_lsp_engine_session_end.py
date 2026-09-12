"""Tests for the v1.9 SessionEnd hook's LSP-engine detach branch.

The branch lives behind ``hooks.lsp_engine.enabled`` AND
``hooks.lsp_engine.detach_on_session_end``. When either is off, the
hook leaves the daemon untouched. When both are on, the hook tries
to open a non-spawning client against the daemon's existing socket
and fires a ``detach`` op.

Detach is courtesy: even if it fails, affinity locks expire on the
debounce timer the daemon enforces internally. So every branch in
this test surface boils down to "no exception bubbles up, no
session_end semantics broken".
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.hooks import session_end  # noqa: E402


def _cfg(lsp: dict | None = None, episodic_mode: str = "off") -> dict:
    return {
        "hooks": {
            "session_end": {"enabled": True},
            "lsp_engine": lsp or {"enabled": False},
        },
        "episodic": {"mode": episodic_mode},
    }


def _event(session_id: str | None = "sess-A", cwd: str = "/tmp") -> dict:
    e: dict = {"cwd": cwd}
    if session_id is not None:
        e["session_id"] = session_id
    return e


# --------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------- #

class TestGating:
    def test_disabled_skips_detach(self):
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc:
            session_end.handle(
                event=_event(),
                config=_cfg(),
                providers=[],
            )
        oc.assert_not_called()

    def test_detach_on_session_end_false_skips(self):
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc:
            session_end.handle(
                event=_event(),
                config=_cfg({
                    "enabled": True,
                    "detach_on_session_end": False,
                }),
                providers=[],
            )
        oc.assert_not_called()

    def test_no_session_id_skips_detach(self):
        """Without a real session_id, the fallback would not match the
        ID SessionStart attached with — detaching the wrong session is
        worse than not detaching at all. Skip silently."""
        with patch("claude_hooks.lsp_integration.open_client_safely") as oc:
            session_end.handle(
                event=_event(session_id=None),
                config=_cfg({"enabled": True}),
                providers=[],
            )
        oc.assert_not_called()


# --------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------- #

class TestDetachHappyPath:
    def test_detach_when_engine_enabled(self):
        client = MagicMock(name="LspEngineClient")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ) as oc:
            session_end.handle(
                event=_event(session_id="sess-A"),
                config=_cfg({"enabled": True}),
                providers=[],
            )
        oc.assert_called_once()
        client.detach.assert_called_once()
        client.close.assert_called_once()

    def test_detach_session_id_round_trips_to_open_client(self):
        client = MagicMock()
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ) as oc:
            session_end.handle(
                event=_event(session_id="my-unique-sid"),
                config=_cfg({"enabled": True}),
                providers=[],
            )
        kwargs = oc.call_args.kwargs
        assert kwargs["session_id"] == "my-unique-sid"


# --------------------------------------------------------------------- #
# Soft-fail
# --------------------------------------------------------------------- #

class TestSoftFail:
    def test_socket_already_gone_silent_noop(self):
        """When the daemon was already reaped (socket missing),
        open_client_safely returns None. SessionEnd must accept that
        without raising."""
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=None,
        ):
            # No exception expected
            session_end.handle(
                event=_event(),
                config=_cfg({"enabled": True}),
                providers=[],
            )

    def test_detach_rpc_raises_client_still_closed(self):
        client = MagicMock()
        client.detach.side_effect = RuntimeError("ipc closed mid-op")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            # No exception bubbles out of the hook.
            session_end.handle(
                event=_event(),
                config=_cfg({"enabled": True}),
                providers=[],
            )
        client.close.assert_called_once()

    def test_close_raise_swallowed(self):
        client = MagicMock()
        client.close.side_effect = OSError("ehh")
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ):
            # Even if close itself raises, the hook returns cleanly.
            session_end.handle(
                event=_event(),
                config=_cfg({"enabled": True}),
                providers=[],
            )


# --------------------------------------------------------------------- #
# Coexistence with episodic
# --------------------------------------------------------------------- #

class TestEpisodicCoexists:
    def test_detach_runs_before_episodic_push(self):
        """LSP detach is order-independent from episodic, but it must
        run regardless of episodic mode."""
        client = MagicMock()
        with patch(
            "claude_hooks.lsp_integration.open_client_safely",
            return_value=client,
        ) as oc, patch(
            "claude_hooks.hooks.session_end._push_transcript",
            return_value=None,
        ) as push:
            session_end.handle(
                event=_event(),
                config=_cfg({"enabled": True}, episodic_mode="client"),
                providers=[],
            )
        oc.assert_called_once()
        client.detach.assert_called_once()
        # Episodic push still ran.
        push.assert_called_once()
