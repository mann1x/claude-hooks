"""The recall query clamp (``claude_hooks.recall.clamp_query``).

Until 2026-07-31 nothing bounded the *length* of a recall query.
``min_prompt_chars`` was the only length check in the pipeline, so a
pasted diagnosis or log dump went to the embedder in full -- and the
embedder's own timeout (180 s) sits well above the UserPromptSubmit hook
cap (65 s), so the embedder never gave up first. Claude Code killed the
hook and discarded the output: the entire recall for that turn was lost,
with nothing to show for it but a timeout notice.

Measured on solidpc against the CPU llamafile embedder: 5 000 chars
9.0 s, 16 000 chars 67.6 s. So ~15 KB was already fatal, and the
30 000-char ``max_chars`` ceiling permitted roughly 300 s.

Two properties are worth stating because neither is obvious from the
diff:

1. **The clamp is not a prefix cut.** A long prompt carries its framing
   at the top and its actual ask at the bottom, with pasted bulk in
   between. Dropping the middle is better for recall *quality* -- a
   20 KB query collapses into one mushy centroid vector -- so this is
   not purely a latency fix.
2. **Fenced blocks are squeezed, not dropped.** For "here is the
   traceback, what is it?" the traceback *is* the query, and its
   identifying line is at the top. Dropping the block would discard
   exactly the string the user wants matched.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

from claude_hooks.providers.base import Memory
from claude_hooks.recall import (
    DEFAULT_MAX_QUERY_CHARS,
    _max_query_chars,
    clamp_query,
    run_recall,
)

MAX = DEFAULT_MAX_QUERY_CHARS


def _halves(out: str) -> tuple[str, str]:
    """Split a clamped query at its marker, discarding the marker's own
    newlines so assertions test the *content* boundary, not ``_ELLIPSIS``."""
    head, _, tail = out.partition("…")
    return head.rstrip("\n"), tail.lstrip("\n")


def _prose(n: int, word: str = "alpha") -> str:
    """n-ish chars of word-boundaried filler."""
    out = []
    size = 0
    i = 0
    while size < n:
        chunk = f"{word}{i} "
        out.append(chunk)
        size += len(chunk)
        i += 1
    return "".join(out)[:n]


# ===================================================================== #
# The fast path -- this runs on every prompt, a clamp is the rare case
# ===================================================================== #
class TestFastPath:
    def test_default_is_the_agreed_budget(self):
        assert DEFAULT_MAX_QUERY_CHARS == 3500

    def test_short_query_is_returned_unchanged(self):
        q = "how does the proxy retry layer honour Retry-After?"
        assert clamp_query(q) is q, "must be the same object, not a copy"

    def test_exactly_at_budget_is_untouched(self):
        q = "x" * MAX
        assert clamp_query(q) is q

    def test_one_char_over_is_clamped(self):
        q = _prose(MAX + 1)
        out = clamp_query(q)
        assert out != q
        assert len(out) <= MAX

    def test_zero_disables_the_clamp(self):
        """``0`` means 'leave it alone', matching ``_truncate`` and the
        ``ef_search`` provider option."""
        q = _prose(50_000)
        assert clamp_query(q, 0) is q

    def test_negative_disables_the_clamp(self):
        q = _prose(50_000)
        assert clamp_query(q, -1) is q


# ===================================================================== #
# Budget is absolute
# ===================================================================== #
class TestBudgetHolds:
    def test_never_exceeds_across_sizes(self):
        for n in (3_600, 5_000, 16_000, 30_000, 200_000):
            assert len(clamp_query(_prose(n))) <= MAX, f"overflowed at {n}"

    def test_holds_for_content_with_no_boundaries_at_all(self):
        """Minified JSON has no paragraph, line or sentence break, and
        the backtrack floor stops the boundary hunt from eating the
        whole budget."""
        blob = json.dumps({f"k{i}": f"v{i}" for i in range(4000)},
                          separators=(",", ":"))
        assert " " not in blob and "\n" not in blob
        out = clamp_query(blob)
        assert len(out) <= MAX
        assert len(out) > MAX * 0.5, "backtracking must not gut the budget"

    def test_holds_for_a_single_unbroken_token(self):
        out = clamp_query("z" * 40_000)
        assert len(out) <= MAX

    def test_tiny_budget_still_holds(self):
        assert len(clamp_query(_prose(9_000), 40)) <= 40

    def test_is_idempotent(self):
        once = clamp_query(_prose(30_000))
        assert clamp_query(once) is once


# ===================================================================== #
# Both ends survive -- the reason this isn't text[:3500]
# ===================================================================== #
class TestBothEndsSurvive:
    def _sandwich(self) -> str:
        return ("HEADMARKER the store backend is unreachable\n\n"
                + _prose(20_000, "filler")
                + "\n\nso what should I do about it? TAILMARKER")

    def test_head_is_kept(self):
        assert "HEADMARKER" in clamp_query(self._sandwich())

    def test_tail_is_kept(self):
        assert "TAILMARKER" in clamp_query(self._sandwich())

    def test_middle_is_dropped(self):
        out = clamp_query(self._sandwich())
        assert "filler5000" not in out

    def test_marker_separates_the_halves(self):
        assert "…" in clamp_query(self._sandwich())

    def test_head_gets_the_larger_share(self):
        out = clamp_query(self._sandwich())
        head, _, tail = out.partition("…")
        assert len(head) > len(tail)

    def test_no_content_is_duplicated(self):
        """head and tail windows must not overlap -- provable from
        head_budget + tail_budget < len(text), and cheap to pin."""
        out = clamp_query(self._sandwich())
        assert out.count("HEADMARKER") == 1
        assert out.count("TAILMARKER") == 1


# ===================================================================== #
# Boundary discipline
# ===================================================================== #
class TestBoundaries:
    def test_head_does_not_end_mid_word(self):
        """A mangled trailing token is pure noise in the vector."""
        head, _ = _halves(clamp_query(_prose(20_000, "distinctword")))
        last = head.rsplit(" ", 1)[-1]
        assert last.startswith("distinctword"), f"cut mid-word: {last!r}"

    def test_tail_does_not_start_mid_word(self):
        _, tail = _halves(clamp_query(_prose(20_000, "distinctword")))
        first = tail.split(" ", 1)[0]
        assert first.startswith("distinctword"), f"cut mid-word: {first!r}"

    def test_paragraph_break_is_preferred_over_a_space(self):
        """``\\n\\n`` outranks ``" "`` even though the space is closer to
        the budget: paragraph breaks are where meaning actually ends."""
        text = ("A" * 2000 + "\n\n" + "B" * 100 + " " + "C" * 1000
                + "\n\n" + "tail here")
        head, _ = _halves(clamp_query(text, 2600))
        assert head.endswith("A" * 10), "should have cut at the paragraph break"
        assert "B" not in head, (
            "a space boundary inside the B/C run would have overshot "
            "the paragraph break")


# ===================================================================== #
# Fenced blocks -- squeezed, never dropped
# ===================================================================== #
class TestFencedBlocks:
    def test_long_block_is_squeezed(self):
        text = ("here is the log\n\n```\n" + _prose(20_000, "logline")
                + "\n```\n\nwhat now?")
        out = clamp_query(text)
        assert "here is the log" in out
        assert "what now?" in out
        assert len(out) <= MAX

    def test_the_identifying_line_survives(self):
        """THE case that rules out dropping blocks outright: the
        exception at the top of a traceback is the highest-signal
        string in the whole prompt."""
        tb = ("```python\n"
              "Traceback (most recent call last):\n"
              + "".join(f'  File "m{i}.py", line {i}, in f{i}\n    call{i}()\n'
                        for i in range(400))
              + "psycopg.OperationalError: server closed the connection\n```")
        out = clamp_query("look at this\n\n" + tb + "\n\nwhy?")
        assert "Traceback (most recent call last):" in out
        assert len(out) <= MAX

    def test_language_tag_is_preserved(self):
        text = "q\n\n```python\n" + _prose(9_000, "code") + "\n```\n\nend"
        assert "```python" in clamp_query(text)

    def test_squeezing_alone_can_avoid_a_head_tail_cut(self):
        """When the bulk *is* the block, squeezing gets under budget on
        its own and the surrounding prose survives whole."""
        text = ("intro paragraph that must survive in full\n\n"
                "```\n" + _prose(9_000, "noise") + "\n```\n\n"
                "closing paragraph that must survive in full")
        out = clamp_query(text)
        assert "intro paragraph that must survive in full" in out
        assert "closing paragraph that must survive in full" in out
        assert "noise500" not in out

    def test_short_blocks_are_left_alone(self):
        block = "```\nshort snippet\n```"
        text = "q\n\n" + block + "\n\n" + _prose(9_000, "tail")
        assert block in clamp_query(text)

    def test_unterminated_block_is_handled(self):
        """A prompt that opens a fence and never closes it (truncated
        paste) must not fall through unsqueezed or raise."""
        text = "q\n\n```\n" + _prose(30_000, "open")
        out = clamp_query(text)
        assert len(out) <= MAX

    def test_multiple_blocks_are_each_squeezed(self):
        text = ("a\n\n```\n" + _prose(5_000, "one") + "\n```\n\n"
                "MIDDLE\n\n```\n" + _prose(5_000, "two") + "\n```\n\nz")
        out = clamp_query(text)
        assert "MIDDLE" in out, "prose between two squeezed blocks survives"
        assert len(out) <= MAX


# ===================================================================== #
# Config knob
# ===================================================================== #
class TestConfigKnob:
    def test_default_when_absent(self):
        assert _max_query_chars({}) == DEFAULT_MAX_QUERY_CHARS

    def test_explicit_value_wins(self):
        assert _max_query_chars({"max_query_chars": 1200}) == 1200

    def test_explicit_zero_is_honoured(self):
        """The sentinel trap that bit ``ef_search``: reading the option
        with ``or`` would turn a configured 0 (disable) back into the
        default."""
        assert _max_query_chars({"max_query_chars": 0}) == 0

    def test_string_is_coerced(self):
        assert _max_query_chars({"max_query_chars": "2000"}) == 2000

    def test_garbage_falls_back_to_default(self):
        assert _max_query_chars(
            {"max_query_chars": "lots"}) == DEFAULT_MAX_QUERY_CHARS

    def test_none_falls_back_to_default(self):
        assert _max_query_chars(
            {"max_query_chars": None}) == DEFAULT_MAX_QUERY_CHARS


# ===================================================================== #
# Wiring -- the clamp is worthless if it doesn't reach the embedder
# ===================================================================== #
class TestRunRecallWiring:
    def test_provider_never_sees_an_oversize_query(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        run_recall(_prose(30_000), config=base_config(), providers=[p])
        assert p.recall_calls
        for q, _ in p.recall_calls:
            assert len(q) <= MAX

    def test_normal_prompt_reaches_the_provider_byte_identical(
        self, base_config, fake_provider,
    ):
        """The clamp must be invisible for ordinary prompts."""
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        q = "why did the pgvector provider return zero after the restart?"
        run_recall(q, config=base_config(), providers=[p])
        assert p.recall_calls[0][0] == q

    def test_hyde_prefill_is_clamped_too(self, base_config, fake_provider):
        """HyDE takes the raw query as chat prefill, so it pays the same
        length cost. Clamping at only the embed call site would leave it
        unbounded."""
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        cfg = base_config(hooks={"user_prompt_submit": {
            "hyde_enabled": True, "hyde_grounded": True}})
        with patch("claude_hooks.recall._hyde_expand_grounded",
                   return_value="expanded") as m:
            run_recall(_prose(30_000), config=cfg, providers=[p])
        assert len(m.call_args.args[0]) <= MAX

    def test_disabling_via_config_passes_the_full_query_through(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        cfg = base_config(hooks={"user_prompt_submit": {"max_query_chars": 0}})
        run_recall(_prose(20_000), config=cfg, providers=[p])
        assert len(p.recall_calls[0][0]) == 20_000

    def test_custom_budget_is_respected(self, base_config, fake_provider):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        cfg = base_config(hooks={"user_prompt_submit": {"max_query_chars": 800}})
        run_recall(_prose(20_000), config=cfg, providers=[p])
        assert len(p.recall_calls[0][0]) <= 800

    def test_clamping_is_logged(self, base_config, fake_provider, caplog):
        """Silent truncation would make thin recall unexplainable."""
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        with caplog.at_level(logging.INFO, logger="claude_hooks.recall"):
            run_recall(_prose(30_000), config=base_config(), providers=[p])
        assert any("query clamped" in r.message for r in caplog.records)

    def test_no_log_when_nothing_was_clamped(
        self, base_config, fake_provider, caplog,
    ):
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        with caplog.at_level(logging.INFO, logger="claude_hooks.recall"):
            run_recall("a short question", config=base_config(), providers=[p])
        assert not any("query clamped" in r.message for r in caplog.records)

    def test_session_start_shares_the_bound(self, base_config, fake_provider):
        """``run_recall`` is also called by SessionStart on compact with
        its own hook block; the default has to apply there too rather
        than being wired per-hook."""
        p = fake_provider(name="qdrant", recall_returns=[Memory(text="hit")])
        run_recall(_prose(30_000), config=base_config(), providers=[p],
                   hook_name="session_start")
        assert len(p.recall_calls[0][0]) <= MAX
