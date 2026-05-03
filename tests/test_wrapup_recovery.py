"""Tests for wrapup_synth.collect_endpoints and wrapup_recovery."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks import wrapup_synth as ws  # noqa: E402
from claude_hooks import wrapup_recovery as wr  # noqa: E402


def _assistant_text(text: str) -> dict:
    return {
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}
    }


class CollectEndpointsTests(unittest.TestCase):

    def test_url_extracted_from_text(self):
        transcript = [
            _assistant_text("Connect to https://abcd1234ef-8888.proxy.runpod.net "
                            "for the notebook UI."),
        ]
        out = ws.collect_endpoints(transcript, [])
        self.assertIn("https://abcd1234ef-8888.proxy.runpod.net", out["urls"])
        self.assertIn("abcd1234ef-8888.proxy.runpod.net", out["pod_ids"])

    def test_ipv4_with_port_extracted(self):
        transcript = [_assistant_text("ssh root@192.168.178.25:22 to reach pandorum.")]
        out = ws.collect_endpoints(transcript, [])
        self.assertTrue(any(ip.startswith("192.168.178.25") for ip in out["ips"]))

    def test_invalid_octet_rejected(self):
        transcript = [_assistant_text("Bogus IP 999.1.1.1 should not match.")]
        out = ws.collect_endpoints(transcript, [])
        self.assertNotIn("999.1.1.1", out["ips"])

    def test_extracted_from_bash_commands_too(self):
        bash = ["curl -fsS https://api.runpod.io/v2/abc/health"]
        out = ws.collect_endpoints([], bash)
        self.assertIn("https://api.runpod.io/v2/abc/health", out["urls"])

    def test_dedup_preserves_first_seen(self):
        transcript = [
            _assistant_text("first https://example.com/a"),
            _assistant_text("second https://example.com/a then https://example.com/b"),
        ]
        out = ws.collect_endpoints(transcript, [])
        self.assertEqual(out["urls"][0], "https://example.com/a")
        self.assertEqual(out["urls"][1], "https://example.com/b")

    def test_trailing_punctuation_stripped(self):
        transcript = [_assistant_text("see https://example.com/foo.")]
        out = ws.collect_endpoints(transcript, [])
        self.assertIn("https://example.com/foo", out["urls"])

    def test_synthesize_markdown_includes_endpoints(self):
        transcript = [
            _assistant_text("Pod: https://xyz12345ab-7860.proxy.runpod.net "
                            "and IP 10.0.0.5"),
        ]
        md = ws.synthesize_markdown(transcript, cwd="", session_id="s")
        self.assertIn("Connection state", md)
        self.assertIn("xyz12345ab-7860.proxy.runpod.net", md)
        self.assertIn("10.0.0.5", md)

    def test_synthesize_markdown_no_endpoints_message(self):
        transcript = [_assistant_text("just refactoring some code.")]
        md = ws.synthesize_markdown(transcript, cwd="", session_id="s")
        self.assertIn("no remote endpoints", md)


class WrapupRecoveryTests(unittest.TestCase):

    def test_finds_recent_file_in_wolf_dir(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f = wolf / "wrapup-pre-compact-2026-05-02T10-00-00.md"
            f.write_text("# wrapup")
            found = wr.find_recent_wrapup(td)
            self.assertEqual(found, f)

    def test_finds_recent_file_in_docs_wrapup(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "docs" / "wrapup"
            d.mkdir(parents=True)
            f = d / "wrapup-pre-compact-x.md"
            f.write_text("# wrapup")
            found = wr.find_recent_wrapup(td)
            self.assertEqual(found, f)

    def test_old_file_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f = wolf / "wrapup-pre-compact-old.md"
            f.write_text("# wrapup")
            old = time.time() - (2 * 86400)
            import os as _os
            _os.utime(f, (old, old))
            found = wr.find_recent_wrapup(td, max_age_seconds=86400)
            self.assertIsNone(found)

    def test_picks_most_recent_when_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f1 = wolf / "wrapup-pre-compact-a.md"; f1.write_text("a")
            f2 = wolf / "wrapup-pre-compact-b.md"; f2.write_text("b")
            import os as _os
            _os.utime(f1, (time.time() - 100, time.time() - 100))
            _os.utime(f2, (time.time() - 10, time.time() - 10))
            found = wr.find_recent_wrapup(td)
            self.assertEqual(found, f2)

    def test_no_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            found = wr.find_recent_wrapup(td)
            self.assertIsNone(found)

    def test_format_block_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {"hooks": {"wrapup_recovery": {"enabled": False}}}
            block = wr.format_recovery_block(td, cfg)
            self.assertEqual(block, "")

    def test_format_block_with_recent_file(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f = wolf / "wrapup-pre-compact-fresh.md"
            f.write_text("# wrapup")
            cfg = {"hooks": {"wrapup_recovery": {"enabled": True}}}
            block = wr.format_recovery_block(td, cfg, mark=False)
            self.assertIn("Pre-compact wrap-up", block)
            self.assertIn(str(f), block)
            # Compact form — bound the size to prevent regression.
            self.assertLess(len(block), 200,
                            "recovery block must stay compact")

    def test_format_block_writes_seen_marker_then_skips(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f = wolf / "wrapup-pre-compact-once.md"
            f.write_text("x")
            cfg = {"hooks": {"wrapup_recovery": {"enabled": True}}}
            first = wr.format_recovery_block(td, cfg)
            self.assertNotEqual(first, "")
            # Sidecar should now exist.
            self.assertTrue((wolf / "wrapup-pre-compact-once.md.seen").exists())
            # Second call must be empty — already-seen.
            second = wr.format_recovery_block(td, cfg)
            self.assertEqual(second, "")

    def test_seen_marker_skipped_in_find(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            f = wolf / "wrapup-pre-compact-seen.md"; f.write_text("x")
            (wolf / "wrapup-pre-compact-seen.md.seen").write_text("")
            self.assertIsNone(wr.find_recent_wrapup(td))
            # But skip_seen=False still finds it.
            self.assertEqual(wr.find_recent_wrapup(td, skip_seen=False), f)

    def test_non_md_files_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            (wolf / "wrapup-pre-compact-x.txt").write_text("x")
            found = wr.find_recent_wrapup(td)
            self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
