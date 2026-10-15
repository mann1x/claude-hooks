"""Plan D9: a long wait on infrastructure is reported, informational only."""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from claude_hooks import ollama_slots
from claude_hooks.get_advice import chat_client as cc
from consultants.engine import stall_chat


def test_notice_thresholds_fire_once_each():
    sent: set = set()
    assert ollama_slots.infra_notices(30, sent) == []
    assert ollama_slots.infra_notices(601, sent) == [600.0]
    assert ollama_slots.infra_notices(900, sent) == []
    assert ollama_slots.infra_notices(7300, sent) == [7200.0]
    assert ollama_slots.infra_notices(9000, sent) == []


class _Ok:
    headers, status = {}, 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter([json.dumps({"message": {"role": "assistant",
                                             "content": "hi"},
                                 "done": True, "done_reason": "stop"}).encode()])


def test_an_outage_wait_is_reported(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setattr(ollama_slots, "INFRA_WAIT_NOTICE_S", (0.0, 1e9))
    seq = iter([urllib.error.URLError("connection refused"), _Ok()])

    def opener(req, timeout=None):
        item = next(seq)
        if isinstance(item, BaseException):
            raise item
        return item

    seen = []
    client = cc.ChatClient("http://stub", max_retries=3)
    client.outage_wait_s = 60
    with patch("urllib.request.urlopen", side_effect=opener):
        out = client.chat_streamed({"model": "m", "messages": []},
                                   on_wait=seen.append)
    assert out["choices"][0]["message"]["content"] == "hi"
    assert len(seen) == 1 and seen[0]["what"] == "outage"


def test_the_stall_wrapper_records_it_as_infra_wait():
    events = []

    def chat_streamed(payload, *, on_token=None, cancel_check=None,
                      on_admitted=None, on_wait=None):
        on_wait({"what": "slot", "waited_s": 601})
        return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

    fn = stall_chat.make_stall_protected_chat_fn(
        chat_streamed, stall_threshold_s=30, hard_cap_s=60, retries=0,
        on_event=events.append)
    fn({"model": "m", "messages": []})
    waits = [e for e in events if e.get("kind") == "infra_wait"]
    assert waits == [{"kind": "infra_wait", "what": "slot", "waited_s": 601}]
