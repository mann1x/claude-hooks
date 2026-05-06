"""Tests for ``claude_hooks.get_advice.config``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hooks.get_advice import config as ac


class TestLoadDefaults:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        cfg = ac.load_config(tmp_path / "missing.json")
        assert cfg.model == ac.DEFAULT_MODEL
        assert cfg.effort == ac.DEFAULT_EFFORT
        assert cfg.reset_threshold == ac.DEFAULT_RESET_THRESHOLD
        assert cfg.tools == list(ac.KNOWN_TOOLS)
        assert cfg.ctx_max is None
        assert cfg.ctx_max_explicit is False

    def test_malformed_json_returns_defaults(self, tmp_path: Path):
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        cfg = ac.load_config(p)
        assert cfg.model == ac.DEFAULT_MODEL

    def test_partial_file_fills_in_defaults(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"model": "custom:tag"}))
        cfg = ac.load_config(p)
        assert cfg.model == "custom:tag"
        assert cfg.effort == ac.DEFAULT_EFFORT
        assert cfg.tools == list(ac.KNOWN_TOOLS)


class TestSetModel:
    def test_basic_set(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_model("foo:bar", path=p)
        assert cfg.model == "foo:bar"
        assert cfg.ctx_max is None
        # File written.
        loaded = ac.load_config(p)
        assert loaded.model == "foo:bar"

    def test_with_ctx_marks_explicit(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_model("foo:bar", ctx_max=8000, path=p)
        assert cfg.ctx_max == 8000
        assert cfg.ctx_max_explicit is True

    def test_set_without_ctx_keeps_explicit(self, tmp_path: Path):
        # User pinned 8000 earlier; switching to a new model without ctx
        # should keep the explicit value (user said "I want 8000 here").
        p = tmp_path / "c.json"
        ac.set_model("a:1", ctx_max=8000, path=p)
        cfg = ac.set_model("b:2", path=p)
        assert cfg.model == "b:2"
        assert cfg.ctx_max == 8000
        assert cfg.ctx_max_explicit is True

    def test_set_without_ctx_clears_auto(self, tmp_path: Path):
        # Prior auto-detected ctx (no explicit flag) must be cleared on
        # model switch — it belonged to the previous model.
        p = tmp_path / "c.json"
        cfg = ac.AdvisorConfig(model="a:1", ctx_max=4000, ctx_max_explicit=False)
        ac.save_config(cfg, p)
        cfg2 = ac.set_model("b:2", path=p)
        assert cfg2.ctx_max is None

    def test_empty_name_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            ac.set_model("", path=tmp_path / "c.json")
        with pytest.raises(ValueError):
            ac.set_model("   ", path=tmp_path / "c.json")


class TestSetEffort:
    @pytest.mark.parametrize("tier", ["low", "medium", "high", "max"])
    def test_valid_tiers(self, tmp_path: Path, tier: str):
        cfg = ac.set_effort(tier, path=tmp_path / "c.json")
        assert cfg.effort == tier

    def test_unknown_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            ac.set_effort("ridiculous", path=tmp_path / "c.json")

    def test_budget_lookup(self, tmp_path: Path):
        cfg = ac.set_effort("max", path=tmp_path / "c.json")
        assert cfg.effort_budget == 25
        cfg = ac.set_effort("low", path=tmp_path / "c.json")
        assert cfg.effort_budget == 1


class TestSetTools:
    def test_csv(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_tools("read_file,grep", path=p)
        assert cfg.tools == ["read_file", "grep"]

    def test_all_token_expands(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_tools("all", path=p)
        assert cfg.tools == list(ac.KNOWN_TOOLS)

    def test_none_token_clears(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_tools("none", path=p)
        assert cfg.tools == []

    def test_unknown_rejected(self, tmp_path: Path):
        p = tmp_path / "c.json"
        with pytest.raises(ValueError) as exc:
            ac.set_tools("read_file,bogus_tool", path=p)
        assert "bogus_tool" in str(exc.value)
        # Valid tools listed in the error so the user can self-correct.
        assert "read_file" in str(exc.value)

    def test_empty_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            ac.set_tools("", path=tmp_path / "c.json")
        with pytest.raises(ValueError):
            ac.set_tools("   ", path=tmp_path / "c.json")

    def test_dedup_preserves_order(self, tmp_path: Path):
        p = tmp_path / "c.json"
        cfg = ac.set_tools("grep,read_file,grep", path=p)
        assert cfg.tools == ["grep", "read_file"]

    def test_persistence_round_trip(self, tmp_path: Path):
        p = tmp_path / "c.json"
        ac.set_tools("read_file,grep,glob", path=p)
        cfg = ac.load_config(p)
        assert cfg.tools == ["read_file", "grep", "glob"]


class TestNormalizeFromFile:
    def test_load_normalizes_string_form(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"tools": "read_file, grep"}))
        cfg = ac.load_config(p)
        assert cfg.tools == ["read_file", "grep"]

    def test_load_drops_unknown_silently(self, tmp_path: Path):
        # ``load_config`` is lenient — unknown names are dropped, not
        # raised. ``set_tools`` is the strict validator.
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"tools": ["read_file", "fakey"]}))
        cfg = ac.load_config(p)
        assert cfg.tools == ["read_file"]

    def test_load_handles_all_token(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"tools": ["all"]}))
        cfg = ac.load_config(p)
        assert cfg.tools == list(ac.KNOWN_TOOLS)

    def test_load_handles_none_token(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"tools": ["none"]}))
        cfg = ac.load_config(p)
        assert cfg.tools == []
