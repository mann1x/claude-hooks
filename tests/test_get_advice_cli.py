"""Tests for ``claude_hooks.get_advice.cli``.

Exercises the CLI surface end-to-end with a fresh HOME/cache to avoid
touching real user state. ``turn`` paths are tested with a mocked
chat_fn.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import cli, config as ac, state as ast


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force base url to a sentinel so probe attempts in tests can be
    # mocked without leaking env.
    monkeypatch.setenv("CALIBER_GROUNDING_UPSTREAM", "http://test.invalid")
    monkeypatch.setenv("CLAUDE_ADVISOR_CACHE_DIR",
                       str(tmp_path / "cache"))
    yield


def _run(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = cli.main(argv)
    text = buf.getvalue().strip()
    if not text:
        return rc, {}
    return rc, json.loads(text)


class TestGetSetModel:
    def test_get_default(self):
        rc, out = _run(["get-model"])
        assert rc == 0
        assert out["model"] == ac.DEFAULT_MODEL

    def test_set_then_get(self):
        rc, _ = _run(["set-model", "deepseek-v4-pro:cloud"])
        assert rc == 0
        rc, out = _run(["get-model"])
        assert out["model"] == "deepseek-v4-pro:cloud"

    def test_set_with_ctx(self):
        _run(["set-model", "qwen3:0.6b", "8000"])
        rc, out = _run(["get-model"])
        assert out["ctx_max"] == 8000
        assert out["ctx_max_explicit"] is True


class TestGetSetEffort:
    def test_default(self):
        rc, out = _run(["get-effort"])
        assert out["effort"] == ac.DEFAULT_EFFORT
        assert out["budget_sessions"] == 3

    def test_set_high(self):
        _run(["set-effort", "high"])
        rc, out = _run(["get-effort"])
        assert out["effort"] == "high"
        assert out["budget_sessions"] == 5

    def test_invalid_rejected(self):
        rc, out = _run(["set-effort", "infinite"])
        assert rc == 2
        assert out["ok"] is False
        assert "low" in out["error"]


class TestGetSetTools:
    def test_default(self):
        rc, out = _run(["get-tools"])
        assert out["tools"] == list(ac.KNOWN_TOOLS)
        assert out["known"] == list(ac.KNOWN_TOOLS)

    def test_set_csv(self):
        _run(["set-tools", "read_file,grep,glob"])
        rc, out = _run(["get-tools"])
        assert out["tools"] == ["read_file", "grep", "glob"]

    def test_all_token(self):
        _run(["set-tools", "read_file"])
        _run(["set-tools", "all"])
        rc, out = _run(["get-tools"])
        assert out["tools"] == list(ac.KNOWN_TOOLS)

    def test_none_token(self):
        _run(["set-tools", "none"])
        rc, out = _run(["get-tools"])
        assert out["tools"] == []

    def test_unknown_rejected(self):
        rc, out = _run(["set-tools", "fake_tool"])
        assert rc == 2
        assert "fake_tool" in out["error"]


class TestCleanup:
    def test_zero_when_empty(self, tmp_path: Path):
        rc, out = _run(["cleanup"])
        assert rc == 0
        assert out["removed"] == 0

    def test_evicts_old(self, tmp_path: Path):
        cache = Path(os.environ["CLAUDE_ADVISOR_CACHE_DIR"])
        old = cache / "old-sid"
        old.mkdir(parents=True)
        sf = old / "state.json"
        sf.write_text("{}")
        import time
        ts = time.time() - 200000
        os.utime(sf, (ts, ts))
        os.utime(old, (ts, ts))
        rc, out = _run(["cleanup", "--max-age-seconds", "86400"])
        assert out["removed"] == 1


class TestTurn:
    def _setup_session_dir(self):
        # ensure the cache root exists for save()
        Path(os.environ["CLAUDE_ADVISOR_CACHE_DIR"]).mkdir(parents=True,
                                                           exist_ok=True)

    def test_first_turn_runs_through_runner(self, tmp_path: Path):
        self._setup_session_dir()
        _run(["set-model", "test:tag"])
        _run(["set-tools", "none"])  # avoid grounding tools needing real cwd
        sentinel = {"choices": [{
            "message": {"role": "assistant", "content": "hi-from-advisor"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                       "total_tokens": 120}}
        # Mock the chat_client so we don't hit the network. Patch where
        # the cli imports it.
        with patch.object(cli.ChatClient, "chat",
                          return_value=sentinel) as m:
            # The runner reads usage off `client.last_usage`, which the
            # chat method normally fills. Simulate that by setting it
            # via side_effect.
            def fake_chat(self_inst, payload):
                self_inst.last_usage = {"prompt_eval_count": 100,
                                        "eval_count": 20}
                return sentinel
            m.side_effect = lambda payload: fake_chat(_FakeSelf(m), payload)
            rc, out = _run([
                "turn", "smoke", "--first",
                "--message", "what's up?",
                "--cwd", str(tmp_path),
            ])
        assert rc == 0
        assert out["ok"] is True
        assert out["reply"] == "hi-from-advisor"
        assert out["turns"] == 1
        assert out["model"] == "test:tag"
        assert out["tools_enabled"] == []

    def test_first_required_for_new_session(self, tmp_path: Path):
        rc, out = _run([
            "turn", "doesnt-exist",
            "--message", "hi", "--cwd", str(tmp_path),
        ])
        assert rc == 2
        assert "not found" in out["error"]

    def test_first_rejected_on_existing_session(self, tmp_path: Path):
        self._setup_session_dir()
        sess = ast.SessionState(sid="dup", model="m", ctx_max=None,
                                reset_threshold=0.85)
        sess.save()
        rc, out = _run([
            "turn", "dup", "--first",
            "--message", "hi", "--cwd", str(tmp_path),
        ])
        assert rc == 2
        assert "already exists" in out["error"]


class _FakeSelf:
    """Stand-in for the bound ``self`` when patching ChatClient.chat
    via ``side_effect`` without a real instance."""
    def __init__(self, mock):
        self.last_usage = {"prompt_eval_count": 0, "eval_count": 0}
        self._mock = mock
