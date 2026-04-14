"""Unit tests for claude_hooks.reflect."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_hooks import reflect as reflect_mod
from claude_hooks.providers.base import Memory
from tests.mocks.ollama import mock_ollama_generate


def _mems(n, prefix="entry", obs_type="general"):
    return [
        Memory(text=f"{prefix} {i}", metadata={"observation_type": obs_type})
        for i in range(n)
    ]


class TestReflectGuards:
    def test_disabled_returns_empty(self, base_config, fake_provider):
        cfg = base_config(reflect={"enabled": False})
        provider = fake_provider(recall_returns=_mems(10))
        out = reflect_mod.reflect(cfg, [provider])
        assert out == []

    def test_no_providers_returns_empty(self, base_config):
        cfg = base_config()
        out = reflect_mod.reflect(cfg, [])
        assert out == []

    def test_too_few_memories_returns_empty(self, base_config, fake_provider):
        cfg = base_config(reflect={"min_pattern_count": 10})
        provider = fake_provider(recall_returns=_mems(2))
        out = reflect_mod.reflect(cfg, [provider])
        assert out == []


class TestReflectHappyPath:
    def test_writes_rules_to_output_file(self, base_config, fake_provider, tmp_path):
        out_path = tmp_path / "CLAUDE.md"
        cfg = base_config(reflect={
            "enabled": True,
            "min_pattern_count": 2,
            "output_path": str(out_path),
        })
        provider = fake_provider(recall_returns=_mems(5))

        response = "- Always validate inputs\n- Never use eval()\n"
        with mock_ollama_generate(response, target="claude_hooks.reflect"):
            rules = reflect_mod.reflect(cfg, [provider])

        assert len(rules) == 2
        assert "Always validate inputs" in rules[0]
        text = out_path.read_text()
        assert "## Auto-reflected rules" in text
        assert "Always validate inputs" in text

    def test_dry_run_does_not_write(self, base_config, fake_provider, tmp_path, capsys):
        out_path = tmp_path / "CLAUDE.md"
        cfg = base_config(reflect={
            "enabled": True,
            "min_pattern_count": 2,
            "output_path": str(out_path),
        })
        provider = fake_provider(recall_returns=_mems(5))

        with mock_ollama_generate("- Rule one\n", target="claude_hooks.reflect"):
            rules = reflect_mod.reflect(cfg, [provider], dry_run=True)

        assert len(rules) == 1
        assert not out_path.exists()


class TestReflectOllamaFailure:
    def test_ollama_failure_returns_empty(self, base_config, fake_provider, tmp_path):
        out_path = tmp_path / "CLAUDE.md"
        cfg = base_config(reflect={
            "enabled": True,
            "min_pattern_count": 2,
            "output_path": str(out_path),
        })
        provider = fake_provider(recall_returns=_mems(5))

        with mock_ollama_generate("", target="claude_hooks.reflect", fail=True):
            rules = reflect_mod.reflect(cfg, [provider])

        assert rules == []
        assert not out_path.exists()

    def test_no_patterns_response_returns_empty(self, base_config, fake_provider, tmp_path):
        out_path = tmp_path / "CLAUDE.md"
        cfg = base_config(reflect={
            "enabled": True,
            "min_pattern_count": 2,
            "output_path": str(out_path),
        })
        provider = fake_provider(recall_returns=_mems(5))

        with mock_ollama_generate("No patterns found.", target="claude_hooks.reflect"):
            rules = reflect_mod.reflect(cfg, [provider])

        assert rules == []
        assert not out_path.exists()


class TestReflectInternals:
    def test_pull_recent_dedups_across_providers(self, fake_provider):
        shared = Memory(text="shared memory text content here", metadata={})
        p1 = fake_provider(name="p1", recall_returns=[shared])
        p2 = fake_provider(name="p2", recall_returns=[shared])
        out = reflect_mod._pull_recent([p1, p2], max_per_provider=10)
        # Same first 100 chars → dedup keeps only one.
        assert len(out) == 1

    def test_pull_recent_skips_failing_provider(self, fake_provider):
        good = fake_provider(name="g", recall_returns=_mems(2, prefix="good"))
        bad = fake_provider(name="b", recall_errors=True)
        out = reflect_mod._pull_recent([good, bad], max_per_provider=10)
        # Good provider's results survive; bad provider raises and is skipped.
        assert all("good" in m.text for m in out)
        assert len(out) >= 2

    def test_group_by_type_uses_observation_type(self):
        mems = [
            Memory(text="a", metadata={"observation_type": "fix"}),
            Memory(text="b", metadata={"observation_type": "fix"}),
            Memory(text="c", metadata={"observation_type": "preference"}),
            Memory(text="d", metadata={}),
        ]
        groups = reflect_mod._group_by_type(mems)
        assert set(groups.keys()) == {"fix", "preference", "general"}
        assert len(groups["fix"]) == 2
        assert len(groups["general"]) == 1


class TestAppendRules:
    def test_skips_if_already_reflected_today(self, tmp_path):
        path = tmp_path / "CLAUDE.md"
        # Pre-populate with today's section.
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path.write_text(f"## Auto-reflected rules ({ts})\n\n- prior\n")

        before = path.read_text()
        reflect_mod._append_rules(path, ["- new rule"])
        # Unchanged — second run on same day is a no-op.
        assert path.read_text() == before

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "CLAUDE.md"
        reflect_mod._append_rules(path, ["- a rule"])
        assert path.exists()
        assert "a rule" in path.read_text()
