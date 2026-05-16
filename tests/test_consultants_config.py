"""Tests for ``consultants.config``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from consultants import config as cc


# ----------------------- fixtures ---------------------------------- #

@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Redirect Path.home() to a tmp dir so user-global writes don't
    touch the real $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path


# ----------------------- defaults --------------------------------- #

class TestDefaults:
    def test_roles_listed_in_order(self):
        # M6: tool_executor joins the role registry as opt-in.
        # M10: coder joins too, inserted between critic and synthesizer
        # so the graph topology fans out coder lanes AFTER the critic
        # round (when critic enabled) but BEFORE the synthesizer.
        assert cc.ROLES == (
            "planner", "researcher", "tool_executor",
            "critic", "coder", "synthesizer",
        )

    def test_synthesizer_mandatory(self):
        assert "synthesizer" in cc.MANDATORY_ROLES

    def test_default_config_passes_validation(self):
        cfg = cc.ConsultantsConfig()
        assert cc.validate_pipeline(cfg) is None

    def test_opt_in_roles_disabled_by_default(self):
        # M6 + M10: tool_executor and coder ship disabled. The rest
        # stay enabled-by-default to preserve v1 behavior across the
        # schema bump.
        cfg = cc.ConsultantsConfig()
        assert cfg.roles["tool_executor"].enabled is False
        assert cfg.roles["coder"].enabled is False
        for r in cc.ROLES:
            if r in ("tool_executor", "coder"):
                continue
            assert cfg.roles[r].enabled is True

    def test_tool_executor_default_model(self):
        # M6: tool_executor uniquely defaults to a tool-call specialist
        # model; the M11c bench will decide whether to flip the default.
        cfg = cc.ConsultantsConfig()
        assert cfg.roles["tool_executor"].model == "gemma4:31b-cloud"

    def test_coder_default_model_is_rubric_winner(self):
        # M11b 2026-05-16 baseline crowned ``glm-5.1:cloud`` as
        # the coder rubric winner (pass=100%, avg_quality=4.88,
        # median_tokens=1841). The constant lives in
        # consultants.engine.coder_defaults so future re-baselines
        # are a single-file edit + a CHANGELOG / baselines-ledger
        # row. See docs/consultants-skill-eval-baselines.md.
        from consultants.engine.coder_defaults import RECOMMENDED_CODER_MODEL
        cfg = cc.ConsultantsConfig()
        assert cfg.roles["coder"].model == RECOMMENDED_CODER_MODEL
        assert RECOMMENDED_CODER_MODEL == "glm-5.1:cloud"

    def test_coder_limits_defaults(self):
        # M10: 50 KB per file, 1 MB total, 16 files max — the
        # CHANGELOG entry pins these as the shipped defaults.
        cfg = cc.ConsultantsConfig()
        assert cfg.coder_limits.max_file_bytes == 50 * 1024
        assert cfg.coder_limits.max_total_bytes == 1024 * 1024
        assert cfg.coder_limits.max_files == 16

    def test_load_with_no_files_returns_defaults(self, isolated_home):
        from consultants.engine.coder_defaults import RECOMMENDED_CODER_MODEL
        cfg = cc.load_config()
        assert cfg.topology == cc.DEFAULT_TOPOLOGY
        assert cfg.effort == cc.DEFAULT_EFFORT
        assert cfg.service.mode == cc.DEFAULT_SERVICE_MODE
        for r in cc.ROLES:
            # tool_executor and coder both ship disabled-by-default
            # AND carry role-specific model picks grounded in the
            # skill-eval bench (M11c pending for tool_executor;
            # M11b 2026-05-16 baseline for coder). Every other role
            # tracks the global DEFAULT_MODEL.
            if r == "tool_executor":
                assert cfg.roles[r].enabled is False
                assert cfg.roles[r].model == "gemma4:31b-cloud"
            elif r == "coder":
                assert cfg.roles[r].enabled is False
                assert cfg.roles[r].model == RECOMMENDED_CODER_MODEL
            else:
                assert cfg.roles[r].enabled is True
                assert cfg.roles[r].model == cc.DEFAULT_MODEL


# ----------------------- save + load round-trip ------------------- #

class TestRoundTrip:
    def test_save_user_then_load(self, isolated_home):
        cc.set_role("planner", model="qwen3.5:cloud", scope="user")
        cfg = cc.load_config()
        assert cfg.roles["planner"].model == "qwen3.5:cloud"
        assert cfg.roles["researcher"].model == cc.DEFAULT_MODEL  # untouched

    def test_project_overrides_user(self, isolated_home, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        cc.set_role("planner", model="qwen3.5:cloud", scope="user")
        cc.set_role("planner", model="custom:tag", scope="project", cwd=proj)
        cfg = cc.load_config(cwd=proj)
        assert cfg.roles["planner"].model == "custom:tag"
        # loading without cwd should still see the user-global value
        cfg2 = cc.load_config()
        assert cfg2.roles["planner"].model == "qwen3.5:cloud"

    def test_partial_project_inherits_other_roles(self, isolated_home,
                                                  tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        cc.set_role("planner", model="user-pl", scope="user")
        cc.set_role("researcher", model="user-rs", scope="user")
        cc.set_role("planner", model="proj-pl", scope="project", cwd=proj)
        cfg = cc.load_config(cwd=proj)
        assert cfg.roles["planner"].model == "proj-pl"
        assert cfg.roles["researcher"].model == "user-rs"

    def test_corrupt_toml_falls_back_to_defaults(self, isolated_home):
        p = cc.user_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not [valid toml")
        cfg = cc.load_config()
        assert cfg.topology == cc.DEFAULT_TOPOLOGY


# ----------------------- set-role ---------------------------------- #

class TestSetRole:
    def test_set_model(self, isolated_home):
        cc.set_role("planner", model="kimi-k2.6:cloud")
        cfg = cc.load_config()
        assert cfg.roles["planner"].model == "kimi-k2.6:cloud"

    def test_set_ctx_marks_explicit(self, isolated_home):
        cc.set_role("researcher", ctx_max=8000)
        cfg = cc.load_config()
        assert cfg.roles["researcher"].ctx_max == 8000
        assert cfg.roles["researcher"].ctx_max_explicit is True

    def test_clear_ctx_with_zero(self, isolated_home):
        cc.set_role("researcher", ctx_max=8000)
        cc.set_role("researcher", ctx_max=0)
        cfg = cc.load_config()
        assert cfg.roles["researcher"].ctx_max is None
        assert cfg.roles["researcher"].ctx_max_explicit is False

    def test_negative_ctx_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_role("researcher", ctx_max=-1)

    def test_disable_optional_role(self, isolated_home):
        cc.set_role("critic", enabled=False)
        cfg = cc.load_config()
        assert cfg.roles["critic"].enabled is False

    def test_disable_synthesizer_rejected(self, isolated_home):
        with pytest.raises(ValueError) as ei:
            cc.set_role("synthesizer", enabled=False)
        assert "mandatory" in str(ei.value)

    def test_unknown_role_rejected(self, isolated_home):
        with pytest.raises(ValueError) as ei:
            cc.set_role("nope", model="x")
        assert "unknown role" in str(ei.value)

    def test_empty_model_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_role("planner", model="")
        with pytest.raises(ValueError):
            cc.set_role("planner", model="   ")

    def test_synthesizer_force_enabled_on_load(self, isolated_home):
        # Even if someone hand-edits the file to disable synthesizer,
        # load_config flips it back on.
        p = cc.user_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            'topology = "council"\n'
            'effort = "medium"\n\n'
            '[service]\n'
            'mode = "always-on"\n'
            'http_port = 38095\n\n'
            '[role.synthesizer]\n'
            'enabled = false\n'
            'model = "x:y"\n'
            'ctx_max_explicit = false\n'
        )
        cfg = cc.load_config()
        assert cfg.roles["synthesizer"].enabled is True


# ----------------------- set-effort -------------------------------- #

class TestSetEffort:
    @pytest.mark.parametrize("tier", ["low", "medium", "high", "max"])
    def test_valid_tiers(self, isolated_home, tier):
        cc.set_effort(tier)
        cfg = cc.load_config()
        assert cfg.effort == tier

    def test_invalid_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_effort("infinite")

    def test_budget_lookup(self):
        cfg = cc.ConsultantsConfig()
        cfg.effort = "high"
        assert cfg.effort_budget == 5
        cfg.effort = "low"
        assert cfg.effort_budget == 1


# ----------------------- set-service-mode -------------------------- #

class TestSetServiceMode:
    @pytest.mark.parametrize("mode", ["always-on", "smart-start"])
    def test_valid_modes(self, isolated_home, mode):
        cc.set_service_mode(mode)
        cfg = cc.load_config()
        assert cfg.service.mode == mode

    def test_invalid_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_service_mode("on-demand")


# ----------------------- validate_pipeline ------------------------- #

class TestValidatePipeline:
    def test_default_valid(self):
        cfg = cc.ConsultantsConfig()
        assert cc.validate_pipeline(cfg) is None

    def test_critic_off_still_valid(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["critic"].enabled = False
        assert cc.validate_pipeline(cfg) is None

    def test_planner_only_valid(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["researcher"].enabled = False
        cfg.roles["critic"].enabled = False
        assert cc.validate_pipeline(cfg) is None

    def test_researcher_only_valid(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["planner"].enabled = False
        cfg.roles["critic"].enabled = False
        assert cc.validate_pipeline(cfg) is None

    def test_no_planner_or_researcher_invalid(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["planner"].enabled = False
        cfg.roles["researcher"].enabled = False
        cfg.roles["critic"].enabled = False
        err = cc.validate_pipeline(cfg)
        assert err is not None
        assert "planner / researcher" in err

    def test_enabled_roles_in_pipeline_order(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["critic"].enabled = False
        order = cc.enabled_roles(cfg)
        assert order == ["planner", "researcher", "synthesizer"]


# ----------------------- TOML formatting --------------------------- #

class TestTomlEmit:
    def test_emitted_toml_parses_back(self, isolated_home):
        cc.set_role("planner", model="x:y", ctx_max=8000)
        cc.set_effort("high")
        cc.set_service_mode("smart-start")
        cfg = cc.load_config()
        # Round trip via the on-disk file
        text = cc.user_config_path().read_text()
        assert 'model = "x:y"' in text
        assert "ctx_max = 8000" in text
        assert "ctx_max_explicit = true" in text
        assert 'effort = "high"' in text
        assert 'mode = "smart-start"' in text

    def test_quote_special_chars(self, isolated_home):
        # A path-like model tag with a backslash (unlikely but legal).
        cc.set_role("planner", model='weird\\tag')
        text = cc.user_config_path().read_text()
        # The double backslash escapes through.
        assert r'"weird\\tag"' in text
        cfg = cc.load_config()
        assert cfg.roles["planner"].model == "weird\\tag"
