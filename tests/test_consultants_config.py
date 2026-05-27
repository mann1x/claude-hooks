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

    def test_opt_in_roles_default_state(self):
        # tool_executor flip history:
        #   M11c-1 scaffold False → M11c-5 (2026-05-17) True
        #   after the M11c-2 bench + #103 gate cleared →
        #   2026-05-18 back to False after the M14 first-real-ask
        #   tool_executor on/off A/B
        #   (benchmarks/consultants/results/2026-05-18/tool-executor-ab/)
        #   showed the role +12 min wall / +43% tokens AND fewer
        #   edge cases identified on a grep-shaped question. The
        #   M11c-2 bench still validates the role on tool-heavy
        #   reasoning; the default-off recognizes most operator
        #   questions don't look like that bench corpus.
        # coder remains disabled-by-default (operator opts in to
        # sandboxed file writes). Every other role stays enabled.
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
            # 2026-05-18: tool_executor flipped back to disabled-
            # by-default after the M14 first-real-ask A/B
            # (benchmarks/consultants/results/2026-05-18/tool-executor-ab/).
            # The role's MODEL pick stays gemma4:31b-cloud (M11c-2
            # bench winner) for operators who opt in. coder stays
            # disabled (operator opts in to sandboxed file writes)
            # with the M11b coder rubric winner as model. Every
            # other role tracks the global DEFAULT_MODEL.
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


# ----------------------- set-store (#220) ------------------------- #

