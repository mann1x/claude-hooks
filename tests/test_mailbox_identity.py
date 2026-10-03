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


# ─── the session's name, and two sessions in one directory ──────────────

import os  # noqa: E402

from claude_hooks.lsp_engine.daemon import process_start_time  # noqa: E402


def _me():
    """This test process, as a live Claude Code client."""
    return (os.getpid(), process_start_time(os.getpid()))


def _dead():
    """A client whose process has ended: our pid, a start time it never had."""
    return (os.getpid(), 1000.0)


class SessionNameTests(StoreHarness):

    def transcript(self, sid, project, *records):
        from claude_hooks.mailbox.identity import project_slug
        d = self.claude_dir / "projects" / project_slug(project)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in records))
        return p

    def test_latest_custom_title_wins(self):
        from claude_hooks.mailbox.identity import session_name
        self.transcript("s1", "/w/opencoti",
                        {"type": "custom-title", "customTitle": "first"},
                        {"type": "user", "message": "x"},
                        {"type": "custom-title", "customTitle": "opencoti mac"})
        self.assertEqual(session_name("s1", project_dir="/w/opencoti"),
                         "opencoti-mac")

    def test_generated_agent_name_is_not_a_name(self):
        """xollama: three /rename never persisted, agent-name carried the
        generated title. That must not become the alias."""
        from claude_hooks.mailbox.identity import session_name
        self.transcript("s1", "/w/xollama",
                        {"type": "ai-title", "aiTitle": "project-tracking"},
                        {"type": "agent-name", "agentName": "project-tracking"})
        self.assertIsNone(session_name("s1", project_dir="/w/xollama"))

    def test_history_rename_is_the_fallback(self):
        from claude_hooks.mailbox.identity import session_name
        (self.claude_dir / "history.jsonl").write_text(
            json.dumps({"display": "/rename other", "sessionId": "s9"}) + "\n"
            + json.dumps({"display": "/rename opencoti-mac",
                          "sessionId": "s1"}) + "\n"
            + json.dumps({"display": "hello", "sessionId": "s1"}) + "\n")
        self.assertEqual(session_name("s1"), "opencoti-mac")

    def test_found_by_session_id_in_any_project(self):
        from claude_hooks.mailbox.identity import session_name
        self.transcript("s1", "/w/a",
                        {"type": "custom-title", "customTitle": "named"})
        self.assertEqual(session_name("s1"), "named")

    def test_reserved_characters_are_cleaned(self):
        from claude_hooks.mailbox.identity import clean_name
        self.assertEqual(clean_name(" a b@c*d,e "), "a-b-c-d-e")


class ClaimTests(StoreHarness):

    def test_a_live_holder_keeps_its_name(self):
        self.store.claim("old", "opencoti", client=_me())
        res = self.store.claim("new", "opencoti", client=(1, 2.0),
                               is_alive=lambda row: True)
        self.assertEqual(res["alias"], "opencoti-2")
        self.assertEqual(self.store.registered_alias("old"), "opencoti")

    def test_numbers_continue_past_held_ones(self):
        alive = lambda row: True  # noqa: E731
        for sid in ("a", "b"):
            self.store.claim(sid, "x", is_alive=alive)
        self.assertEqual(self.store.claim("c", "x", is_alive=alive)["alias"],
                         "x-3")

    def test_an_ended_holder_is_replaced(self):
        self.store.claim("old", "opencoti", client=_dead())
        res = self.store.claim("new", "opencoti", client=(1, 2.0))
        self.assertEqual(res["alias"], "opencoti")
        self.assertIsNone(self.store.registered_alias("old"))

    def test_the_same_process_inherits_after_clear(self):
        self.store.claim("before-clear", "opencoti", client=(7, 100.0))
        res = self.store.claim("after-clear", "opencoti", client=(7, 100.5),
                               is_alive=lambda row: True)
        self.assertEqual(res["alias"], "opencoti")

    def test_a_legacy_row_is_judged_by_last_seen(self):
        self.store.register("legacy", "opencoti")      # no process recorded
        res = self.store.claim("new", "opencoti", client=(1, 2.0))
        self.assertEqual(res["alias"], "opencoti-2")

    def test_rename_moves_only_mail_received_while_held(self):
        from datetime import timedelta
        from claude_hooks.mailbox import store as store_mod
        early = store_mod.utcnow() - timedelta(minutes=5)
        with mock.patch.object(store_mod, "utcnow", return_value=early):
            self.store.send("opencoti", "meant for the old one", "b",
                            from_alias="xollama")
        self.store.claim("s1", "opencoti", client=_me())
        self.store.send("opencoti", "mine", "b", from_alias="xollama")
        res = self.store.claim("s1", "opencoti-mac", client=_me())
        self.assertEqual((res["previous"], res["alias"], res["moved"]),
                         ("opencoti", "opencoti-mac", 1))
        mine = [m.subject if hasattr(m, "subject") else m["subject"]
                for m in self.store.inbox(alias="opencoti-mac")]
        left = [m.subject if hasattr(m, "subject") else m["subject"]
                for m in self.store.inbox(alias="opencoti")]
        self.assertEqual((mine, left), (["mine"], ["meant for the old one"]))

    def test_a_cloud_registration_still_takes_its_alias(self):
        """The relay asks for an alias by name; register() keeps that."""
        self.store.claim("a", "osync", host="cloud", is_alive=lambda r: True)
        self.store.register("b", "osync", host="cloud")
        self.assertEqual(self.store.registered_alias("b"), "osync")


