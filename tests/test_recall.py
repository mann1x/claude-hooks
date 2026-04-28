"""Unit tests for claude_hooks.recall.run_recall()."""

from __future__ import annotations

from unittest.mock import patch

from claude_hooks.providers.base import Memory
from claude_hooks.recall import _truncate, format_block, run_recall


class TestFormatBlock:
    def test_basic_block(self):
        mems = [Memory(text="first"), Memory(text="second")]
        out = format_block("Qdrant", mems)
        assert out.startswith("### Qdrant (2)")
        assert "- first" in out
        assert "- second" in out

    def test_multiline_preserved_by_default(self):
        mems = [Memory(text="line1\nline2\nline3")]
        out = format_block("X", mems)
        assert "- line1" in out
        assert "  line2" in out
        assert "  line3" in out

    def test_progressive_collapses(self):
        mems = [Memory(text="line1\nline2\nline3")]
        out = format_block("X", mems, progressive=True)
        assert "- line1" in out
        assert "line2" not in out
        assert "+ chars" in out

    def test_skips_empty_text(self):
        mems = [Memory(text=""), Memory(text="real")]
        out = format_block("X", mems)
        assert "- real" in out
        # Header still counts all inputs (by len).
        assert "(2)" in out


class TestTruncate:
    def test_short_preserved(self):
        assert _truncate("short", 100) == "short"

    def test_no_limit_passes_through(self):
        assert _truncate("anything", 0) == "anything"

    def test_long_truncated_with_marker(self):
        out = _truncate("x" * 500, 200)
        assert len(out) < 220
        assert out.endswith("…(truncated)")


class TestRunRecallSimple:
    def test_no_providers_returns_none(self, base_config):
        assert run_recall("q", config=base_config(), providers=[]) is None

    def test_empty_recall_returns_none(self, base_config, fake_provider):
        p = fake_provider(name="qdrant", recall_returns=[])
        assert run_recall("q", config=base_config(), providers=[p]) is None

    def test_happy_path_formats_context(self, base_config, fake_provider):
        p = fake_provider(
            name="qdrant",
            recall_returns=[Memory(text="memory one"), Memory(text="memory two")],
        )
        out = run_recall("q", config=base_config(), providers=[p])
        assert out is not None
        assert "## Recalled memory" in out
        assert "memory one" in out
        assert "memory two" in out
        # The summary line names the contributing provider(s) explicitly.
        assert "2 hit(s) from Qdrant" in out

    def test_include_providers_filter(self, base_config, fake_provider):
        qdrant = fake_provider(name="qdrant", recall_returns=[Memory(text="from q")])
        kg = fake_provider(name="memory_kg", recall_returns=[Memory(text="from kg")])
        cfg = base_config(hooks={"user_prompt_submit": {"include_providers": ["qdrant"]}})
        out = run_recall("q", config=cfg, providers=[qdrant, kg])
        assert "from q" in out
        assert "from kg" not in out
        # The filtered-out provider was never asked.
        assert kg.recall_calls == []

    def test_recall_error_continues(self, base_config, fake_provider):
        bad = fake_provider(name="qdrant", recall_errors=True)
        good = fake_provider(name="memory_kg", recall_returns=[Memory(text="survived")])
        out = run_recall("q", config=base_config(), providers=[bad, good])
        assert out is not None
        assert "survived" in out

    def test_truncation_applies(self, base_config, fake_provider):
        big = Memory(text="X" * 5000)
        p = fake_provider(name="qdrant", recall_returns=[big])
        out = run_recall("q", config=base_config(), providers=[p], max_total_chars=500)
        assert out.endswith("…(truncated)")


class TestRunRecallWithHyde:
    def test_hyde_skipped_when_raw_empty(self, base_config, fake_provider):
        """Grounded short-circuit: no raw hits → no HyDE call."""
        p = fake_provider(name="qdrant", recall_returns=[])
        cfg = base_config(hooks={"user_prompt_submit": {"hyde_enabled": True}})
        with patch("claude_hooks.recall._hyde_expand") as m_plain, \
             patch("claude_hooks.recall._hyde_expand_grounded") as m_ground:
            out = run_recall("q", config=cfg, providers=[p])
        assert out is None
        m_plain.assert_not_called()
        m_ground.assert_not_called()

    def test_grounded_hyde_uses_raw_hits_as_context(
        self, base_config, fake_provider,
    ):
        p = fake_provider(
            name="qdrant",
            recall_returns=[Memory(text="real memory"), Memory(text="second")],
        )
        cfg = base_config(hooks={"user_prompt_submit": {
            "hyde_enabled": True,
            "hyde_grounded": True,
            "hyde_ground_k": 2,
        }})
        with patch(
            "claude_hooks.recall._hyde_expand_grounded",
            return_value="expanded grounded query",
        ) as m:
            run_recall("q", config=cfg, providers=[p])
        m.assert_called_once()
        args = m.call_args
        # Second positional arg is the list of grounding memories.
        grounding = args.args[1]
        assert "real memory" in grounding
        assert "second" in grounding

    def test_refined_recall_calls_provider_with_expanded_query(
        self, base_config, fake_provider,
    ):
        # Provider returns some raw hits; HyDE expansion triggers a second
        # recall call with the expanded query.
        p = fake_provider(
            name="qdrant",
            recall_returns=[Memory(text="hit")],
        )
        cfg = base_config(hooks={"user_prompt_submit": {
            "hyde_enabled": True,
            "hyde_grounded": True,
        }})
        with patch(
            "claude_hooks.recall._hyde_expand_grounded",
            return_value="expanded-query",
        ):
            run_recall("raw-query", config=cfg, providers=[p])
        # Two recall calls: raw + refined.
        assert len(p.recall_calls) == 2
        queries = [q for q, _ in p.recall_calls]
        assert "raw-query" in queries
        assert "expanded-query" in queries

    def test_hyde_noop_when_expansion_equals_query(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        cfg = base_config(hooks={"user_prompt_submit": {"hyde_enabled": True}})
        # Expansion returns the raw query unchanged — no refined recall.
        with patch(
            "claude_hooks.recall._hyde_expand_grounded",
            return_value="raw-query",
        ):
            run_recall("raw-query", config=cfg, providers=[p])
        assert len(p.recall_calls) == 1  # raw only, no refined call

    def test_plain_hyde_path_when_grounded_disabled(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        cfg = base_config(hooks={"user_prompt_submit": {
            "hyde_enabled": True,
            "hyde_grounded": False,
        }})
        with patch("claude_hooks.recall._hyde_expand", return_value="p") as m_plain, \
             patch("claude_hooks.recall._hyde_expand_grounded") as m_ground:
            run_recall("q", config=cfg, providers=[p])
        m_plain.assert_called_once()
        m_ground.assert_not_called()


class TestRunRecallOpenWolf:
    def test_openwolf_appended_when_available(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        with patch(
            "claude_hooks.openwolf.recall_context",
            return_value="### OpenWolf\n- Do-Not-Repeat: X",
        ):
            out = run_recall("q", config=base_config(), providers=[p],
                              cwd="/some/project", include_openwolf=True)
        assert "OpenWolf" in out

    def test_openwolf_skipped_when_flag_false(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        with patch(
            "claude_hooks.openwolf.recall_context",
            return_value="should not appear",
        ):
            out = run_recall("q", config=base_config(), providers=[p],
                              cwd="/some/project", include_openwolf=False)
        assert "should not appear" not in out
