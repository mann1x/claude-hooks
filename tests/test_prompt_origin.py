"""Harness-started turns are not user prompts (claude_hooks.prompt_origin).

Background task notifications and scheduled wake-ups / cron ticks fire
UserPromptSubmit like a typed prompt. They used to get a HyDE expansion
and a full recall, be stored as "## Prompt <task-notification>…", and
stand in for the user's last word in the stop guard.
"""
from __future__ import annotations

import json

import pytest

from claude_hooks import prompt_origin as po
from claude_hooks.hooks import stop
from claude_hooks.hooks import user_prompt_submit as ups

NOTE = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
        "<summary>Background command \"run tests\" completed (exit code 0)"
        "</summary>\n</task-notification>")


def _row(text, **meta):
    return {"type": "user", "message": {"role": "user", "content": text},
            **meta}


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                 encoding="utf-8")
    return str(p)


class TestClassify:
    def test_a_notification_is_known_from_its_text(self):
        o = po.classify(NOTE)
        assert (o.kind, o.decided_by, o.synthetic) == (
            "task-notification", "text", True)

    def test_a_cron_tick_is_known_from_the_transcript(self, tmp_path):
        tick = "WATCHDOG TICK — check the pod and report"
        path = _write(tmp_path, [
            _row("hello there", origin={"kind": "human"}, promptSource="typed"),
            _row(tick, isMeta=True, promptSource="system"),
        ])
        assert po.classify(tick, path) == po.PromptOrigin("scheduled",
                                                          "transcript")

    def test_a_typed_prompt_is_human(self, tmp_path):
        path = _write(tmp_path, [_row("fix the bug please",
                                      origin={"kind": "human"},
                                      promptSource="typed")])
        assert po.classify("fix the bug please", path).kind == "human"

    def test_a_wakeup_suffix_marks_it_scheduled(self):
        o = po.classify("continue: check it\n\n(When this fires: report)")
        assert (o.kind, o.decided_by) == ("scheduled", "marker")

    def test_unknown_defaults_to_human(self, tmp_path):
        assert po.classify("anything", str(tmp_path / "missing")).kind == "human"

    def test_last_human_text_skips_harness_turns(self):
        rows = [_row("please wrap up", origin={"kind": "human"},
                     promptSource="typed"),
                _row(NOTE, origin={"kind": "task-notification"},
                     promptSource="system"),
                _row("tick", isMeta=True, promptSource="system"),
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "content": "x"}]}}]
        assert po.last_human_text(rows) == "please wrap up"


class TestUserPromptSubmit:
    @pytest.fixture
    def calls(self, monkeypatch):
        seen = []

        def fake_run_recall(prompt, *, config, **kw):
            seen.append(config["hooks"]["user_prompt_submit"].get(
                "hyde_enabled"))
            return "## Recalled memory\n- x"

        import claude_hooks.recall as recall
        monkeypatch.setattr(recall, "run_recall", fake_run_recall)
        return seen

    def _cfg(self, **ups_cfg):
        return {"hooks": {"user_prompt_submit": {"hyde_enabled": True,
                                                 **ups_cfg}}}

    def test_no_recall_for_a_notification(self, calls):
        ups.handle(event={"prompt": NOTE, "cwd": ""}, config=self._cfg(),
                   providers=[])
        assert calls == []

    def test_plain_recall_without_hyde_for_a_scheduled_tick(self, calls):
        ups.handle(event={"prompt": "continue: check the long run\n\n"
                                    "(When this fires: report)", "cwd": ""},
                   config=self._cfg(), providers=[])
        assert calls == [False]

    def test_a_user_prompt_keeps_hyde(self, calls):
        ups.handle(event={"prompt": "why does the proxy return 502 so often",
                          "cwd": ""}, config=self._cfg(), providers=[])
        assert calls == [True]

    def test_the_mode_is_configurable(self, calls):
        ups.handle(event={"prompt": NOTE, "cwd": ""},
                   config=self._cfg(synthetic_recall={
                       "task-notification": "full"}), providers=[])
        assert calls == [True]


class TestStopSummary:
    def test_a_notification_turn_is_stored_as_one(self):
        transcript = [
            _row(NOTE, origin={"kind": "task-notification"},
                 promptSource="system"),
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "All 6760 tests passed."}]}},
        ]
        out = stop._label_synthetic_prompt(transcript[0], NOTE)
        assert out.startswith("[not a user message: background task "
                              "notification]")
        assert "run tests" in out and "<task-id>" not in out

    def test_a_human_prompt_is_unchanged(self):
        row = _row("fix it", origin={"kind": "human"}, promptSource="typed")
        assert stop._label_synthetic_prompt(row, "fix it") == "fix it"