class FollowsTheNameTests(IdentityTests):
    """End to end through the hook and the MCP binding."""

    def setUp(self):
        super().setUp()
        self.names = {}
        p = mock.patch("claude_hooks.mailbox.identity.session_name",
                       side_effect=lambda sid, **kw: self.names.get(sid))
        p.start()
        self.addCleanup(p.stop)

    def ev(self, sid, client):
        return {"session_id": sid, "cwd": str(self.root),
                "claude_project_dir": str(self.root),
                "claude_client": list(client)}

    def test_second_live_session_in_a_folder_gets_a_number(self):
        from claude_hooks.mailbox import hook
        first = hook.register_session(event=self.ev("s1", _me()),
                                      config=self.cfg, providers=[self.provider])
        second = hook.register_session(event=self.ev("s2", (1, 2.0)),
                                       config=self.cfg, providers=[self.provider])
        self.assertEqual(self.store.registered_alias("s1"), "xollama")
        self.assertEqual(self.store.registered_alias("s2"), "xollama-2")
        self.assertIn("you are `xollama@solidpc`", first)
        self.assertIn("`xollama-2@solidpc`", second)
        self.assertIn("another live session", second)

    def test_the_badge_of_a_second_session_is_its_own(self):
        from claude_hooks import statusline
        from claude_hooks.mailbox import hook
        hook.register_session(event=self.ev("s1", _me()), config=self.cfg,
                              providers=[self.provider])
        self.store.send("xollama", "for s1", "b", from_alias="opencoti")
        with mock.patch.dict("os.environ", {"CLAUDE_HOOKS_CLIENT_PID": "1"}), \
                mock.patch("claude_hooks.dispatcher.build_providers",
                           return_value=[self.provider]):
            n = statusline.lookup_unread("s2", str(self.root), self.cfg)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.registered_alias("s1"), "xollama")

    def test_a_name_wins_over_the_directory(self):
        from claude_hooks.mailbox import hook
        self.names["s1"] = "xollama-mac"
        hook.register_session(event=self.ev("s1", _me()), config=self.cfg,
                              providers=[self.provider])
        self.assertEqual(self.store.registered_alias("s1"), "xollama-mac")

    def test_a_rename_reaches_the_next_tool_call(self):
        from claude_hooks.mailbox.integration import tools_for_provider
        env = {"CLAUDE_CODE_SESSION_ID": "s1",
               "CLAUDE_PROJECT_DIR": str(self.root)}
        with mock.patch.dict("os.environ", env):
            t = tools_for_provider(self.provider, client=_me())
            self.assertEqual(t.alias, "xollama")
            self.store.send("xollama", "hello", "b", from_alias="opencoti")
            self.names["s1"] = "xo-mac"
            out = t.call("mailbox-list", {})
        self.assertEqual(t.alias, "xo-mac")
        self.assertIn("renamed `xollama@solidpc` → `xo-mac@solidpc`", out)
        self.assertIn("hello", out)

    def test_mcp_after_clear_adopts_the_new_session(self):
        """The MCP child's env still names the session before /clear."""
        from claude_hooks.mailbox.integration import tools_for_provider
        me = _me()
        self.store.claim("after-clear", "xollama", client=me)
        env = {"CLAUDE_CODE_SESSION_ID": "before-clear",
               "CLAUDE_PROJECT_DIR": str(self.root)}
        with mock.patch.dict("os.environ", env):
            t = tools_for_provider(self.provider, client=me)
        self.assertEqual((t.session_id, t.alias), ("after-clear", "xollama"))
        self.assertIsNone(self.store.registered_alias("before-clear"))


