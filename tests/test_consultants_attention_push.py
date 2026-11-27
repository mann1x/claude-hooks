"""The engine wakes the session that started a run (backup_models #2414):
attention_push posts each new attention state, and the run's end, into
the caller's Claude Code inbox socket, or mails the caller's alias.

langgraph-free → runs in BOTH envs.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from consultants import cli
from consultants.server import attention_push as ap

TOKEN = "test-only-token"


class FakeState:
    def __init__(self, sid="csl-1"):
        self.sid = sid
        self.rec = {"sid": sid, "status": "running"}

    def public_dict(self):
        return dict(self.rec)


def _watcher(state, cwd="", sent=None):
    sent = sent if sent is not None else []

    def deliver(notify, subject, body):
        sent.append((subject, body))
        return "inbox"
    clock = {"t": 0.0}
    w = ap.AttentionWatcher(state, {"socket": "/x.sock", "host": "h"}, cwd=cwd,
                            deliver_fn=deliver, clock=lambda: clock["t"])
    return w, sent, clock


class TestNotifyFromBody:
    def test_known_fields_only(self):
        n = ap.notify_from_body({"notify": {"socket": "/run/a.sock", "token": TOKEN,
                                            "session_id": "s", "host": "h", "x": "y"}})
        assert n == {"socket": "/run/a.sock", "token": TOKEN, "session_id": "s", "host": "h"}

    def test_a_relative_socket_is_dropped(self):
        assert ap.notify_from_body({"notify": {"socket": "a.sock", "session_id": "s"}}) == \
            {"session_id": "s"}
        assert ap.notify_from_body({"notify": {"socket": "a.sock"}}) is None

    def test_nothing(self):
        assert ap.notify_from_body({}) is None
        assert ap.notify_from_body({"notify": "x"}) is None


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
class TestInbox:
    def test_posts_claude_codes_injection_format(self, tmp_path):
        path = str(tmp_path / "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def accept():
            conn, _ = srv.accept()
            with conn:
                buf = b""
                while chunk := conn.recv(4096):
                    buf += chunk
                got.append(buf)
        t = threading.Thread(target=accept)
        t.start()
        assert ap.post_inbox(path, TOKEN, "hello") is True
        t.join(5)
        srv.close()
        lines = got[0].decode().splitlines()
        assert json.loads(lines[0]) == {"type": "auth", "token": TOKEN}
        assert json.loads(lines[1]) == {"type": "user", "message": {
            "role": "user", "content": "hello"}}

    def test_a_dead_socket_is_false(self, tmp_path):
        assert ap.post_inbox(str(tmp_path / "gone.sock"), TOKEN, "x") is False


class TestDeliver:
    def test_inbox_on_this_host(self):
        post = mock.Mock(return_value=True)
        mail = mock.Mock()
        assert ap.deliver({"socket": "/s", "token": TOKEN, "host": "h"}, "S", "B",
                          here="h", post_fn=post, mail_fn=mail) == "inbox"
        post.assert_called_once_with("/s", TOKEN, "S\n\nB")
        mail.assert_not_called()

    def test_mail_for_another_host(self):
        post = mock.Mock()
        mail = mock.Mock(return_value=True)
        assert ap.deliver({"socket": "/s", "host": "other", "session_id": "x"}, "S", "B",
                          here="h", post_fn=post, mail_fn=mail) == "mail"
        post.assert_not_called()

    def test_mail_when_the_inbox_fails(self):
        assert ap.deliver({"socket": "/s", "host": "h", "session_id": "x"}, "S", "B",
                          here="h", post_fn=lambda *a: False,
                          mail_fn=lambda *a: True) == "mail"

    def test_none(self):
        assert ap.deliver({"session_id": "x"}, "S", "B", here="h",
                          post_fn=lambda *a: False, mail_fn=lambda *a: False) == "none"

    def test_mail_goes_to_the_registered_alias_at_priority_1(self):
        store = mock.Mock()
        store.registered_alias.return_value = "backup_models"
        assert ap.mail_fallback({"session_id": "s1", "host": "solidpc"}, "S", "B",
                                store_fn=lambda: store) is True
        args, kw = store.send.call_args
        assert args[0] == "backup_models@solidpc" and kw["priority"] == 1
        store.registered_alias.return_value = None
        assert ap.mail_fallback({"session_id": "s1"}, "S", "B",
                                store_fn=lambda: store) is False


class TestDescribe:
    def test_attention_names_the_answer_and_the_rewait(self):
        subject, body = ap.describe("csl-1", {
            "reason": "adversary_checkpoint", "deadline_ts": 0, "on_timeout": "auto-resumes",
            "answer": "claude-consultants adversary-ack csl-1"})
        assert "needs you: adversary_checkpoint" in subject
        assert "claude-consultants adversary-ack csl-1" in body
        assert "claude-consultants wait csl-1" in body
        assert ap.NOT_THE_USER in body

    def test_finished(self):
        subject, body = ap.describe("csl-1", {"reason": "finished", "status": "completed"})
        assert subject == "[consultants] csl-1 completed"
        assert "claude-consultants result csl-1" in body


class TestWatcher:
    def test_nothing_while_the_run_just_runs(self):
        w, sent, _ = _watcher(FakeState())
        assert w.check() is True and sent == []

    def test_each_attention_state_once(self):
        st = FakeState()
        w, sent, _ = _watcher(st)
        st.rec["adversary_checkpoint_deadline_ts"] = 100.0
        w.check()
        w.check()
        assert len(sent) == 1
        del st.rec["adversary_checkpoint_deadline_ts"]
        w.check()
        st.rec["adversary_checkpoint_deadline_ts"] = 200.0   # a second checkpoint
        w.check()
        assert len(sent) == 2

    def test_a_pending_pause_after_the_grace(self):
        st = FakeState()
        w, sent, clock = _watcher(st)
        st.rec["pause_state"] = "pending"
        w.check()
        assert sent == []
        clock["t"] = 61.0
        w.check()
        assert "pause_not_taking_effect" in sent[0][0]

    def test_the_end_is_pushed_and_the_watcher_stops(self):
        st = FakeState()
        w, sent, _ = _watcher(st)
        st.rec["status"] = "failed"
        assert w.check() is False
        assert sent[0][0] == "[consultants] csl-1 failed"

    def test_a_suspended_run_is_not_its_end(self):
        st = FakeState()
        w, sent, _ = _watcher(st)
        st.rec["status"] = "suspended"
        assert w.check() is True and sent == []

    def test_a_slow_tool_once(self, tmp_path):
        st = FakeState()
        db = tmp_path / ".claude-hooks" / "consultants" / "csl-1" / "transcript.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE runtime_events (event_id INTEGER PRIMARY KEY "
                    "AUTOINCREMENT, ts REAL, kind TEXT, role TEXT, round INTEGER, "
                    "lane_idx INTEGER, payload TEXT)")
        for elapsed in (60.0, 600.0, 3600.0):
            con.execute("INSERT INTO runtime_events (ts, kind, lane_idx, payload) "
                        "VALUES (0, 'guard', 2, ?)", (json.dumps(
                            {"kind": "slow_tool", "tool": "grep", "args": "{}",
                             "elapsed_s": elapsed}),))
        con.commit()
        con.close()
        w, sent, _ = _watcher(st, cwd=str(tmp_path))
        w.check()
        w.check()
        assert len(sent) == 1 and "grep call has run 10 min" in sent[0][0]

    def test_a_failing_delivery_never_raises(self):
        st = FakeState()
        w = ap.AttentionWatcher(st, {"socket": "/x"}, deliver_fn=mock.Mock(
            side_effect=OSError("boom")))
        st.rec["status"] = "completed"
        assert w.check() is False and w.sent == [("finished", "none")]

    def test_start_needs_a_target(self):
        assert ap.start(FakeState(), None) is None


class TestCli:
    def test_caller_notify_from_the_environment(self):
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": "/run/x.sock",
               "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN, "CLAUDE_CODE_SESSION_ID": "sid"}
        n = cli.caller_notify(env)
        assert n["socket"] == "/run/x.sock" and n["token"] == TOKEN
        assert n["session_id"] == "sid" and n["host"]
        assert cli.caller_notify({}) is None

    def test_consult_sends_it_unless_no_push(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/run/x.sock")
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", TOKEN)
        bodies = []

        def fake(method, url, *, body=None, timeout=600.0):
            bodies.append(body)
            return {"sid": "csl-1", "status": "running"}
        for argv in (["consult", "--message", "q"], ["consult", "--message", "q", "--no-push"]):
            args = cli.build_parser().parse_args(argv)
            with mock.patch("consultants.cli._http", side_effect=fake), \
                    mock.patch("sys.stdout"):
                args.fn(args, "http://b")
        assert bodies[0]["notify"]["socket"] == "/run/x.sock"
        assert "notify" not in bodies[1]

    def test_the_test_run_never_sees_the_live_socket(self):
        import os
        assert "CLAUDE_CODE_MESSAGING_SOCKET" not in os.environ


def test_every_run_start_starts_a_watcher():
    """consult, follow-up and the resume after a restart each start one,
    and keep the target in the in-flight record so a resume has it."""
    src = (Path(__file__).resolve().parent.parent / "consultants" / "server" /
           "app.py").read_text(encoding="utf-8")
    assert src.count("attention_push.start(") == 3
    assert src.count('**({"notify": notify} if notify else {})') == 2
    assert 'rec.get("notify")' in src
