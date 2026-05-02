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

    def test_skill_present_emits_additional_context(self):
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

        self.assertIsNotNone(out)
        self.assertIn("hookSpecificOutput", out)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreCompact")
        ac = hso["additionalContext"]
        # Sections present.
        self.assertIn("## 1.", ac)
        # Saved-to pointer is the LAST non-empty content line.
        last_line = [l for l in ac.splitlines() if l.strip()][-1]
        self.assertIn("State summary saved to:", last_line)
        self.assertIn("Read this file", last_line)

    def test_save_to_file_off_skips_pointer(self):
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

        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("State summary saved to:", ac)


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
            # Still emits a (mostly-empty) summary.
            self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
