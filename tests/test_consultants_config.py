"""Tests for ``consultants.config``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from consultants import config as cc


# ----------------------- fixtures ---------------------------------- #

# ``isolated_home`` is provided by tests/conftest.py (cross-platform: POSIX
# $HOME + Windows %USERPROFILE%/%HOMEDRIVE%%HOMEPATH%). The per-file HOME-only
# copy was a silent no-op on Windows — bug-635.


# ----------------------- defaults --------------------------------- #

class TestDefaults:
    def test_roles_listed_in_order(self):
        # M6: tool_executor joins the role registry as opt-in.
        # M10: coder joins too, inserted between critic and synthesizer
        # so the graph topology fans out coder lanes AFTER the critic
        # round (when critic enabled) but BEFORE the synthesizer.
        # M3 (dynamic-adversary): the opt-in ``adversary`` post-synthesis
        # refuter joins last so its singleton node hangs off
        # synthesizer → adversary → END (no per-lane Send; x-tier safe).
        assert cc.ROLES == (
            "planner", "researcher", "tool_executor",
            "critic", "coder", "synthesizer", "adversary",
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
        # adversary (M3) is the third opt-in role — default OFF so the
        # default council stays synthesizer → END (cohort-2 parity).
        cfg = cc.ConsultantsConfig()
        assert cfg.roles["tool_executor"].enabled is False
        assert cfg.roles["coder"].enabled is False
        assert cfg.roles["adversary"].enabled is False
        for r in cc.ROLES:
            if r in ("tool_executor", "coder", "adversary"):
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
        # median_tokens=1841). 2026-08-01 routes the declared
        # successor ``glm-5.2:cloud`` instead — the SCORE is
        # inherited, not re-measured, which is exactly what
        # coder_defaults.MODEL_SUCCESSIONS records. The cohort
        # lists stay frozen at the tags that actually ran.
        # See docs/consultants-skill-eval-baselines.md.
        from consultants.engine.coder_defaults import RECOMMENDED_CODER_MODEL
        cfg = cc.ConsultantsConfig()
        assert cfg.roles["coder"].model == RECOMMENDED_CODER_MODEL
        assert RECOMMENDED_CODER_MODEL == "glm-5.2:cloud"

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
            elif r == "adversary":
                # M3: opt-in post-synthesis refuter, off by default,
                # tracks the global DEFAULT_MODEL until the operator
                # pins one via Subflow G.
                assert cfg.roles[r].enabled is False
                assert cfg.roles[r].model == cc.DEFAULT_MODEL
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


# ----------------------- adversary / verify budget (M1) ---------- #

class TestAdversaryKnobs:
    """M1 — the dynamic-adversary config surface: verify_budget,
    adversary_strictness, adversary_checkpoint (+ timeout). All
    default-OFF / bounded so the council stays byte-identical until an
    operator opts in (cohort-2 parity is asserted in
    test_consultants_v2_parity.py)."""

    def test_defaults(self):
        cfg = cc.ConsultantsConfig()
        assert cfg.verify_budget == cc.DEFAULT_VERIFY_BUDGET == "bounded"
        assert cfg.adversary_strictness == \
            cc.DEFAULT_ADVERSARY_STRICTNESS == "normal"
        assert cfg.adversary_checkpoint is False
        assert cfg.adversary_checkpoint_timeout_s == 600

    def test_set_verify_budget_round_trip(self, isolated_home):
        cc.set_verify_budget("generous")
        cfg = cc.load_config()
        assert cfg.verify_budget == "generous"
        assert 'verify_budget = "generous"' in \
            cc.user_config_path().read_text()

    def test_set_verify_budget_rejects_unknown(self, isolated_home):
        with pytest.raises(ValueError) as ei:
            cc.set_verify_budget("unlimited")
        assert "verify_budget must be one of" in str(ei.value)

    def test_set_adversary_strictness_round_trip(self, isolated_home):
        cc.set_adversary_strictness("strict")
        cfg = cc.load_config()
        assert cfg.adversary_strictness == "strict"
        assert 'adversary_strictness = "strict"' in \
            cc.user_config_path().read_text()

    def test_set_adversary_strictness_rejects_unknown(self, isolated_home):
        with pytest.raises(ValueError) as ei:
            cc.set_adversary_strictness("brutal")
        assert "adversary_strictness must be one of" in str(ei.value)

    def test_set_adversary_checkpoint_round_trip(self, isolated_home):
        cc.set_adversary_checkpoint(True, timeout_s=300)
        cfg = cc.load_config()
        assert cfg.adversary_checkpoint is True
        assert cfg.adversary_checkpoint_timeout_s == 300
        text = cc.user_config_path().read_text()
        assert "adversary_checkpoint = true" in text
        assert "adversary_checkpoint_timeout_s = 300" in text

    def test_set_adversary_checkpoint_timeout_optional(self, isolated_home):
        # Enabling without a timeout keeps the default; a later toggle-off
        # leaves the timeout untouched.
        cc.set_adversary_checkpoint(True)
        cfg = cc.load_config()
        assert cfg.adversary_checkpoint is True
        assert cfg.adversary_checkpoint_timeout_s == 600
        cc.set_adversary_checkpoint(False)
        cfg = cc.load_config()
        assert cfg.adversary_checkpoint is False
        assert cfg.adversary_checkpoint_timeout_s == 600

    def test_set_adversary_checkpoint_rejects_bad_timeout(self, isolated_home):
        with pytest.raises(ValueError):
            cc.set_adversary_checkpoint(True, timeout_s=0)

    def test_merge_ignores_bad_values(self, isolated_home):
        # Out-of-vocab strings / wrong-typed knobs fall back to defaults.
        cc.user_config_path().parent.mkdir(parents=True, exist_ok=True)
        cc.user_config_path().write_text(
            'verify_budget = "huge"\n'
            'adversary_strictness = "savage"\n'
            'adversary_checkpoint = "yes"\n'
            'adversary_checkpoint_timeout_s = 0\n',
            encoding="utf-8")
        cfg = cc.load_config()
        assert cfg.verify_budget == "bounded"
        assert cfg.adversary_strictness == "normal"
        assert cfg.adversary_checkpoint is False
        assert cfg.adversary_checkpoint_timeout_s == 600

    def test_adversary_role_toggle_round_trip(self, isolated_home):
        # The role itself is reached via the shared set_role mutator.
        cc.set_role("adversary", enabled=True)
        cfg = cc.load_config()
        assert cfg.roles["adversary"].enabled is True
        cc.set_role("adversary", enabled=False)
        assert cc.load_config().roles["adversary"].enabled is False


# ----------- per-project override_user_global directive ----------- #
# The flag is a per-project-file-only directive (NOT a ConsultantsConfig
# field). load_config(cwd) merges the project layer iff the file exists
# AND its flag is on (absent/non-bool → treated on). It is read only from
# the raw project TOML (before the merge) and written only to the project
# file via save_config(..., override_flag=...) / set_override_user_global.

class TestOverrideFlagHelpers:
    def test_override_flag_absent_is_true(self):
        # Legacy project files (pre-feature) have no key → merged (on).
        assert cc._override_flag({}) is True
        assert cc._override_flag({"effort": "high"}) is True

    def test_override_flag_non_bool_is_true(self):
        # Defensive: a stray non-bool value never silently turns the
        # project layer off.
        assert cc._override_flag({"override_user_global": "no"}) is True
        assert cc._override_flag({"override_user_global": 0}) is True

    def test_override_flag_explicit_bool(self):
        assert cc._override_flag({"override_user_global": True}) is True
        assert cc._override_flag({"override_user_global": False}) is False

    def test_project_override_active_no_file(self, isolated_home,
                                             tmp_path: Path):
        assert cc.project_override_active(tmp_path) is False

    def test_project_override_active_flag_on(self, isolated_home,
                                             tmp_path: Path):
        cc.set_role("planner", model="proj:tag", scope="project",
                    cwd=tmp_path)  # new file → flag on by default
        assert cc.project_override_active(tmp_path) is True

    def test_project_override_active_flag_off(self, isolated_home,
                                              tmp_path: Path):
        cc.set_role("planner", model="proj:tag", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        assert cc.project_override_active(tmp_path) is False


class TestOverrideFlagGate:
    def test_flag_on_merges_project(self, isolated_home, tmp_path: Path):
        cc.set_role("planner", model="user-pl", scope="user")
        cc.set_role("planner", model="proj-pl", scope="project",
                    cwd=tmp_path)  # flag on by default
        assert cc.load_config(cwd=tmp_path).roles["planner"].model == "proj-pl"

    def test_flag_off_ignores_project(self, isolated_home, tmp_path: Path):
        cc.set_role("planner", model="user-pl", scope="user")
        cc.set_role("planner", model="proj-pl", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        # Flag off → load_config(cwd) is identical to user-global.
        assert cc.load_config(cwd=tmp_path).roles["planner"].model == "user-pl"
        assert (cc.load_config(cwd=tmp_path).roles["planner"].model
                == cc.load_config().roles["planner"].model)

    def test_legacy_file_missing_flag_treated_as_on(self, isolated_home,
                                                    tmp_path: Path):
        # A hand-written project TOML with no override_user_global still
        # merges (preserves the historical "project always wins" behavior).
        p = cc.project_config_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('effort = "max"\n', encoding="utf-8")
        assert cc.load_config(cwd=tmp_path).effort == "max"

    def test_no_project_file_is_user_global(self, isolated_home,
                                            tmp_path: Path):
        cc.set_effort("high", scope="user")
        # Gate is a no-op without a file → byte-identical to load_config().
        assert cc.load_config(cwd=tmp_path).effort == "high"
        assert cc.load_config(cwd=tmp_path).effort == cc.load_config().effort


class TestOverrideFlagRender:
    def test_project_save_emits_flag_line(self, isolated_home,
                                          tmp_path: Path):
        cc.set_role("planner", model="proj:tag", scope="project",
                    cwd=tmp_path)
        text = cc.project_config_path(tmp_path).read_text(encoding="utf-8")
        assert "override_user_global = true" in text

    def test_user_save_omits_flag_line(self, isolated_home):
        # User-global renders must NOT carry the directive (byte-identical
        # to pre-feature output → M12 parity).
        cc.set_role("planner", model="user:tag", scope="user")
        text = cc.user_config_path().read_text(encoding="utf-8")
        assert "override_user_global" not in text

    def test_project_save_flag_off_emits_false(self, isolated_home,
                                               tmp_path: Path):
        cc.set_role("planner", model="proj:tag", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        text = cc.project_config_path(tmp_path).read_text(encoding="utf-8")
        assert "override_user_global = false" in text


class TestSetOverrideUserGlobal:
    def test_creates_file_flag_on(self, isolated_home, tmp_path: Path):
        assert not cc.project_config_path(tmp_path).exists()
        cc.set_override_user_global(True, cwd=tmp_path)
        assert cc.project_config_path(tmp_path).exists()
        assert cc.project_override_active(tmp_path) is True

    def test_creates_file_flag_off(self, isolated_home, tmp_path: Path):
        cc.set_override_user_global(False, cwd=tmp_path)
        assert cc.project_config_path(tmp_path).exists()
        assert cc.project_override_active(tmp_path) is False

    def test_round_trip_on_off_on(self, isolated_home, tmp_path: Path):
        cc.set_role("planner", model="proj:tag", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        assert cc.project_override_active(tmp_path) is False
        cc.set_override_user_global(True, cwd=tmp_path)
        assert cc.project_override_active(tmp_path) is True

    def test_off_preserves_project_content(self, isolated_home,
                                           tmp_path: Path):
        # Turning the flag off must NOT clobber the project file's own
        # content (the dormant snapshot survives so flipping back on
        # restores it).
        cc.set_role("researcher", model="proj-rs", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        raw = cc._read_toml(cc.project_config_path(tmp_path))
        assert raw["role"]["researcher"]["model"] == "proj-rs"
        # Flip back on → the preserved value is active again.
        cc.set_override_user_global(True, cwd=tmp_path)
        assert (cc.load_config(cwd=tmp_path).roles["researcher"].model
                == "proj-rs")

    def test_rejects_non_bool(self, isolated_home, tmp_path: Path):
        with pytest.raises(ValueError):
            cc.set_override_user_global("on", cwd=tmp_path)  # type: ignore

    def test_mutator_preserves_existing_flag(self, isolated_home,
                                             tmp_path: Path):
        # A normal project-scoped set-* after the flag is off must keep
        # the flag off (save_config preserves the on-disk directive when
        # override_flag is not passed) AND preserve the dormant file's
        # other content — a project-scope mutator loads the project layer
        # unconditionally (via _load_for_edit), not through load_config's
        # off-gate, so editing one field never reverts the rest to
        # user-global. (Regression guard: the gated load silently
        # clobbered dormant content — caught by the M4 adversarial review.)
        cc.set_role("planner", model="proj-pl", scope="project",
                    cwd=tmp_path)
        cc.set_override_user_global(False, cwd=tmp_path)
        cc.set_effort("max", scope="project", cwd=tmp_path)
        raw = cc._read_toml(cc.project_config_path(tmp_path))
        assert cc._override_flag(raw) is False
        assert raw["effort"] == "max"                       # the edit landed
        assert raw["role"]["planner"]["model"] == "proj-pl"  # rest survived
        # Flipping back on restores the (still-intact) project content.
        cc.set_override_user_global(True, cwd=tmp_path)
        cfg = cc.load_config(cwd=tmp_path)
        assert cfg.effort == "max"
        assert cfg.roles["planner"].model == "proj-pl"


# ----------------------- set-tools (M-A) -------------------------- #

class TestSetTools:
    """The ``[tools]`` block: what the council can reach, and what each
    tool needs before it runs.

    Every test pins **cwd as well as home**. ``isolated_home`` alone is
    not enough here: ``load_config`` resolves the project file from
    ``os.getcwd()``, and this repo's own project config sets
    ``override_user_global``, so an unpinned mutation test writes into
    the developer's live council config (bug-664).
    """

    @pytest.fixture(autouse=True)
    def _pin_cwd(self, isolated_home, tmp_path, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        return work

    def test_defaults(self):
        cfg = cc.load_config()
        assert cfg.tools.enabled is True
        assert cfg.tools.git is False       # opt-in, see M12 parity
        assert cfg.tools.default_level == "auto"
        assert cfg.tools.permissions == {}

    def test_toggle_git_persists(self):
        cc.set_tools(git=True)
        assert cc.load_config().tools.git is True
        cc.set_tools(git=False)
        assert cc.load_config().tools.git is False

    def test_disable_registry_persists(self):
        cc.set_tools(enabled=False)
        assert cc.load_config().tools.enabled is False

    def test_default_level_validated(self):
        cc.set_tools(default_level="ask_human")
        assert cc.load_config().tools.default_level == "ask_human"
        with pytest.raises(ValueError) as ei:
            cc.set_tools(default_level="allow")   # the pre-ladder value
        assert "invalid permission level" in str(ei.value)

    def test_set_and_clear_a_permission(self):
        cc.set_tools(set_permission=("git_diff", "ask_assistant"))
        assert cc.load_config().tools.permissions == {
            "git_diff": "ask_assistant"}
        cc.set_tools(clear_permission="git_diff")
        assert cc.load_config().tools.permissions == {}

    def test_permission_level_validated(self):
        with pytest.raises(ValueError):
            cc.set_tools(set_permission=("git_diff", "maybe"))

    def test_permission_tool_name_required(self):
        with pytest.raises(ValueError):
            cc.set_tools(set_permission=("  ", "auto"))

    def test_clear_all_permissions(self):
        cc.set_tools(set_permission=("a", "deny"))
        cc.set_tools(set_permission=("b", "ask_human"))
        assert len(cc.load_config().tools.permissions) == 2
        cc.set_tools(clear_all_permissions=True)
        assert cc.load_config().tools.permissions == {}

    def test_none_leaves_values_unchanged(self):
        """The 'pass None to leave unchanged' contract shared with
        set_role / set_store."""
        cc.set_tools(git=True, default_level="deny")
        cc.set_tools(enabled=True)          # touches nothing else
        cfg = cc.load_config()
        assert cfg.tools.git is True
        assert cfg.tools.default_level == "deny"

    def test_permissions_round_trip_through_toml(self):
        cc.set_tools(set_permission=("some_tool", "ask_human"))
        text = cc.user_config_path().read_text(encoding="utf-8")
        assert "[tools.permissions]" in text
        assert cc.load_config().tools.permissions["some_tool"] == "ask_human"

    def test_levels_match_the_registry_enum(self):
        """config duplicates the ladder to stay importable without
        claude_hooks on the path; the two must not drift."""
        from claude_hooks.tool_registry.policy import LEVELS
        assert cc.VALID_PERMISSION_LEVELS == LEVELS
