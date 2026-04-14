"""Unit tests for claude_hooks.instincts."""

from __future__ import annotations

from pathlib import Path

from claude_hooks.instincts import (
    Instinct,
    _derive_title,
    detect_bug_fix,
    extract_instinct,
    merge_if_duplicate,
    save_instinct,
)


def _msg(role, *blocks):
    return {"message": {"role": role, "content": list(blocks)}}


def _tool_use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def _tool_result(text):
    return {"type": "tool_result", "content": text}


class TestDetectBugFix:
    def test_none_on_empty(self):
        assert detect_bug_fix([]) is None
        assert detect_bug_fix(None) is None

    def test_detects_error_followed_by_edit(self):
        # detect_bug_fix looks at content blocks AFTER the last 'user' role
        # message, so the tool_result and the Edit have to live on messages
        # that come after the user prompt. Putting both on a single
        # assistant message preserves block order.
        transcript = [
            _msg("user", {"type": "text", "text": "fix this"}),
            _msg(
                "assistant",
                _tool_use("Bash", command="python foo.py"),
                _tool_result("Traceback (most recent call last):\nValueError: bad"),
                _tool_use("Edit", file_path="foo.py",
                          new_string="corrected code here"),
            ),
        ]
        result = detect_bug_fix(transcript)
        assert result is not None
        assert "Traceback" in result["error_text"]
        assert result["fix_file"] == "foo.py"
        assert "corrected" in result["fix_snippet"]

    def test_none_when_no_error(self):
        transcript = [
            _msg("user", {"type": "text", "text": "edit"}),
            _msg("assistant", _tool_use("Edit", file_path="x.py", new_string="y")),
        ]
        assert detect_bug_fix(transcript) is None

    def test_none_when_error_but_no_fix(self):
        transcript = [
            _msg("user", {"type": "text", "text": "run"}),
            _msg(
                "assistant",
                _tool_use("Bash", command="x"),
                _tool_result("ERROR: bad"),
                # No Edit — conversation just ends.
            ),
        ]
        assert detect_bug_fix(transcript) is None


class TestDeriveTitle:
    def test_extracts_error_type(self):
        assert "ValueError" in _derive_title("ValueError: bad input", "foo.py")
        assert "foo.py" in _derive_title("ValueError: bad input", "foo.py")

    def test_extracts_common_failures(self):
        assert "command not found" in _derive_title(
            "bash: foo: command not found", "x"
        ).lower()
        assert "permission denied" in _derive_title(
            "permission denied", "x"
        ).lower()

    def test_falls_back_without_keywords(self):
        t = _derive_title("something broke somehow", "path/to/file.py")
        assert "file.py" in t or "code" in t


class TestExtractInstinct:
    def test_shape(self):
        bug = {
            "error_text": "NameError: foo is undefined",
            "fix_file": "mod.py",
            "fix_snippet": "foo = 1",
        }
        inst = extract_instinct(bug, summary="fix summary", session_id="sess-1")
        assert inst.source_session == "sess-1"
        assert inst.source_file == "mod.py"
        assert "foo = 1" in inst.action or "NameError" in inst.title
        assert 0.0 < inst.confidence <= 1.0


class TestSaveInstinct:
    def test_writes_markdown_with_frontmatter(self, tmp_path):
        inst = Instinct(
            title="ValueError in foo.py",
            action="do the thing",
            evidence="err here",
            confidence=0.7,
            created="2026-04-14T12:00:00+00:00",
            source_session="s1",
            source_file="foo.py",
        )
        path = save_instinct(inst, tmp_path)
        assert path.exists()
        text = path.read_text()
        assert text.startswith("---")
        assert 'title: "ValueError in foo.py"' in text
        assert "confidence: 0.7" in text
        assert "## Action" in text
        assert "## Evidence" in text

    def test_filename_has_slug_and_timestamp(self, tmp_path):
        inst = Instinct(
            title="ValueError in foo.py",
            action="a",
            evidence="e",
            confidence=0.5,
            created="2026-04-14T12:00:00+00:00",
            source_session="s",
            source_file="foo.py",
        )
        path = save_instinct(inst, tmp_path)
        # Slug from lowercase title, non-alnum replaced with '-'.
        assert "valueerror-in-foo-py" in path.stem


class TestMergeIfDuplicate:
    def _inst(self, **overrides):
        base = dict(
            title="T",
            action="a",
            evidence="new evidence",
            confidence=0.6,
            created="2026-04-14T12:00:00+00:00",
            source_session="s",
            source_file="the/same/file.py",
        )
        base.update(overrides)
        return Instinct(**base)

    def test_returns_none_when_no_dir(self, tmp_path):
        # Directory doesn't exist yet.
        fresh_dir = tmp_path / "never-created"
        assert merge_if_duplicate(self._inst(), fresh_dir) is None

    def test_returns_none_when_no_match(self, tmp_path):
        # Save an instinct about a DIFFERENT file, then try to merge a new one.
        existing = self._inst(source_file="other.py", title="Other")
        save_instinct(existing, tmp_path)
        new = self._inst(source_file="the/same/file.py")
        assert merge_if_duplicate(new, tmp_path) is None

    def test_merges_and_bumps_confidence(self, tmp_path):
        existing = self._inst(confidence=0.5, evidence="old evidence")
        save_instinct(existing, tmp_path)
        new = self._inst(evidence="new evidence here", confidence=0.6)
        merged_path = merge_if_duplicate(new, tmp_path)
        assert merged_path is not None
        text = merged_path.read_text()
        # Confidence bumped by 0.1 to ~0.6.
        assert "confidence: 0.6" in text
        # New evidence appended.
        assert "new evidence" in text
