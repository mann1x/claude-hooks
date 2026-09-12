"""Tests for ``consultants.engine.storage`` and
``consultants.engine.sessions_index``. Both are stdlib-only so the
main test suite runs them without installing the consultants venv."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from consultants.engine import sessions_index, storage


# ----------------------- helpers ---------------------------------- #

def _make_result(**overrides) -> storage.ConsultationResult:
    base = dict(
        session_id="csl-2026-05-06-1",
        created="2026-05-06T20:00:00+02:00",
        question="Audit the latest release notes against the diff.",
        models={
            "planner": "kimi-k2.6:cloud",
            "researcher": "qwen3.5:cloud",
            "critic": "deepseek-v4-pro:cloud",
            "synthesizer": "kimi-k2.6:cloud",
        },
        topology="council",
        effort="medium",
        final_answer="**Verdict**: looks fine. Minor nit on §3.\n",
        turns=[
            storage.RoleTurn(role="planner", round=1, content="Plan: do X.",
                             prompt_tokens=10, completion_tokens=20),
            storage.RoleTurn(role="researcher", round=1,
                             content="Found Y at file.py:42.",
                             tool_calls=[{"name": "read_file",
                                          "arguments": {"path": "file.py"}}],
                             tool_results=[{"content": "line content"}],
                             prompt_tokens=100, completion_tokens=50),
            storage.RoleTurn(role="critic", round=1, content="LGTM.",
                             prompt_tokens=30, completion_tokens=10),
            storage.RoleTurn(role="synthesizer", round=1,
                             content="Final: looks fine.",
                             prompt_tokens=40, completion_tokens=20),
        ],
        duration_seconds=287.5,
        status="completed",
        cwd="/srv/proj",
        total_prompt_tokens=180,
        total_completion_tokens=100,
        retries_by_role={"researcher": 1},
    )
    base.update(overrides)
    return storage.ConsultationResult(**base)


# ----------------------- YAML front-matter ------------------------ #

class TestYamlFrontMatter:
    def test_simple_field_unquoted(self):
        assert storage._yaml_str("council") == "council"
        assert storage._yaml_str("medium") == "medium"

    def test_string_with_colon_quoted(self):
        out = storage._yaml_str("kimi-k2.6:cloud")
        assert out == '"kimi-k2.6:cloud"'

    def test_string_with_newline_escaped(self):
        out = storage._yaml_str("line1\nline2")
        assert out == '"line1\\nline2"'

    def test_string_with_quotes_escaped(self):
        out = storage._yaml_str('say "hi"')
        assert out == '"say \\"hi\\""'

    def test_empty_string_quoted(self):
        assert storage._yaml_str("") == '""'

    def test_leading_dash_quoted(self):
        # Leading "-" would parse as a YAML list marker.
        assert storage._yaml_str("-foo") == '"-foo"'

    def test_front_matter_round_trips_via_pyyaml_if_available(self):
        # Best-effort: if PyYAML is installed in this env, validate
        # we produce parseable YAML.
        try:
            import yaml  # type: ignore
        except ImportError:
            pytest.skip("PyYAML not installed in test env")
        result = _make_result()
        fm_text = storage._yaml_front_matter(result)
        # Strip the leading/trailing fences.
        body = re.sub(r"^---\n|\n---\n$", "", fm_text)
        parsed = yaml.safe_load(body)
        assert parsed["session_id"] == result.session_id
        assert parsed["models"]["planner"] == "kimi-k2.6:cloud"
        assert parsed["status"] == "completed"


# ----------------------- summary.md ------------------------------- #

class TestRenderSummary:
    def test_starts_and_ends_with_fence(self):
        text = storage.render_summary(_make_result())
        assert text.startswith("---\n")
        assert "\n---\n\n" in text

    def test_includes_question_and_models(self):
        text = storage.render_summary(_make_result())
        assert "Audit the latest release notes" in text
        assert "kimi-k2.6:cloud" in text
        assert "qwen3.5:cloud" in text

    def test_body_after_front_matter(self):
        text = storage.render_summary(_make_result())
        body_start = text.index("---\n", 4) + len("---\n") + 1
        body = text[body_start:]
        assert "**Verdict**" in body

    def test_failed_status_emits_error(self):
        # Plain string with no special chars renders bare (valid YAML
        # plain scalar). Quotes only appear when a char triggers
        # _QUOTE_NEEDED_RX.
        r = _make_result(status="failed", error="cloud upstream 500",
                         final_answer="(no final answer)")
        text = storage.render_summary(r)
        assert "status: failed" in text
        assert "error: cloud upstream 500" in text

    def test_failed_status_with_special_chars_quoted(self):
        r = _make_result(status="failed",
                         error="HTTP 400: parser bug",
                         final_answer="(no final answer)")
        text = storage.render_summary(r)
        assert 'error: "HTTP 400: parser bug"' in text


# ----------------------- transcript.md ---------------------------- #

class TestRenderTranscript:
    def test_includes_each_role(self):
        text = storage.render_transcript(_make_result())
        for role in ("Planner", "Researcher", "Critic", "Synthesizer"):
            assert f"## {role}" in text

    def test_question_in_header(self):
        text = storage.render_transcript(_make_result())
        assert "Audit the latest release notes" in text

    def test_tool_calls_rendered(self):
        text = storage.render_transcript(_make_result())
        assert "read_file" in text
        assert "Tool calls" in text

    def test_tool_results_truncated(self):
        long = "x" * 1000
        r = _make_result(turns=[
            storage.RoleTurn(role="researcher", round=1, content="x",
                             tool_calls=[{"name": "read_file",
                                          "arguments": {"path": "a"}}],
                             tool_results=[{"content": long}]),
            storage.RoleTurn(role="synthesizer", round=1, content="ok"),
        ])
        text = storage.render_transcript(r)
        # Don't include the entire 1000-char content; truncated to 400 + …
        assert "…" in text
        assert long not in text

    def test_multi_round_researcher_numbered(self):
        r = _make_result(turns=[
            storage.RoleTurn(role="planner", round=1, content="p"),
            storage.RoleTurn(role="researcher", round=1, content="r1"),
            storage.RoleTurn(role="researcher", round=2, content="r2"),
            storage.RoleTurn(role="synthesizer", round=1, content="s"),
        ])
        text = storage.render_transcript(r)
        assert "## Researcher (round 1)" in text
        assert "## Researcher (round 2)" in text
        # Single-occurrence roles drop the "(round 1)" suffix.
        assert "## Planner" in text
        assert "## Planner (round 1)" not in text


# ----------------------- metadata.json ---------------------------- #

class TestRenderMetadata:
    def test_is_valid_json(self):
        text = storage.render_metadata(_make_result())
        data = json.loads(text)
        assert data["session_id"] == "csl-2026-05-06-1"

    def test_includes_token_totals_and_retries(self):
        text = storage.render_metadata(_make_result())
        data = json.loads(text)
        assert data["total_prompt_tokens"] == 180
        assert data["retries_by_role"] == {"researcher": 1}

    def test_turns_serialized(self):
        text = storage.render_metadata(_make_result())
        data = json.loads(text)
        assert len(data["turns"]) == 4
        assert data["turns"][0]["role"] == "planner"


# ----------------------- write_consultation ---------------------- #

class TestWriteConsultation:
    def test_creates_three_files(self, tmp_path: Path):
        d = storage.write_consultation(_make_result(), cwd=tmp_path)
        assert (d / storage.SUMMARY_FILENAME).exists()
        assert (d / storage.TRANSCRIPT_FILENAME).exists()
        assert (d / storage.METADATA_FILENAME).exists()

    def test_directory_layout(self, tmp_path: Path):
        d = storage.write_consultation(_make_result(), cwd=tmp_path)
        rel = d.relative_to(tmp_path)
        assert rel == Path(".claude-hooks") / "consultants" / "csl-2026-05-06-1"

    def test_idempotent_overwrite(self, tmp_path: Path):
        # Second write with updated content overwrites.
        storage.write_consultation(_make_result(), cwd=tmp_path)
        r2 = _make_result(final_answer="Different answer")
        d = storage.write_consultation(r2, cwd=tmp_path)
        text = (d / storage.SUMMARY_FILENAME).read_text()
        assert "Different answer" in text

    def test_no_temp_files_left(self, tmp_path: Path):
        d = storage.write_consultation(_make_result(), cwd=tmp_path)
        # The atomic write uses tempfile.mkstemp in the dir; ensure
        # nothing's left dangling.
        leftovers = [p for p in d.iterdir()
                     if ".tmp." in p.name or p.name.endswith(".tmp")]
        assert leftovers == []

    def test_session_dir_helper(self, tmp_path: Path):
        d = storage.session_dir(tmp_path, "abc")
        assert d == tmp_path / ".claude-hooks" / "consultants" / "abc"


# ----------------------- sessions index --------------------------- #

class TestSessionsIndex:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert sessions_index.load_index(tmp_path) == []

    def test_append_and_load(self, tmp_path: Path):
        e = sessions_index.SessionEntry(
            session_id="a", created="2026-05-06T20:00:00+02:00",
            question="q", topology="council", effort="medium",
            status="completed", duration_seconds=10.0)
        sessions_index.append(tmp_path, e)
        loaded = sessions_index.load_index(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].session_id == "a"

    def test_question_truncated(self, tmp_path: Path):
        long_q = "x" * 1000
        e = sessions_index.SessionEntry(
            session_id="a", created="2026-05-06T20:00:00+02:00",
            question=long_q, topology="council", effort="medium",
            status="completed", duration_seconds=10.0)
        sessions_index.append(tmp_path, e)
        loaded = sessions_index.load_index(tmp_path)
        assert len(loaded[0].question) == sessions_index.QUESTION_PREVIEW_LIMIT

    def test_replace_on_same_id(self, tmp_path: Path):
        e1 = sessions_index.SessionEntry(
            session_id="a", created="t", question="q", topology="council",
            effort="medium", status="running", duration_seconds=0.0)
        sessions_index.append(tmp_path, e1)
        e2 = sessions_index.SessionEntry(
            session_id="a", created="t", question="q", topology="council",
            effort="medium", status="completed", duration_seconds=42.0)
        sessions_index.append(tmp_path, e2)
        loaded = sessions_index.load_index(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].status == "completed"
        assert loaded[0].duration_seconds == 42.0

    def test_distinct_ids_accumulate(self, tmp_path: Path):
        for i in range(3):
            sessions_index.append(tmp_path, sessions_index.SessionEntry(
                session_id=f"s{i}", created="t", question="q",
                topology="council", effort="medium",
                status="completed", duration_seconds=0.0))
        assert [e.session_id for e in sessions_index.load_index(tmp_path)] \
            == ["s0", "s1", "s2"]

    def test_list_recent_reverses(self, tmp_path: Path):
        for i in range(3):
            sessions_index.append(tmp_path, sessions_index.SessionEntry(
                session_id=f"s{i}", created="t", question="q",
                topology="council", effort="medium",
                status="completed", duration_seconds=0.0))
        recent = sessions_index.list_recent(tmp_path, limit=2)
        assert [e.session_id for e in recent] == ["s2", "s1"]

    def test_get_by_id(self, tmp_path: Path):
        sessions_index.append(tmp_path, sessions_index.SessionEntry(
            session_id="abc", created="t", question="q",
            topology="council", effort="medium",
            status="completed", duration_seconds=0.0))
        assert sessions_index.get(tmp_path, "abc").session_id == "abc"
        assert sessions_index.get(tmp_path, "missing") is None

    def test_corrupt_file_returns_empty(self, tmp_path: Path):
        p = sessions_index.index_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json")
        assert sessions_index.load_index(tmp_path) == []


# ----------------------- allowed roots in metadata ---------------- #

class TestRootsInMetadata:
    """``extra_roots`` / ``cwd_display`` / ``extra_roots_display`` land
    in metadata.json.

    Added 2026-08-02. Their absence is what made the
    csl-2026-08-02-0532-d737 post-mortem read a reopened session's
    ``extra_roots = None`` as evidence the roots had been dropped —
    when the field was simply never persisted. That false lead cost an
    investigation round on the way to a real bug. With the roots on
    disk, "the roots were wrong" and "the model made it up" are
    distinguishable after the fact.
    """

    def test_roots_round_trip_through_metadata_json(self):
        result = _make_result(
            extra_roots=["/srv/real/a", "/srv/real/b"],
            cwd_display="/shared/proj",
            extra_roots_display=["/shared/a", "/shared/b"],
        )
        payload = json.loads(storage.render_metadata(result))
        assert payload["extra_roots"] == ["/srv/real/a", "/srv/real/b"]
        assert payload["cwd_display"] == "/shared/proj"
        assert payload["extra_roots_display"] == ["/shared/a", "/shared/b"]

    def test_defaults_are_empty_not_absent(self):
        # An empty list and a missing key read very differently in a
        # post-mortem: one says "no extra roots", the other says
        # "this version didn't record them".
        payload = json.loads(storage.render_metadata(_make_result()))
        assert payload["extra_roots"] == []
        assert payload["extra_roots_display"] == []
        assert payload["cwd_display"] is None

    def test_front_matter_stays_list_free(self):
        # summary.md's YAML is a hand-rolled, deliberately narrow
        # subset with no list emitter. Adding list fields to
        # ConsultationResult must not leak into it.
        text = storage.render_summary(_make_result(
            extra_roots=["/srv/real/a"],
            extra_roots_display=["/shared/a"],
        ))
        front = text.split("---")[1]
        assert "extra_roots" not in front
