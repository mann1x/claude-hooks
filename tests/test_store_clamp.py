"""Store-side payload clamp — hooks/stop.py ``max_store_chars``.

Recall has bounded its queries for a long time (``max_query_chars``);
the store path never did. That asymmetry mattered because both go to the
same single embedder, whose latency is steeply superlinear in payload
size (measured on solidpc's CPU llamafile: 500 ch = 0.7 s, 4 k = 6.0 s,
12 k = 27 s, 30 k > 87 s). A background turn-store therefore sat in
front of the user's interactive recall for seconds at a time.

``max_chars`` in ``embedder_options`` is not the same knob: that is a
context-overflow guard at 30 k, three orders of magnitude away from
where the cost actually bites. This one is a latency budget.
"""
from claude_hooks.hooks import stop


class TestClampMechanics:
    def test_short_summary_is_untouched(self):
        s = "a short turn summary"
        assert stop._clamp_for_store(s, {}) == s

    def test_long_summary_is_clamped_to_default(self):
        out = stop._clamp_for_store("x" * 10_000, {})
        assert len(out) <= stop.DEFAULT_MAX_STORE_CHARS

    def test_explicit_budget_is_honoured(self):
        out = stop._clamp_for_store("x" * 10_000, {"max_store_chars": 500})
        assert len(out) <= 500

    def test_zero_disables_the_clamp(self):
        """Matches the convention used by max_query_chars and ef_search."""
        s = "x" * 10_000
        assert stop._clamp_for_store(s, {"max_store_chars": 0}) == s

    def test_negative_disables_the_clamp(self):
        s = "x" * 10_000
        assert stop._clamp_for_store(s, {"max_store_chars": -1}) == s

    def test_garbage_budget_falls_back_to_default(self, caplog):
        with caplog.at_level("WARNING", logger="claude_hooks.hooks.stop"):
            out = stop._clamp_for_store("x" * 10_000,
                                        {"max_store_chars": "lots"})
        assert len(out) <= stop.DEFAULT_MAX_STORE_CHARS
        assert "invalid max_store_chars" in caplog.text

    def test_clamping_is_logged(self, caplog):
        with caplog.at_level("INFO", logger="claude_hooks.hooks.stop"):
            stop._clamp_for_store("x" * 10_000, {"max_store_chars": 500})
        assert "summary clamped for store" in caplog.text

    def test_no_log_when_nothing_was_cut(self, caplog):
        with caplog.at_level("INFO", logger="claude_hooks.hooks.stop"):
            stop._clamp_for_store("short", {})
        assert "summary clamped for store" not in caplog.text


class TestWhatSurvives:
    """A turn's *outcome* is at the end of the summary. A head-only
    truncation would keep every summary's preamble and drop the result
    it exists to record, which is worse than storing nothing."""

    def _summary(self):
        return ("# Turn @ 2026-09-09\n"
                "## Prompt\nthe original question\n"
                + ("filler line\n" * 2000)
                + "## Result\nTHE DECISIVE OUTCOME\n")

    def test_head_survives(self):
        out = stop._clamp_for_store(self._summary(), {"max_store_chars": 800})
        assert "the original question" in out

    def test_tail_survives(self):
        out = stop._clamp_for_store(self._summary(), {"max_store_chars": 800})
        assert "THE DECISIVE OUTCOME" in out

    def test_budget_is_respected_even_with_both_ends_kept(self):
        out = stop._clamp_for_store(self._summary(), {"max_store_chars": 800})
        assert len(out) <= 800


class TestWiredIntoTheStopHook:
    """The helper being right is not the same as it being called. This
    is the assertion that would have caught the original gap: the store
    path simply never had a budget."""

    def _run(self, base_config, transcript_file, fake_provider, **stop_cfg):
        cfg = base_config(hooks={"stop": dict(
            {"store_threshold": "noteworthy", "detach_store": False},
            **stop_cfg)})
        path = transcript_file(
            user="do the thing",
            assistant_text="here is a very long answer " + ("y" * 20_000),
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
        )
        p = fake_provider()
        stop.handle(event={"transcript_path": path, "cwd": "/p"},
                    config=cfg, providers=[p])
        return p

    def test_stored_summary_is_clamped(self, base_config, transcript_file,
                                       fake_provider):
        p = self._run(base_config, transcript_file, fake_provider)
        assert p.stored, "expected a store on a noteworthy turn"
        content = p.stored[-1][0]
        assert len(content) <= stop.DEFAULT_MAX_STORE_CHARS

    def test_configured_budget_reaches_the_store(self, base_config,
                                                 transcript_file,
                                                 fake_provider):
        p = self._run(base_config, transcript_file, fake_provider,
                      max_store_chars=600)
        assert p.stored
        assert len(p.stored[-1][0]) <= 600

    def test_opt_out_stores_more_than_a_budget_does(self, base_config,
                                                    transcript_file,
                                                    fake_provider):
        """Relative, not absolute: ``_build_summary`` already truncates
        individual fields, so an opted-out summary is not necessarily
        over any particular size. What must hold is that the knob
        changes what reaches the provider."""
        opted_out = self._run(base_config, transcript_file, fake_provider,
                              max_store_chars=0).stored[-1][0]
        budgeted = self._run(base_config, transcript_file, fake_provider,
                             max_store_chars=600).stored[-1][0]
        assert len(opted_out) > len(budgeted)
        assert len(budgeted) <= 600


class TestDefaultIsBelowObservedPayloads:
    def test_default_bites_at_the_measured_p50(self):
        """Observed stored-summary p50 was ~2.9 KB. A budget above that
        would be decorative — it has to cut the common case to move the
        latency it exists to move."""
        assert stop.DEFAULT_MAX_STORE_CHARS < 2900

    def test_default_still_holds_a_useful_summary(self):
        """...but not so small that a turn stops being recallable."""
        assert stop.DEFAULT_MAX_STORE_CHARS >= 1000
