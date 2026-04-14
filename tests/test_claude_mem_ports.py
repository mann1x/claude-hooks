"""
Tests for the four features ported from thedotmack/claude-mem:

    Port 1: structured XML observation summary
    Port 2: metadata-gated semantic rerank
    Port 3: system-reminder / persisted-output tag strip before storing
    Port 4: null-byte-delimited composite dedup hash
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_hooks.decay import memory_hash
from claude_hooks.hooks.stop import (
    _build_summary, _build_summary_xml, _classify_turn_type,
    _derive_title, _extract_text, _strip_system_tags,
)
from claude_hooks.providers.base import Memory
from claude_hooks.recall import _apply_metadata_filter


# ================================================================ #
# Port 3 — tag strip
# ================================================================ #
class TestTagStrip:
    def test_strips_system_reminder_block(self):
        raw = (
            "hello\n"
            "<system-reminder>remember X</system-reminder>\n"
            "world"
        )
        assert _strip_system_tags(raw) == "hello\n\nworld"

    def test_strips_persisted_output(self):
        raw = "a\n<persisted-output>cached stuff</persisted-output>\nb"
        assert _strip_system_tags(raw) == "a\n\nb"

    def test_strips_command_name_family(self):
        raw = (
            "<command-name>/wrapup</command-name>"
            "<command-message>compact</command-message>"
            "<command-args>--foo</command-args>"
            "actual content"
        )
        assert _strip_system_tags(raw) == "actual content"

    def test_strips_local_command_stdout(self):
        raw = "<local-command-stdout>ls output</local-command-stdout>\nok"
        assert _strip_system_tags(raw) == "ok"

    def test_empty_string_passes(self):
        assert _strip_system_tags("") == ""

    def test_no_tags_unchanged(self):
        assert _strip_system_tags("just text") == "just text"

    def test_nested_tags_greedy(self):
        raw = "a<system-reminder>outer<system-reminder>inner</system-reminder></system-reminder>b"
        out = _strip_system_tags(raw)
        # Greedy strip removes the outer-to-outer span including the inner.
        assert out == "ab"

    def test_case_insensitive(self):
        raw = "a<SYSTEM-REMINDER>X</SYSTEM-REMINDER>b"
        assert _strip_system_tags(raw) == "ab"

    def test_collapses_extra_newlines(self):
        raw = "before\n\n\n<system-reminder>x</system-reminder>\n\n\nafter"
        out = _strip_system_tags(raw)
        assert "\n\n\n" not in out

    def test_extract_text_runs_strip(self):
        msg = {"message": {"role": "user", "content": [
            {"type": "text",
             "text": "hey\n<system-reminder>noise</system-reminder>\nkeep me"},
        ]}}
        out = _extract_text(msg)
        assert "system-reminder" not in out
        assert "keep me" in out


# ================================================================ #
# Port 4 — composite dedup hash
# ================================================================ #
class TestCompositeDedupHash:
    def test_same_text_same_hash(self):
        m = Memory(text="hello world, this is a decent test payload")
        assert memory_hash(m) == memory_hash(m)

    def test_different_tails_different_hash(self):
        a = Memory(text="x" * 200 + "TAIL-A" * 20)
        b = Memory(text="x" * 200 + "TAIL-B" * 20)
        assert memory_hash(a) != memory_hash(b)

    def test_different_length_different_hash(self):
        # Prefix identical, last-50 identical, only length differs because
        # one has extra content in the middle.
        a = Memory(text="x" * 200 + "m" * 100 + "z" * 50)
        b = Memory(text="x" * 200 + "m" * 300 + "z" * 50)
        assert memory_hash(a) != memory_hash(b)

    def test_whitespace_only_differences_collapse(self):
        a = Memory(text="  hello world  ")
        b = Memory(text="hello world")
        assert memory_hash(a) == memory_hash(b)

    def test_short_texts_still_hash(self):
        a = Memory(text="abc")
        b = Memory(text="xyz")
        assert memory_hash(a) != memory_hash(b)


# ================================================================ #
# Port 1 — structured XML observation
# ================================================================ #
class TestXmlSummary:
    def test_xml_has_required_tags(self):
        event = {"cwd": "/srv/project"}
        xml = _build_summary_xml(
            event, user_text="please fix the bug",
            asst_text="Fixed the off-by-one in parser.py",
            files_modified={"parser.py"}, files_read=set(),
            commands=[],
        )
        assert xml.startswith("<observation")
        assert "<type>" in xml
        assert "<title>" in xml
        assert "<cwd>/srv/project</cwd>" in xml
        assert "<files_modified>" in xml
        assert "<file>parser.py</file>" in xml
        assert xml.rstrip().endswith("</observation>")

    def test_xml_escapes_special_chars(self):
        xml = _build_summary_xml(
            {"cwd": "/p"}, user_text="use <script>alert(1)</script>",
            asst_text="", files_modified=set(), files_read=set(),
            commands=[],
        )
        # HTML-escaped, so the raw <script> tag isn't present.
        assert "<script>" not in xml
        assert "&lt;script&gt;" in xml

    def test_xml_classifies_fix_type(self):
        assert _classify_turn_type(
            "bug in parser", "fixed the traceback", set(), set(), [],
        ) == "fix"

    def test_xml_classifies_feature_type(self):
        assert _classify_turn_type(
            "add a new cache", "implemented cache", set(), set(), [],
        ) == "feature"

    def test_xml_classifies_general_fallback(self):
        assert _classify_turn_type("", "", set(), set(), []) == "general"

    def test_xml_classifies_shell_on_commands_only(self):
        # No keywords in text, no files, but a command ran.
        assert _classify_turn_type("", "", set(), set(), ["ls -la"]) == "shell"

    def test_xml_derive_title_first_line_of_assistant(self):
        t = _derive_title(
            "Fixed the parser\nmore detail here", set(), [],
        )
        assert t == "Fixed the parser"

    def test_xml_derive_title_falls_back_to_files(self):
        t = _derive_title("", {"a.py", "b.py"}, [])
        assert "a.py" in t or "b.py" in t

    def test_build_summary_picks_format_from_arg(self):
        event = {"cwd": "/p"}
        md = _build_summary(event, transcript=None, fmt="markdown")
        xml = _build_summary(event, transcript=None, fmt="xml")
        assert md.startswith("# Turn")
        assert xml.startswith("<observation")

    def test_build_summary_markdown_still_groups_files(self):
        # Back-compat: read + modified land under one "Files touched" heading.
        event = {"cwd": "/p"}
        # Need a fake transcript to reach the heading.
        transcript = [
            {"message": {"role": "user", "content": [
                {"type": "text", "text": "do stuff"}]}},
            {"message": {"role": "assistant", "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "a.py"}},
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "b.py"}},
            ]}},
        ]
        md = _build_summary(event, transcript=transcript, fmt="markdown")
        assert "## Files touched" in md
        assert "- a.py" in md
        assert "- b.py" in md


# ================================================================ #
# Port 2 — metadata-gated filter
# ================================================================ #
class TestMetadataFilter:
    def _m(self, text: str, **meta) -> Memory:
        return Memory(text=text, metadata=dict(meta))

    def test_cwd_match_keeps_matching(self):
        mems = [
            self._m("a", cwd="/proj/foo"),
            self._m("b", cwd="/proj/bar"),
            self._m("c"),  # no cwd — always passes
        ]
        out = _apply_metadata_filter(
            mems, {"require_cwd_match": True}, cwd="/proj/foo",
        )
        texts = [m.text for m in out]
        assert "a" in texts
        assert "c" in texts         # no-cwd memory passes (recall-friendly)
        assert "b" not in texts

    def test_cwd_match_without_cwd_passes_all(self):
        mems = [self._m("a", cwd="/x"), self._m("b", cwd="/y")]
        out = _apply_metadata_filter(
            mems, {"require_cwd_match": True}, cwd="",
        )
        assert len(out) == 2

    def test_observation_type_filter(self):
        mems = [
            self._m("fix1", observation_type="fix"),
            self._m("pref1", observation_type="preference"),
            self._m("no-type"),
        ]
        out = _apply_metadata_filter(
            mems, {"require_observation_type": "fix"}, cwd="/p",
        )
        texts = [m.text for m in out]
        assert "fix1" in texts
        assert "pref1" not in texts
        assert "no-type" in texts   # no-field memories pass

    def test_max_age_days_drops_stale(self):
        import datetime as _dt
        fresh = _dt.datetime.utcnow().isoformat() + "Z"
        stale = (_dt.datetime.utcnow() - _dt.timedelta(days=100)).isoformat() + "Z"
        mems = [
            self._m("fresh", stored_at=fresh),
            self._m("stale", stored_at=stale),
            self._m("undated"),
        ]
        out = _apply_metadata_filter(
            mems, {"max_age_days": 30}, cwd="/p",
        )
        texts = [m.text for m in out]
        assert "fresh" in texts
        assert "undated" in texts
        assert "stale" not in texts

    def test_required_tags_match_any(self):
        mems = [
            self._m("a", tags=["hooks", "mcp"]),
            self._m("b", tags=["ml"]),
            self._m("c"),
        ]
        out = _apply_metadata_filter(
            mems, {"require_tags": ["hooks"]}, cwd="/p",
        )
        texts = [m.text for m in out]
        assert "a" in texts
        assert "c" in texts     # no tags = pass
        assert "b" not in texts

    def test_empty_filter_is_identity(self):
        mems = [self._m("a"), self._m("b")]
        out = _apply_metadata_filter(mems, {}, cwd="/p")
        assert len(out) == 2

    def test_empty_input(self):
        assert _apply_metadata_filter([], {"require_cwd_match": True},
                                      cwd="/p") == []

    def test_bad_timestamp_passes(self):
        # Malformed timestamps should NOT cause a reject.
        m = self._m("x", stored_at="not a date")
        out = _apply_metadata_filter(
            [m], {"max_age_days": 1}, cwd="/p",
        )
        assert len(out) == 1


# ================================================================ #
# Port 5 — PreToolUse file-read gate
# ================================================================ #
class TestFileReadGate:
    def _cfg(self, **overrides):
        from copy import deepcopy
        from claude_hooks.config import DEFAULT_CONFIG
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["hooks"]["pre_tool_use"]["safety_log_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["safety_scan_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["rtk_rewrite_enabled"] = False
        cfg["hooks"]["pre_tool_use"]["enabled"] = True
        for k, v in overrides.items():
            cfg["hooks"]["pre_tool_use"][k] = v
        return cfg

    def test_gate_off_keeps_pattern_filter(self, fake_provider):
        from claude_hooks.hooks.pre_tool_use import handle
        from claude_hooks.providers.base import Memory
        cfg = self._cfg(
            warn_on_tools=["Read", "Edit"],
            warn_on_patterns=["DROP TABLE"],
            file_read_gate=False,
        )
        p = fake_provider(name="qdrant",
                          recall_returns=[Memory(text="prior note about a.py")])
        # Read on a file that doesn't match the pattern -> no output.
        r = handle(
            event={"tool_name": "Read",
                   "tool_input": {"file_path": "/proj/a.py"}},
            config=cfg, providers=[p],
        )
        assert r is None

    def test_gate_on_bypasses_pattern_for_read(self, fake_provider):
        from claude_hooks.hooks.pre_tool_use import handle
        from claude_hooks.providers.base import Memory
        cfg = self._cfg(
            warn_on_tools=["Read", "Edit"],
            warn_on_patterns=["DROP TABLE"],
            file_read_gate=True,
            file_read_gate_tools=["Read", "Edit", "MultiEdit"],
        )
        p = fake_provider(name="qdrant",
                          recall_returns=[Memory(text="prior note about a.py")])
        r = handle(
            event={"tool_name": "Read",
                   "tool_input": {"file_path": "/proj/a.py"}},
            config=cfg, providers=[p],
        )
        assert r is not None
        assert "prior note about a.py" in r["hookSpecificOutput"]["additionalContext"]

    def test_gate_does_not_bypass_patterns_for_bash(self, fake_provider):
        from claude_hooks.hooks.pre_tool_use import handle
        from claude_hooks.providers.base import Memory
        cfg = self._cfg(
            warn_on_tools=["Bash", "Read"],
            warn_on_patterns=["DROP TABLE"],
            file_read_gate=True,
        )
        p = fake_provider(name="qdrant",
                          recall_returns=[Memory(text="prior bash note")])
        r = handle(
            event={"tool_name": "Bash",
                   "tool_input": {"command": "ls"}},
            config=cfg, providers=[p],
        )
        # Bash never bypasses patterns even with the gate on.
        assert r is None

    def test_gate_skips_unknown_tool(self, fake_provider):
        from claude_hooks.hooks.pre_tool_use import handle
        from claude_hooks.providers.base import Memory
        cfg = self._cfg(
            warn_on_tools=["Read", "Glob"],
            warn_on_patterns=["DROP TABLE"],
            file_read_gate=True,
            file_read_gate_tools=["Read"],
        )
        p = fake_provider(name="qdrant",
                          recall_returns=[Memory(text="note")])
        r = handle(
            event={"tool_name": "Glob",
                   "tool_input": {"pattern": "*.py"}},
            config=cfg, providers=[p],
        )
        # Glob isn't in the gate list, so pattern filter still applies
        # and rejects (no "DROP TABLE" in the pattern).
        assert r is None
