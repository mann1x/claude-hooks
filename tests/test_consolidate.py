"""Unit tests for claude_hooks.consolidate."""

from __future__ import annotations

import json
from pathlib import Path

from claude_hooks import consolidate as con_mod
from claude_hooks.providers.base import Memory
from tests.mocks.ollama import mock_ollama_generate


def _mems(n, prefix="m"):
    return [Memory(text=f"{prefix} {i} unique content text", metadata={}) for i in range(n)]


class TestConsolidateGuards:
    def test_disabled_returns_empty_result(self, base_config, fake_provider):
        cfg = base_config(consolidate={"enabled": False})
        provider = fake_provider(recall_returns=_mems(20))
        result = con_mod.consolidate(cfg, [provider])
        assert result.merged == 0
        assert result.compressed == 0

    def test_no_providers_returns_empty_result(self, base_config):
        cfg = base_config(consolidate={"enabled": True})
        result = con_mod.consolidate(cfg, [])
        assert result.merged == 0

    def test_too_few_memories_skips(self, base_config, fake_provider):
        cfg = base_config(consolidate={"enabled": True})
        # Below the hardcoded floor of 5.
        provider = fake_provider(recall_returns=_mems(2))
        result = con_mod.consolidate(cfg, [provider])
        assert result.merged == 0
        assert result.compressed == 0


class TestConsolidateMerge:
    def test_finds_near_duplicate_pairs(self, base_config, fake_provider, tmp_path):
        # Two identical memories + a few unique → expect at least one merge pair.
        dup = "the same long text appears twice in the recall set"
        mems = [
            Memory(text=dup, metadata={}),
            Memory(text=dup + " ", metadata={}),  # whitespace-only diff
            Memory(text="different content one", metadata={}),
            Memory(text="different content two", metadata={}),
            Memory(text="different content three", metadata={}),
        ]
        # Provider dedups identical first-100-char keys, so wrap each in its
        # own provider to bypass the _pull_all dedup.
        p1 = fake_provider(name="p1", recall_returns=[mems[0], mems[2], mems[3], mems[4]])
        p2 = fake_provider(name="p2", recall_returns=[mems[1]])

        state = tmp_path / "state.json"
        cfg = base_config(consolidate={
            "enabled": True,
            "merge_similarity_threshold": 0.5,
            "state_file": str(state),
        })
        result = con_mod.consolidate(cfg, [p1, p2], dry_run=True)
        assert result.merged >= 1

    def test_writes_state_file_when_not_dry_run(self, base_config, fake_provider, tmp_path):
        state = tmp_path / "state.json"
        cfg = base_config(consolidate={
            "enabled": True,
            "state_file": str(state),
        })
        provider = fake_provider(recall_returns=_mems(10))
        con_mod.consolidate(cfg, [provider])
        assert state.exists()
        data = json.loads(state.read_text())
        assert "last_run" in data


class TestCompressInternal:
    def test_compress_returns_short_text(self):
        long_text = "x" * 2000
        with mock_ollama_generate("short summary", target="claude_hooks.consolidate"):
            out = con_mod._compress(
                long_text,
                model="m",
                url="http://localhost:11434/api/generate",
            )
        assert out == "short summary"

    def test_compress_returns_none_on_failure(self):
        with mock_ollama_generate("", target="claude_hooks.consolidate", fail=True):
            out = con_mod._compress("x" * 100, model="m", url="http://x")
        assert out is None

    def test_compress_returns_none_on_empty_response(self):
        with mock_ollama_generate("", target="claude_hooks.consolidate"):
            out = con_mod._compress("x" * 100, model="m", url="http://x")
        assert out is None


class TestShouldRun:
    def test_disabled_returns_false(self, base_config, tmp_path):
        cfg = base_config(consolidate={
            "enabled": False,
            "trigger": "session_start",
            "state_file": str(tmp_path / "s.json"),
        })
        assert con_mod.should_run(cfg) is False

    def test_manual_trigger_returns_false(self, base_config, tmp_path):
        cfg = base_config(consolidate={
            "enabled": True,
            "trigger": "manual",
            "state_file": str(tmp_path / "s.json"),
        })
        assert con_mod.should_run(cfg) is False

    def test_session_start_no_state_returns_true(self, base_config, tmp_path):
        cfg = base_config(consolidate={
            "enabled": True,
            "trigger": "session_start",
            "state_file": str(tmp_path / "missing.json"),
            "min_sessions_between_runs": 10,
        })
        # No state file → 999 sessions since last → above threshold.
        assert con_mod.should_run(cfg) is True

    def test_session_start_corrupt_state_treats_as_never_run(self, base_config, tmp_path):
        state = tmp_path / "s.json"
        state.write_text("not valid json {{{")
        cfg = base_config(consolidate={
            "enabled": True,
            "trigger": "session_start",
            "state_file": str(state),
            "min_sessions_between_runs": 10,
        })
        assert con_mod.should_run(cfg) is True


class TestPullAll:
    def test_dedups_across_providers(self, fake_provider):
        same = Memory(text="identical first hundred chars repeated across both providers and queries here", metadata={})
        p1 = fake_provider(name="a", recall_returns=[same])
        p2 = fake_provider(name="b", recall_returns=[same])
        out = con_mod._pull_all([p1, p2], max_total=50)
        assert len(out) == 1

    def test_skips_failing_provider(self, fake_provider):
        good = fake_provider(name="g", recall_returns=_mems(3, prefix="g"))
        bad = fake_provider(name="b", recall_errors=True)
        out = con_mod._pull_all([good, bad], max_total=50)
        assert len(out) >= 3
        assert all("g" in m.text for m in out)


class TestFindMergeCandidates:
    def test_returns_pairs_above_threshold(self):
        a = Memory(text="alpha beta gamma delta epsilon", metadata={})
        b = Memory(text="alpha beta gamma delta epsilon", metadata={})
        c = Memory(text="totally different content", metadata={})
        pairs = con_mod._find_merge_candidates([a, b, c], threshold=0.9)
        assert len(pairs) == 1
        assert pairs[0] == (a, b)

    def test_empty_input_returns_empty(self):
        assert con_mod._find_merge_candidates([], threshold=0.5) == []
