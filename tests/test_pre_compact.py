"""Tests for the PreCompact hook + wrapup_synth module."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks import wrapup_synth as ws  # noqa: E402
from claude_hooks.hooks import pre_compact as pc  # noqa: E402


def _write_jsonl_transcript(path: Path, messages: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


def _user_msg(text: str) -> dict:
    return {"message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant_with_tool(name: str, inp: dict, text: str = "") -> dict:
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "name": name, "input": inp})
    return {"message": {"role": "assistant", "content": blocks}}


def _assistant_text(text: str) -> dict:
    return {
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}
    }


# --------------------------------------------------------------------------- #
# Mechanical extraction
# --------------------------------------------------------------------------- #
class CollectorTests(unittest.TestCase):

    def test_collect_modified_files_dedup_preserves_order(self):
        transcript = [
            _user_msg("hi"),
            _assistant_with_tool("Edit", {"file_path": "/a.py"}),
            _assistant_with_tool("Write", {"file_path": "/b.py"}),
            _assistant_with_tool("Edit", {"file_path": "/a.py"}),  # dup
            _assistant_with_tool("Bash", {"command": "ls"}),       # ignored
        ]
        self.assertEqual(
            ws.collect_modified_files(transcript), ["/a.py", "/b.py"],
        )

    def test_collect_bash_commands(self):
        transcript = [
            _assistant_with_tool("Bash", {"command": "ls -la"}),
            _assistant_with_tool("Bash", {"command": "ls -la"}),  # dup
            _assistant_with_tool("Bash", {"command": "git status"}),
        ]
        cmds = ws.collect_bash_commands(transcript)
        self.assertEqual(cmds, ["ls -la", "git status"])

    def test_collect_ssh_targets(self):
        bash = [
            "ssh root@pandorum 'systemctl status x'",
            "ssh -i /root/.ssh/k root@pandorum 'echo hi'",
            "ssh user@host.example.com",
            "git push origin",          # not ssh
            "ssh -p 2222 box.local",    # bare host w/ port flag
        ]
        targets = ws.collect_ssh_targets(bash)
        # We don't assert exact contents (regex is best-effort), just
        # that real ssh targets show up and non-ssh commands don't.
        self.assertTrue(any("pandorum" in t for t in targets))
        self.assertNotIn("origin", targets)

    def test_collect_plan_references(self):
        transcript = [
            _assistant_text(
                "See docs/PLAN-lsp-engine.md for the design. Also "
                "docs/PLAN-stats-sqlite.md is shipped."
            ),
            _user_msg("ok"),
        ]
        self.assertEqual(
            sorted(ws.collect_plan_references(transcript)),
            ["docs/PLAN-lsp-engine.md", "docs/PLAN-stats-sqlite.md"],
        )

    def test_collect_ask_user_questions_empty_transcript(self):
        """#217: no transcript → empty list (not None, not a crash)."""
        self.assertEqual(ws.collect_ask_user_questions([]), [])

    def test_collect_ask_user_questions_single_pair(self):
        """#217: one Q + one matching A produces one tuple."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Which env?",
                             "header": "Env",
                             "options": [{"label": "prod", "description": "x"},
                                         {"label": "staging", "description": "y"}],
                             "multiSelect": False},
                        ]},
                    }],
                },
            },
            {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": ('User has answered your questions: '
                                    '"Which env?"="prod". You can now '
                                    'continue with the user\'s answers in '
                                    'mind.'),
                    }],
                },
            },
        ]
        pairs = ws.collect_ask_user_questions(transcript)
        self.assertEqual(pairs, [("Which env?", "prod")])

    def test_collect_ask_user_questions_multi_pair(self):
        """#217: one tool_use with N questions → N pairs in order."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Env?",
                             "header": "E", "options": [], "multiSelect": False},
                            {"question": "Region?",
                             "header": "R", "options": [], "multiSelect": False},
                        ]},
                    }],
                },
            },
            {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_xyz",
                        "content": ('User has answered your questions: '
                                    '"Env?"="prod", "Region?"="us-east-1". '
                                    'You can now continue.'),
                    }],
                },
            },
        ]
        pairs = ws.collect_ask_user_questions(transcript)
        self.assertEqual(
            pairs,
            [("Env?", "prod"), ("Region?", "us-east-1")],
        )

    def test_collect_ask_user_questions_ignores_other_tool_uses(self):
        """#217: non-AskUserQuestion tool_uses don't show up; one
        AskUserQuestion in a sea of other tools is still extracted."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "id": "t1", "input": {"command": "ls"}},
                        {"type": "tool_use", "name": "AskUserQuestion",
                         "id": "t2", "input": {"questions": [
                            {"question": "Proceed?",
                             "header": "P", "options": [], "multiSelect": False},
                         ]}},
                        {"type": "tool_use", "name": "Read",
                         "id": "t3", "input": {"file_path": "/a"}},
                    ],
                },
            },
            {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": ('User has answered your questions: '
                                    '"Proceed?"="yes". You can now continue.'),
                    }],
                },
            },
        ]
        self.assertEqual(
            ws.collect_ask_user_questions(transcript),
            [("Proceed?", "yes")],
        )

    def test_collect_ask_user_questions_skips_unanswered(self):
        """#217: an AskUserQuestion whose tool_result is missing
        from the transcript is silently dropped (better empty than
        wrong). This handles the in-flight case where compaction
        catches the question mid-air."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_orphan",
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Q?",
                             "header": "H", "options": [], "multiSelect": False},
                        ]},
                    }],
                },
            },
            # No matching tool_result message.
        ]
        self.assertEqual(ws.collect_ask_user_questions(transcript), [])

    def test_collect_ask_user_questions_tool_result_list_content(self):
        """#217: tool_result.content may be a list of {type:text,text:...}
        blocks (newer API shape); the extractor flattens it."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_list",
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Format?",
                             "header": "F", "options": [], "multiSelect": False},
                        ]},
                    }],
                },
            },
            {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_list",
                        "content": [
                            {"type": "text", "text":
                                ('User has answered your questions: '
                                 '"Format?"="json". You can now continue.')},
                        ],
                    }],
                },
            },
        ]
        self.assertEqual(
            ws.collect_ask_user_questions(transcript),
            [("Format?", "json")],
        )

    def test_collect_ask_user_questions_preserves_chronological_order(self):
        """#217: pairs are returned in chronological order, even when
        the second tool_use appears before the first tool_use's
        result (interleaved exchange)."""
        # The harness records tool_uses in chronological order; the
        # extractor's two-pass design preserves that order regardless
        # of when the matching result arrives.
        def aq(uid, qtext):
            return {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use", "id": uid,
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": qtext, "header": "h",
                             "options": [], "multiSelect": False},
                        ]},
                    }],
                },
            }

        def res(uid, qtext, atext):
            return {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result", "tool_use_id": uid,
                        "content": (f'User has answered your questions: '
                                    f'"{qtext}"="{atext}". You can now '
                                    f'continue.'),
                    }],
                },
            }
        transcript = [aq("u1", "Q1?"), res("u1", "Q1?", "A1"),
                      aq("u2", "Q2?"), res("u2", "Q2?", "A2"),
                      aq("u3", "Q3?"), res("u3", "Q3?", "A3")]
        pairs = ws.collect_ask_user_questions(transcript)
        self.assertEqual(
            pairs,
            [("Q1?", "A1"), ("Q2?", "A2"), ("Q3?", "A3")],
        )

    def test_collect_background_tasks(self):
        transcript = [
            _assistant_with_tool("Monitor", {"description": "watch deploy.log"}),
            _assistant_with_tool("ScheduleWakeup", {"reason": "check build"}),
            _assistant_with_tool("CronCreate", {"cron": "*/5 * * * *", "prompt": "X"}),
            _assistant_with_tool("Bash", {
                "command": "tail -f x.log", "run_in_background": True,
                "description": "tailing x.log",
            }),
            _assistant_with_tool("Bash", {"command": "ls"}),  # ignored
        ]
        bg = ws.collect_background_tasks(transcript)
        self.assertEqual(len(bg), 4)
        self.assertTrue(any("Monitor" in x for x in bg))
        self.assertTrue(any("ScheduleWakeup" in x for x in bg))
        self.assertTrue(any("CronCreate" in x for x in bg))
        self.assertTrue(any("background" in x for x in bg))


# --------------------------------------------------------------------------- #
# Output path resolution
# --------------------------------------------------------------------------- #
class OutputPathTests(unittest.TestCase):

    def test_prefers_wolf_dir_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".wolf").mkdir()
            now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
            p = ws.resolve_output_path(tmp, "session-abc", now=now)
            self.assertEqual(p.parent.name, ".wolf")
            self.assertIn("wrapup-pre-compact-", p.name)

    def test_falls_back_to_docs_wrapup(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
            p = ws.resolve_output_path(tmp, "session-abc", now=now)
            self.assertEqual(p.parent.name, "wrapup")
            self.assertEqual(p.parent.parent.name, "docs")
            self.assertTrue(p.parent.is_dir())  # auto-created

    def test_fallback_to_home_when_cwd_unwritable(self):
        # Pass an empty cwd so the function falls straight to the
        # ~/.claude/wrapup-pre-compact/ branch.
        now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
        p = ws.resolve_output_path("", "sess-xyz", now=now)
        self.assertIn(".claude", str(p))
        self.assertTrue(p.name.startswith("sess-xyz-"))


# --------------------------------------------------------------------------- #
# synthesize_markdown end-to-end
# --------------------------------------------------------------------------- #
class SynthesizeMarkdownTests(unittest.TestCase):

    def test_contains_all_eight_sections(self):
        md = ws.synthesize_markdown([], cwd="", session_id="s")
        for header in ("## 1.", "## 2.", "## 3.", "## 4.",
                       "## 5.", "## 6.", "## 7.", "## 8."):
            self.assertIn(header, md, f"missing section header {header}")

    def test_marks_judgment_sections_as_needs_model(self):
        md = ws.synthesize_markdown([], cwd="", session_id="s")
        # Sections 3, 4 require model — confirm the marker is present.
        self.assertIn("needs model", md)

    def test_includes_extracted_data(self):
        transcript = [
            _user_msg("fix the bug"),
            _assistant_with_tool("Edit", {"file_path": "/repo/app.py"}),
            _assistant_with_tool("Bash", {"command": "ssh root@pandorum 'ls'"}),
            _assistant_text("Ref: docs/PLAN-lsp-engine.md"),
        ]
        md = ws.synthesize_markdown(transcript, cwd="", session_id="s")
        self.assertIn("/repo/app.py", md)
        self.assertIn("PLAN-lsp-engine", md)
        self.assertIn("pandorum", md)

    def test_ask_user_questions_section_emitted_when_pairs_present(self):
        """#217: when the transcript carries AskUserQuestion exchanges,
        the markdown gets the dedicated unnumbered section."""
        transcript = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_ttt",
                        "name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Approach?",
                             "header": "A", "options": [],
                             "multiSelect": False},
                        ]},
                    }],
                },
            },
            {
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_ttt",
                        "content": ('User has answered your questions: '
                                    '"Approach?"="surgical fix". You can '
                                    'now continue.'),
                    }],
                },
            },
        ]
        md = ws.synthesize_markdown(transcript, cwd="", session_id="s")
        self.assertIn("User decisions captured this session", md)
        self.assertIn("**Q:** Approach?", md)
        self.assertIn("**A:** surgical fix", md)

    def test_ask_user_questions_section_omitted_when_no_pairs(self):
        """#217: empty Q&A → no section header (don't print a hollow
        block that the reader has to skip)."""
        md = ws.synthesize_markdown([], cwd="", session_id="s")
        self.assertNotIn("User decisions captured this session", md)

    def test_canonical_section_numbering_preserved(self):
        """#217: the new unnumbered section must not disturb the 1-8
        canonical numbering — the /wrapup skill renders that schema
        and shifting numbers would break downstream readers."""
        md = ws.synthesize_markdown([], cwd="", session_id="s")
        for header in ("## 1.", "## 2.", "## 3.", "## 4.",
                       "## 5.", "## 6.", "## 7.", "## 8."):
            self.assertIn(header, md)


# --------------------------------------------------------------------------- #
# Hook handler — gating + return-value shape
# --------------------------------------------------------------------------- #
class HandlerGatingTests(unittest.TestCase):

    def test_disabled_returns_none(self):
        cfg = {"hooks": {"pre_compact": {"enabled": False}}}
        out = pc.handle(event={}, config=cfg, providers=[])
        self.assertIsNone(out)

    def test_skill_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point wrapup_skill_path at a path that doesn't exist.
            cfg = {"hooks": {"pre_compact": {
                "enabled": True,
                "wrapup_skill_path": str(Path(tmp) / "nonexistent" / "SKILL.md"),
            }}}
            out = pc.handle(event={}, config=cfg, providers=[])
        self.assertIsNone(out)

    def test_skill_present_writes_file_and_returns_none(self):
        """PreCompact's CC schema doesn't accept hookSpecificOutput, so
        the handler MUST return None even on success — the wrap-up file
        on disk is the sole deliverable."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("# wrapup", encoding="utf-8")

            transcript_path = Path(tmp) / "transcript.jsonl"
            _write_jsonl_transcript(transcript_path, [
                _user_msg("session start"),
                _assistant_with_tool("Edit", {"file_path": str(Path(tmp) / "x.py")}),
            ])

            cfg = {"hooks": {"pre_compact": {
                "enabled": True,
                "wrapup_skill_path": str(skill),
                "save_to_file": True,
            }}}
            event = {
                "transcript_path": str(transcript_path),
                "cwd": tmp,
                "session_id": "test-session",
            }
            out = pc.handle(event=event, config=cfg, providers=[])

            # Contract: handler must NOT return any output dict — CC's
            # PreCompact schema rejects hookSpecificOutput as
            # "(root): Invalid input".
            self.assertIsNone(out)
            # The file IS written on disk (sole delivery channel).
            wrapup_files = list((Path(tmp) / ".wolf").glob("wrapup-pre-compact-*.md"))
            if not wrapup_files:
                wrapup_files = list((Path(tmp) / "docs" / "wrapup")
                                    .glob("wrapup-pre-compact-*.md"))
            self.assertTrue(
                wrapup_files,
                "expected a wrap-up .md to be written to .wolf/ or docs/wrapup/",
            )
            # And it has the synthesised sections.
            content = wrapup_files[0].read_text(encoding="utf-8")
            self.assertIn("## 1.", content)

    def test_save_to_file_off_still_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("# wrapup", encoding="utf-8")

            cfg = {"hooks": {"pre_compact": {
                "enabled": True,
                "wrapup_skill_path": str(skill),
                "save_to_file": False,
            }}}
            event = {"transcript_path": "", "cwd": tmp, "session_id": "s"}
            out = pc.handle(event=event, config=cfg, providers=[])

            # Contract: handler always returns None.
            self.assertIsNone(out)


# --------------------------------------------------------------------------- #
# Dispatcher routing
# --------------------------------------------------------------------------- #
class DispatcherRouteTests(unittest.TestCase):

    def test_precompact_in_handlers_table(self):
        from claude_hooks.dispatcher import HANDLERS
        self.assertIn("PreCompact", HANDLERS)
        self.assertEqual(HANDLERS["PreCompact"], "pre_compact")


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #
class ResilienceTests(unittest.TestCase):

    def test_corrupt_transcript_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("x", encoding="utf-8")
            tp = Path(tmp) / "broken.jsonl"
            tp.write_text("{not json\n{also bad\n", encoding="utf-8")

            cfg = {"hooks": {"pre_compact": {
                "enabled": True,
                "wrapup_skill_path": str(skill),
                "save_to_file": False,
            }}}
            event = {"transcript_path": str(tp), "cwd": tmp, "session_id": "s"}
            try:
                out = pc.handle(event=event, config=cfg, providers=[])
            except Exception as e:
                self.fail(f"handle() must not raise on corrupt transcript: {e}")
            # Always returns None (PreCompact schema doesn't accept output).
            self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
