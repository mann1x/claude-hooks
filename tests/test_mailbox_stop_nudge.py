"""The Stop hook nudges the model about unread mail: once, not a loop.

The Stop notice is a systemMessage, which the operator sees and the model
never does, so mail that arrived during a long turn waited for the next
prompt. The nudge blocks the stop with a reason naming the messages,
under two rules that keep it from looping:

* never when the stop is already the continuation of a block
  (``stop_hook_active``);
* never twice for the same message in the same session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from claude_hooks import hook_parts
from claude_hooks.hooks import stop
from claude_hooks.mailbox import hook as mb

CFG = {"hooks": {"mailbox": {"enabled": True}}}


def _msg(i, subject="M2+M3 shipped", alias="opencoti", host="solidpc"):
    return {"id": i, "subject": subject, "from_alias": alias, "from_host": host,
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=42)}


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_MAILBOX_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def inbox():
    rows = [_msg(101), _msg(102, "M4 shipped: speech-to-text")]
    with mock.patch.object(mb, "unread_messages", side_effect=lambda **kw: list(rows)):
        yield rows


def _nudge(event, cfg=CFG, result=None):
    return stop._with_mailbox_nudge(result, event, cfg, providers=[])


def test_first_stop_blocks_with_ids_and_subjects(inbox):
    out = _nudge({"session_id": "s1"}, result={"systemMessage": "stored"})
    assert out["decision"] == "block"
    assert out["systemMessage"] == "stored"       # the operator line stays
    r = out["reason"]
    assert "2 unread mailbox message(s)" in r
    assert "#101 `M2+M3 shipped` — from `opencoti@solidpc`, 42 min ago" in r
    assert "#102" in r and "ids: [101, 102]" in r
    assert "mailbox-read" in r


def test_the_continuation_of_a_block_is_never_blocked(inbox):
    out = _nudge({"session_id": "s1", "stop_hook_active": True},
                 result={"systemMessage": "x"})
    assert out == {"systemMessage": "x"}


def test_the_same_messages_are_nudged_once_per_session(inbox):
    assert _nudge({"session_id": "s1"})["decision"] == "block"
    # Next turn: still unread (the session chose not to read them).
    assert _nudge({"session_id": "s1"}) is None
    # A new message: nudged, and only that one.
    inbox.append(_msg(103, "M5 shipped"))
    out = _nudge({"session_id": "s1"})
    assert "#103" in out["reason"] and "#101" not in out["reason"]
    assert "1 unread" in out["reason"]
    # Another session is a different reader.
    assert "#101" in _nudge({"session_id": "s2"})["reason"]


def test_no_mail_no_block(state_dir):
    with mock.patch.object(mb, "unread_messages", return_value=[]):
        assert _nudge({"session_id": "s1"}, result={"systemMessage": "x"}) == {"systemMessage": "x"}


def test_it_can_be_turned_off(inbox):
    cfg = {"hooks": {"mailbox": {"enabled": True, "stop_nudge": False}}}
    assert _nudge({"session_id": "s1"}, cfg=cfg) is None


def test_a_broken_state_dir_fails_closed(inbox, state_dir):
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.write_text("not a directory")
    assert _nudge({"session_id": "s1"}) is None


def test_no_session_id_no_nudge(inbox):
    assert _nudge({}) is None


def test_the_kept_parts_stop_path_nudges_too(inbox):
    with mock.patch("claude_hooks.hooks.stop._mailbox_notice",
                    return_value="[claude-hooks] 2 new message(s) while working:"):
        out = hook_parts.run("Stop", event={"session_id": "s1", "cwd": "/x"},
                             config=CFG, providers=[], keep=frozenset({"mailbox"}))
    assert out["decision"] == "block" and "#101" in out["reason"]
    assert "while working" in out["systemMessage"]


def test_the_memory_only_kept_path_does_not(inbox):
    with mock.patch("claude_hooks.hooks.stop.store_turn", return_value="stored"):
        out = hook_parts.run("Stop", event={"session_id": "s1", "cwd": "/x"},
                             config=CFG, providers=[], keep=frozenset({"memory"}))
    assert "decision" not in out


def test_claim_nudge_prunes_old_sessions(state_dir):
    import os
    import time
    assert mb.claim_nudge("old", [1]) == [1]
    old = state_dir / "nudged-old.json"
    t = time.time() - mb.NUDGE_STATE_MAX_AGE_SECONDS - 60
    os.utime(old, (t, t))
    mb.claim_nudge("new", [2])
    assert not old.exists()
