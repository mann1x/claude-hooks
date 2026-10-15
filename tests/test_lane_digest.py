"""Plan D8: every lane is seen — index first, whole reports while they
fit, the rest named with a pager."""
from __future__ import annotations

import json

from consultants.engine import council, lane_digest as ld


def _report(n, size):
    return f"Finding {n}: the cache is keyed on mtime.\n" + "x" * size


def test_reports_that_fit_render_exactly_as_before():
    reports = [_report(1, 100), _report(2, 100)]
    msgs = council.build_synthesizer_messages("q", "p", reports, None)
    body = msgs[1]["content"]
    assert "RESEARCHER REPORT (round 1):" in body
    assert "RESEARCH INDEX" not in body


def test_past_the_budget_every_lane_is_indexed_and_none_is_cut():
    reports = [_report(1, 12_000), _report(2, 20_000), "(researcher lane "
               "failed: HTTP 502)", _report(4, 3_000)]
    msgs = council.build_synthesizer_messages("q", "p", reports, None,
                                              pager=True)
    body = msgs[1]["content"]
    # The index names all four, failed status included.
    for i in range(1, 5):
        assert f"- report {i} [" in body
    assert "- report 3 [failed" in body
    # Report 2 does not fit after 1; the shorter 3 and 4 still go in.
    assert "RESEARCHER REPORT 1 of 4" in body
    assert "RESEARCHER REPORT 2 of 4" not in body
    assert "RESEARCHER REPORT 4 of 4" in body
    assert "NOT SHOWN for space: report 2." in body
    assert "read_lane_report" in body
    # Whole or absent: report 1 is in full.
    assert reports[0].strip() in body


def test_without_tools_the_note_says_the_index_is_all_there_is():
    reports = [_report(1, 20_000), _report(2, 20_000)]
    body = council.build_critic_messages("q", "p", reports)[1]["content"]
    assert "Their index lines above are all you have" in body


def test_pager_is_added_only_when_needed_and_reads_one_report():
    calls = []
    inner = lambda n, a, c: calls.append(n) or "inner"
    specs = [{"type": "function", "function": {"name": "grep"}}]
    small = [_report(1, 10)]
    assert ld.with_pager(small, specs, inner) == (specs, inner)
    big = [_report(1, 20_000), _report(2, 20_000)]
    specs2, run = ld.with_pager(big, specs, inner)
    assert [s["function"]["name"] for s in specs2] == ["grep", "read_lane_report"]
    assert run("read_lane_report", json.dumps({"report": 2}), "/") \
        .startswith("RESEARCHER REPORT 2 of 2:\nFinding 2")
    assert run("read_lane_report", '{"report": 9}', "/").startswith("error:")
    assert run("grep", "{}", "/") == "inner" and calls == ["grep"]
    # No tools, no pager.
    assert ld.with_pager(big, None, None) == (None, None)
