"""Only the session someone is working in gets the hooks.

A `claude -p` run spawned by a tool (Caliber's pre-commit refresh runs
one in the repo on every commit) took the project's mailbox alias, got
its unread mail with a Stop nudge, read it, and stored its turns as the
operator's. These pin the gate that keeps every hook out of such runs.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import session_kind  # noqa: E402

# Measured on Claude Code 2.1.280: the parent's own tool shell and the
# SessionStart hook of a `claude -p` it spawned.
INTERACTIVE = {"CLAUDE_CODE_ENTRYPOINT": "cli",
               "CLAUDE_CODE_SESSION_ATTENDED": "1",
               "CLAUDE_CODE_CHILD_SESSION": "1"}
SPAWNED = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli",
           "CLAUDE_CODE_SESSION_ATTENDED": "0",
           "CLAUDE_CODE_CHILD_SESSION": "1"}


class TestPredicate:
    def test_interactive_session_is_not_a_subprocess(self):
        assert not session_kind.is_subprocess(INTERACTIVE)

    def test_spawned_claude_p_is(self):
        assert session_kind.subprocess_reason(SPAWNED) == "CLAUDE_CODE_SESSION_ATTENDED=0"

    @pytest.mark.parametrize("entry", ["sdk-cli", "sdk-ts", "sdk-py", "SDK-CLI"])
    def test_sdk_entrypoints_alone_are_enough(self, entry):
        assert session_kind.is_subprocess({"CLAUDE_CODE_ENTRYPOINT": entry})

    def test_unattended_alone_is_enough(self):
        assert session_kind.is_subprocess({"CLAUDE_CODE_SESSION_ATTENDED": "0"})

    def test_child_session_flag_is_not_a_signal(self):
        """The parent exports it to its own tool shells too."""
        assert not session_kind.is_subprocess({"CLAUDE_CODE_CHILD_SESSION": "1"})

    def test_nothing_set_counts_as_interactive(self):
        """Older Claude Code / other clients keep working."""
        assert not session_kind.is_subprocess({})

    def test_config_can_turn_the_gate_off(self):
        assert not session_kind.hooks_allowed({}, SPAWNED)
        assert session_kind.hooks_allowed(
            {"hooks": {"run_in_subprocesses": True}}, SPAWNED)
        assert session_kind.hooks_allowed({}, INTERACTIVE)


class TestRunEntryPoint:
    def _run(self, monkeypatch, env, dispatched):
        for k in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("CLAUDE_HOOKS_DAEMON_DISABLE", "1")
        monkeypatch.setattr(sys, "argv", ["run.py", "Stop"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s"})))
        import importlib
        import run as run_mod
        importlib.reload(run_mod)
        with mock.patch("claude_hooks.dispatcher.dispatch",
                        side_effect=lambda *a, **k: dispatched.append(a) or 0):
            return run_mod.main()

    def test_spawned_run_never_reaches_the_dispatcher(self, monkeypatch):
        dispatched: list = []
        assert self._run(monkeypatch, SPAWNED, dispatched) == 0
        assert dispatched == []
        assert sys.stdin.read() == "", "stdin is drained"

    def test_interactive_run_is_dispatched(self, monkeypatch):
        dispatched: list = []
        self._run(monkeypatch, INTERACTIVE, dispatched)
        assert dispatched and dispatched[0][0] == "Stop"


class TestMailboxInASpawnedRun:
    @pytest.fixture
    def tools(self, tmp_path):
        import sqlite3
        import threading
        from claude_hooks.mailbox.store import MailboxStore
        from claude_hooks.mailbox.tools import MailboxTools
        conn = sqlite3.connect(str(tmp_path / "m.db"), check_same_thread=False)
        store = MailboxStore(lambda: conn, threading.RLock(), dialect="sqlite")
        store.ensure_schema()
        store.register("real-session", "xollama", host="solidpc")
        store.send("xollama@solidpc", "for the real session", "body",
                   from_alias="opencoti")
        yield store, MailboxTools(store, alias="xollama",
                                  session_id="caliber-run", host="solidpc")
        conn.close()

    def test_read_and_ack_are_refused_and_nothing_is_marked(self, tools, monkeypatch):
        store, t = tools
        for k, v in SPAWNED.items():
            monkeypatch.setenv(k, v)
        mid = store.inbox(alias="xollama", host="solidpc")[0]["id"]
        assert "not available in a non-interactive run" in t.call("mailbox-read", {"ids": [mid]})
        assert "not available" in t.call("mailbox-ack", {"id": mid, "note": "x"})
        assert store.inbox(alias="xollama", host="solidpc")[0]["read_at"] is None

    def test_a_spawned_run_does_not_take_over_the_registration(self, tools, monkeypatch):
        store, t = tools
        for k, v in SPAWNED.items():
            monkeypatch.setenv(k, v)
        t.call("mailbox-list", {})
        assert store.registered_alias("real-session") == "xollama"
        assert store.registered_alias("caliber-run") is None

    def test_hook_side_is_off(self, monkeypatch):
        from claude_hooks.mailbox import hook
        cfg = {"hooks": {"mailbox": {"enabled": True}}}
        assert hook._enabled(cfg)
        for k, v in SPAWNED.items():
            monkeypatch.setenv(k, v)
        assert not hook._enabled(cfg)