class TestSetStore:
    def test_toggle_enabled_and_persist(self, isolated_home):
        cc.set_store(enabled=False)
        cfg = cc.load_config()
        assert cfg.store.enabled is False
        cc.set_store(enabled=True)
        cfg = cc.load_config()
        assert cfg.store.enabled is True

    def test_backend_validated(self, isolated_home):
        cc.set_store(backend="pgvector")
        cfg = cc.load_config()
        assert cfg.store.backend == "pgvector"
        with pytest.raises(ValueError) as ei:
            cc.set_store(backend="qdrant")
        assert "backend must be one of" in str(ei.value)

    def test_recall_limit_must_be_positive(self, isolated_home):
        cc.set_store(recall_limit=7)
        cfg = cc.load_config()
        assert cfg.store.recall_limit == 7
        with pytest.raises(ValueError):
            cc.set_store(recall_limit=0)

    def test_paths_and_dsn_set_and_clear(self, isolated_home):
        cc.set_store(
            sqlite_vec_path="/tmp/x.db",
            pgvector_dsn="postgres://u:p@h/db",
            pgvector_table="ctab",
            embedder="ollama",
        )
        cfg = cc.load_config()
        assert cfg.store.sqlite_vec_path == "/tmp/x.db"
        assert cfg.store.pgvector_dsn == "postgres://u:p@h/db"
        assert cfg.store.pgvector_table == "ctab"
        assert cfg.store.embedder == "ollama"
        # Empty string clears overrides. pgvector_dsn / pgvector_table /
        # embedder default to None, so clearing → None. sqlite_vec_path
        # has a non-None default (~/.claude/consultants-store.db); the
        # mutator writes None, but load_config restores the default so
        # the field reverts to its baked-in path rather than going None.
        cc.set_store(sqlite_vec_path="", pgvector_dsn="",
                     pgvector_table="", embedder="")
        cfg = cc.load_config()
        assert cfg.store.pgvector_dsn is None
        assert cfg.store.pgvector_table is None
        assert cfg.store.embedder is None
        # sqlite_vec_path reverts to default rather than None.
        assert cfg.store.sqlite_vec_path == "~/.claude/consultants-store.db"

    def test_enable_at_effort_add_remove_idempotent(self, isolated_home):
        # The field is a tuple by dataclass type; mutator uses a list
        # for in-place edits, load_config rehydrates as a tuple.
        cc.set_store(clear_enable_at_efforts=True)
        cfg = cc.load_config()
        assert tuple(cfg.store.enable_at_efforts) == ()
        cc.set_store(add_enable_at_effort="high")
        cc.set_store(add_enable_at_effort="high")  # idempotent
        cfg = cc.load_config()
        assert tuple(cfg.store.enable_at_efforts) == ("high",)
        cc.set_store(remove_enable_at_effort="high")
        cc.set_store(remove_enable_at_effort="high")  # idempotent
        cfg = cc.load_config()
        assert tuple(cfg.store.enable_at_efforts) == ()

    def test_enable_at_effort_validated(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_store(add_enable_at_effort="infinite")

    def test_none_leaves_unchanged(self, isolated_home):
        cc.set_store(enabled=True, recall_limit=11)
        cc.set_store()  # all defaults None → no-op
        cfg = cc.load_config()
        assert cfg.store.enabled is True
        assert cfg.store.recall_limit == 11


# ----------------------- set-store-ttl (#220) --------------------- #

class TestSetStoreTtl:
    def test_toggle_enabled(self, isolated_home):
        cc.set_store_ttl(enabled=False)
        cfg = cc.load_config()
        assert cfg.store.ttl.enabled is False

    def test_zero_or_negative_means_never(self, isolated_home):
        cc.set_store_ttl(research_days=0)
        cfg = cc.load_config()
        assert cfg.store.ttl.research_days is None
        cc.set_store_ttl(research_days=-5)
        cfg = cc.load_config()
        assert cfg.store.ttl.research_days is None
        cc.set_store_ttl(research_days=30.0)
        cfg = cc.load_config()
        assert cfg.store.ttl.research_days == 30.0

    def test_all_namespace_knobs(self, isolated_home):
        cc.set_store_ttl(
            research_days=14.0,
            tool_results_hours=6.0,
            project_days=180.0,
            user_days=365.0,
        )
        cfg = cc.load_config()
        assert cfg.store.ttl.research_days == 14.0
        assert cfg.store.ttl.tool_results_hours == 6.0
        assert cfg.store.ttl.project_days == 180.0
        assert cfg.store.ttl.user_days == 365.0

    def test_refresh_on_read_toggle(self, isolated_home):
        cc.set_store_ttl(refresh_on_read=False)
        cfg = cc.load_config()
        assert cfg.store.ttl.refresh_on_read is False

    def test_jitter_pct_range_validated(self, isolated_home):
        cc.set_store_ttl(jitter_pct=0.0)
        cc.set_store_ttl(jitter_pct=1.0)
        cc.set_store_ttl(jitter_pct=0.25)
        cfg = cc.load_config()
        assert cfg.store.ttl.jitter_pct == 0.25
        with pytest.raises(ValueError):
            cc.set_store_ttl(jitter_pct=-0.1)
        with pytest.raises(ValueError):
            cc.set_store_ttl(jitter_pct=1.5)


# ----------------------- set-store-distillation (#220) ------------ #

class TestSetStoreDistillation:
    def test_toggle_enabled(self, isolated_home):
        cc.set_store_distillation(enabled=False)
        cfg = cc.load_config()
        assert cfg.store.distillation.enabled is False

    def test_model_must_be_nonempty(self, isolated_home):
        cc.set_store_distillation(model="kimi-k2.6:cloud")
        cfg = cc.load_config()
        assert cfg.store.distillation.model == "kimi-k2.6:cloud"
        with pytest.raises(ValueError):
            cc.set_store_distillation(model="")
        with pytest.raises(ValueError):
            cc.set_store_distillation(model="   ")

    def test_fallback_chain_add_remove_clear(self, isolated_home):
        cc.set_store_distillation(clear_fallback_models=True)
        cfg = cc.load_config()
        assert cfg.store.distillation.fallback_models == ()
        cc.set_store_distillation(add_fallback_model="glm-5.1:cloud")
        cc.set_store_distillation(add_fallback_model="glm-5.1:cloud")  # idempotent
        cc.set_store_distillation(add_fallback_model="kimi-k2.6:cloud")
        cfg = cc.load_config()
        assert cfg.store.distillation.fallback_models == (
            "glm-5.1:cloud", "kimi-k2.6:cloud",
        )
        cc.set_store_distillation(remove_fallback_model="glm-5.1:cloud")
        cfg = cc.load_config()
        assert cfg.store.distillation.fallback_models == ("kimi-k2.6:cloud",)

    def test_fallback_dedup_against_primary(self, isolated_home):
        cc.set_store_distillation(
            clear_fallback_models=True,
            model="kimi-k2.6:cloud",
        )
        cc.set_store_distillation(add_fallback_model="kimi-k2.6:cloud")
        cfg = cc.load_config()
        # Adding the primary as a fallback is a no-op.
        assert cfg.store.distillation.fallback_models == ()

    def test_numeric_caps_validated(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_store_distillation(sweep_interval_seconds=10)  # < 30 minimum
        with pytest.raises(ValueError):
            cc.set_store_distillation(min_entries_per_distillation=0)
        with pytest.raises(ValueError):
            cc.set_store_distillation(max_session_entries=0)
        with pytest.raises(ValueError):
            cc.set_store_distillation(max_groups_per_sweep=-1)
        with pytest.raises(ValueError):
            cc.set_store_distillation(pace_seconds_between_distillations=-1.0)

    def test_215_pacing_knobs(self, isolated_home):
        cc.set_store_distillation(
            max_groups_per_sweep=3,
            pace_seconds_between_distillations=10.0,
        )
        cfg = cc.load_config()
        assert cfg.store.distillation.max_groups_per_sweep == 3
        assert cfg.store.distillation.pace_seconds_between_distillations == 10.0
        # 0 = uncapped is legal.
        cc.set_store_distillation(max_groups_per_sweep=0)
        cfg = cc.load_config()
        assert cfg.store.distillation.max_groups_per_sweep == 0


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
        # M11c-5: tool_executor is now in the default enabled set.
        # Disable it explicitly to keep this test focused on the
        # critic-disable invariant (the pipeline-order contract).
        cfg = cc.ConsultantsConfig()
        cfg.roles["critic"].enabled = False
        cfg.roles["tool_executor"].enabled = False
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


# ----------------------- review-loop knobs --------------------- #

class TestReviewLoopKnobs:
    def test_defaults(self):
        cfg = cc.ConsultantsConfig()
        assert cfg.max_followups == 4
        assert cfg.allow_extra == 1

    def test_set_round_trip(self, isolated_home):
        cc.set_max_followups(6)
        cc.set_allow_extra(3)
        cfg = cc.load_config()
        assert cfg.max_followups == 6
        assert cfg.allow_extra == 3
        text = cc.user_config_path().read_text()
        assert "max_followups = 6" in text
        assert "allow_extra = 3" in text

    def test_max_followups_zero_allowed(self, isolated_home):
        cc.set_max_followups(0)
        assert cc.load_config().max_followups == 0

    def test_max_followups_negative_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_max_followups(-1)

    def test_allow_extra_below_one_rejected(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_allow_extra(0)

    def test_merge_clamps_bad_types(self, isolated_home):
        # Hand-written TOML with bad values is ignored (defaults stand).
        cc.user_config_path().parent.mkdir(parents=True, exist_ok=True)
        cc.user_config_path().write_text(
            'max_followups = -5\nallow_extra = 0\n', encoding="utf-8")
        cfg = cc.load_config()
        assert cfg.max_followups == 4   # -5 rejected → default
        assert cfg.allow_extra == 1     # 0 rejected → default