class RegistryMigrationTests(StoreHarness):

    def test_an_old_registry_gains_the_process_columns(self):
        from claude_hooks.mailbox.store import MailboxStore
        conn = self.db.conn
        conn.execute("DROP TABLE session_registry")
        conn.execute("CREATE TABLE session_registry (session_id TEXT PRIMARY "
                     "KEY, alias TEXT NOT NULL, host TEXT NOT NULL, os TEXT "
                     "NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', "
                     "started_at TEXT NOT NULL, last_seen TEXT NOT NULL)")
        conn.commit()
        st = MailboxStore(self.db, self.db.lock, dialect="sqlite")
        st.ensure_schema()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_registry)")}
        self.assertTrue({"client_pid", "client_started"} <= cols)
        st.claim("s1", "x", client=(5, 6.0))
        self.assertEqual(st.registration("s1")["client_pid"], 5)


class ClientProcessTests(StoreHarness):

    def test_forced_pid_reports_its_start_time(self):
        from claude_hooks.mailbox.identity import client_process
        with mock.patch.dict("os.environ",
                             {"CLAUDE_HOOKS_CLIENT_PID": str(os.getpid())}):
            self.assertEqual(client_process(), _me())

    def test_zero_means_unknown(self):
        from claude_hooks.mailbox.identity import client_process
        self.assertIsNone(client_process())

    def test_the_walk_stops_at_claude(self):
        from claude_hooks.mailbox import identity
        chain = [(30, "bash", "bash -c x"), (20, "claude", "claude"),
                 (10, "tmux", "tmux")]
        with mock.patch.dict("os.environ"), \
                mock.patch.object(identity, "_ancestors_linux",
                                  return_value=iter(chain)), \
                mock.patch.object(identity, "_ancestors_windows",
                                  return_value=iter(chain)), \
                mock.patch("claude_hooks._proc.process_start_time",
                           return_value=42.0):
            os.environ.pop("CLAUDE_HOOKS_CLIENT_PID", None)
            self.assertEqual(identity.client_process(), (20, 42.0))

    def test_node_launched_claude_is_recognised(self):
        from claude_hooks.mailbox.identity import _is_claude
        self.assertTrue(_is_claude(
            "node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"))
        self.assertTrue(_is_claude("claude.exe"))
        self.assertFalse(_is_claude("bash", "bash -c claude-hook Stop"))


_PG_DSN = os.environ.get("CLAUDE_HOOKS_TEST_PG_DSN", "")


import unittest  # noqa: E402


@unittest.skipUnless(_PG_DSN, "set CLAUDE_HOOKS_TEST_PG_DSN to run against "
                              "Postgres (a throwaway schema is used)")
class PostgresClaimTests(unittest.TestCase):
    """claim() and the column migration in the Postgres dialect."""

    def setUp(self):
        import threading
        import uuid
        import psycopg
        from claude_hooks.mailbox.store import MailboxStore
        self.conn = psycopg.connect(_PG_DSN)
        self.schema = f"mailbox_test_{uuid.uuid4().hex[:8]}"
        with self.conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {self.schema}")
            cur.execute(f"SET search_path TO {self.schema}")
        self.conn.commit()
        self.addCleanup(self._drop)
        self.store = MailboxStore(lambda: self.conn, threading.RLock(),
                                  dialect="postgres")
        env = mock.patch.dict("os.environ", {"CLAUDE_HOOKS_HOST": "solidpc"})
        env.start()
        self.addCleanup(env.stop)

    def _drop(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.commit()
        self.conn.close()

    def test_old_registry_is_migrated_then_claimed(self):
        with self.conn.cursor() as cur:
            cur.execute("CREATE TABLE session_registry (session_id TEXT "
                        "PRIMARY KEY, alias TEXT NOT NULL, host TEXT NOT "
                        "NULL, os TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL "
                        "DEFAULT '', started_at TIMESTAMPTZ NOT NULL DEFAULT "
                        "now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now())")
        self.conn.commit()
        self.store.ensure_schema()
        self.store.claim("old", "opencoti", client=_me())
        self.store.send("opencoti", "mine", "b", from_alias="xollama")
        res = self.store.claim("new", "opencoti", client=(1, 2.0))
        self.assertEqual(res["alias"], "opencoti-2")
        res = self.store.claim("old", "opencoti-mac", client=_me())
        self.assertEqual((res["alias"], res["moved"]), ("opencoti-mac", 1))
        self.store.claim("gone", "z", client=_dead())
        self.assertEqual(self.store.claim("next", "z", client=(1, 2.0))["alias"],
                         "z")
        row = self.store.registration_for_client(_me(), "solidpc")
        self.assertEqual(row["session_id"], "old")
