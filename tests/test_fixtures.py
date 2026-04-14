"""Smoke tests for the fixtures in conftest.py and tests/mocks/.

If these pass, downstream test files can rely on fixture shape without
re-checking invariants.
"""

from __future__ import annotations

import os

import pytest

from claude_hooks.providers.base import Memory


# --------------------------------------------------------------------- #
# fake_provider
# --------------------------------------------------------------------- #
class TestFakeProvider:
    def test_returns_recall_items(self, fake_provider):
        p = fake_provider(
            name="q",
            recall_returns=[Memory(text="hello", metadata={"a": 1})],
        )
        mems = p.recall("q", k=5)
        assert len(mems) == 1
        assert mems[0].text == "hello"
        assert p.recall_calls == [("q", 5)]

    def test_respects_k_cap(self, fake_provider):
        p = fake_provider(recall_returns=[Memory(text=str(i)) for i in range(10)])
        mems = p.recall("x", k=3)
        assert len(mems) == 3

    def test_recall_errors_raises(self, fake_provider):
        p = fake_provider(recall_errors=True)
        with pytest.raises(RuntimeError):
            p.recall("x")

    def test_store_records_call(self, fake_provider):
        p = fake_provider()
        p.store("content", {"type": "t"})
        assert p.stored == [("content", {"type": "t"})]

    def test_store_errors_raises(self, fake_provider):
        p = fake_provider(store_errors=True)
        with pytest.raises(RuntimeError):
            p.store("x", {})


# --------------------------------------------------------------------- #
# base_config
# --------------------------------------------------------------------- #
class TestBaseConfig:
    def test_default_disables_safety_log(self, base_config):
        cfg = base_config()
        assert cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] is False

    def test_disables_claudemem_reindex_by_default(self, base_config):
        cfg = base_config()
        assert cfg["hooks"]["claudemem_reindex"]["enabled"] is False

    def test_applies_nested_overrides(self, base_config):
        cfg = base_config(hooks={"stop": {"enabled": False}})
        assert cfg["hooks"]["stop"]["enabled"] is False
        # neighbouring keys preserved
        assert cfg["hooks"]["stop"]["store_threshold"] == "noteworthy"

    def test_each_call_returns_fresh_copy(self, base_config):
        a = base_config()
        b = base_config()
        a["hooks"]["stop"]["enabled"] = False
        assert b["hooks"]["stop"]["enabled"] is True


# --------------------------------------------------------------------- #
# fake_transcript / transcript_file
# --------------------------------------------------------------------- #
class TestFakeTranscript:
    def test_user_and_assistant_text(self, fake_transcript):
        t = fake_transcript(user="hi", assistant_text="ok")
        assert len(t) == 2
        assert t[0]["message"]["role"] == "user"
        assert t[1]["message"]["role"] == "assistant"
        assert t[1]["message"]["content"][0]["text"] == "ok"

    def test_tool_use_block(self, fake_transcript):
        t = fake_transcript(
            user="go",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
        )
        blocks = t[1]["message"]["content"]
        assert any(b.get("type") == "tool_use" and b["name"] == "Edit" for b in blocks)

    def test_transcript_file_is_jsonl(self, transcript_file):
        path = transcript_file(user="u", assistant_text="a")
        with open(path) as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 2


# --------------------------------------------------------------------- #
# tmp_claude_home
# --------------------------------------------------------------------- #
class TestTmpClaudeHome:
    def test_home_env_redirected(self, tmp_claude_home):
        assert os.environ["HOME"] == str(tmp_claude_home)

    def test_expanduser_follows_tmp(self, tmp_claude_home):
        assert os.path.expanduser("~") == str(tmp_claude_home)


# --------------------------------------------------------------------- #
# tests.mocks.ollama
# --------------------------------------------------------------------- #
class TestOllamaMock:
    def _call_args(self):
        return dict(
            user_prompt="raw",
            system_prompt="sys",
            model="qwen3.5:2b",
            url="http://localhost:11434/api/generate",
            timeout=5.0,
            max_tokens=50,
        )

    def test_generate_returns_response_text(self):
        from tests.mocks.ollama import mock_ollama_generate
        from claude_hooks.hyde import _call_ollama

        # Must be >=10 chars — _call_ollama drops shorter responses.
        with mock_ollama_generate("expanded response text", target="claude_hooks.hyde"):
            out = _call_ollama(**self._call_args())
        assert out == "expanded response text"

    def test_generate_fail_returns_empty(self):
        from tests.mocks.ollama import mock_ollama_generate
        from claude_hooks.hyde import _call_ollama

        with mock_ollama_generate("", target="claude_hooks.hyde", fail=True):
            out = _call_ollama(**self._call_args())
        assert out == ""


class TestMcpMockExport:
    def test_fake_mcp_provider_exported(self):
        from tests.mocks.mcp import FakeMcpProvider
        p = FakeMcpProvider(name="x")
        assert p.name == "x"
