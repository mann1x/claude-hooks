"""A session's mailbox identity is its session id, not a directory.

xollama registered as ``v0.34.4-xollama.1`` and opencoti as
``llamafile``: the default alias for a session id's first registration
(which also happens after ``/clear``) came from the event's ``cwd`` —
wherever the session last cd'd to — and a registration is remembered, so
their badges and Stop nudges counted an inbox nobody writes to. The MCP
tools, reading a variable Claude Code never sets, had no session id at
all. These pin the rules: registered alias (an explicit rename, or the
default given then) by session id; the project root only as the fallback
for a session id with no registration; ``cwd`` only if even that is
missing.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.test_mailbox import StoreHarness  # noqa: E402


class _Provider:
    """What store_for_provider borrows: a connection, a lock, a name."""
    name = "sqlite_vec"

    def __init__(self, db):
        self._conn = db.conn
        self._lock = db.lock

    def _ensure_ready(self):
        pass


class IdentityTests(StoreHarness):

    def setUp(self):
        super().setUp()
        self.provider = _Provider(self.db)
        self.cfg = {"hooks": {"mailbox": {"enabled": True}}}
        self.root = Path(self._tmp.name) / "xollama"
        self.sub = self.root / "release" / "v0.34.4-xollama.1"
        self.sub.mkdir(parents=True)

    def tools(self, event):
        from claude_hooks.mailbox import hook
        return hook._tools(self.cfg, [self.provider], event)

    def test_first_registration_defaults_to_the_project_root_not_cwd(self):
        from claude_hooks.mailbox import hook
        ev = {"session_id": "s1", "cwd": str(self.sub),
              "claude_project_dir": str(self.root)}
        hook.register_session(event=ev, config=self.cfg, providers=[self.provider])
        self.assertEqual(self.store.registered_alias("s1"), "xollama")

    def test_cwd_is_the_last_resort_only(self):
        ev = {"session_id": "s1", "cwd": str(self.sub)}
        self.assertEqual(self.tools(ev).alias, "v0.34.4-xollama.1")

    def test_a_registered_alias_wins_over_every_directory(self):
        """A rename is kept for the life of the session id."""
        self.store.register("s1", "my-rename", host="solidpc")
        ev = {"session_id": "s1", "cwd": str(self.sub),
              "claude_project_dir": str(self.root)}
        self.assertEqual(self.tools(ev).alias, "my-rename")

    def test_explicit_rename_file_names_a_new_registration(self):
        from claude_hooks.mailbox import hook
        (self.root / ".claude-hooks").mkdir()
        (self.root / ".claude-hooks" / "mailbox.toml").write_text('alias = "xo"\n')
        ev = {"session_id": "s2", "cwd": str(self.sub),
              "claude_project_dir": str(self.root)}
        hook.register_session(event=ev, config=self.cfg, providers=[self.provider])
        self.assertEqual(self.store.registered_alias("s2"), "xo")

    def test_mcp_tools_bind_to_claude_code_session_id(self):
        from claude_hooks.mailbox.integration import tools_for_provider
        self.store.register("live-sid", "xollama", host="solidpc")
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "live-sid",
                                            "CLAUDE_PROJECT_DIR": str(self.sub)}):
            t = tools_for_provider(self.provider)
        self.assertEqual((t.session_id, t.alias), ("live-sid", "xollama"))

    def test_mcp_fallback_is_the_project_dir(self):
        from claude_hooks.mailbox.integration import tools_for_provider
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "new-sid",
                                            "CLAUDE_PROJECT_DIR": str(self.root)}):
            self.assertEqual(tools_for_provider(self.provider).alias, "xollama")

    def test_status_line_counts_the_registered_alias(self):
        from claude_hooks import statusline
        self.store.register("s1", "my-rename", host="solidpc")
        self.store.send("my-rename", "hello", "b", from_alias="opencoti")
        self.store.send("xollama", "not mine", "b", from_alias="opencoti")
        with mock.patch("claude_hooks.dispatcher.build_providers",
                        return_value=[self.provider]):
            n = statusline.lookup_unread("s1", str(self.root), self.cfg)
        self.assertEqual(n, 1)


class RunInjectsProjectDirTests(StoreHarness):

    def test_run_copies_claude_project_dir_into_the_event(self):
        seen = []
        env = {"CLAUDE_PROJECT_DIR": "/work/xollama",
               "CLAUDE_HOOKS_DAEMON_DISABLE": "1"}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(sys, "argv", ["run.py", "Stop"]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    {"session_id": "s", "cwd": "/work/xollama/sub"}))), \
                mock.patch("claude_hooks.dispatcher.dispatch",
                           side_effect=lambda name, ev: seen.append(ev) or 0):
            import importlib
            import run as run_mod
            importlib.reload(run_mod)
            run_mod.main()
        self.assertEqual(seen[0]["claude_project_dir"], "/work/xollama")
        self.assertEqual(seen[0]["cwd"], "/work/xollama/sub")
