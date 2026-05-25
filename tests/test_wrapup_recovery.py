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
from tests._fixtures_net import (  # noqa: E402
    FIXTURE_REGEX_IP_PRIMARY,
    FIXTURE_REGEX_IP_SECONDARY,
)


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
        # The IP is regex test fodder, not infrastructure. Drawn from
        # the RFC 5737 documentation pool so the test stays portable.
        ip = FIXTURE_REGEX_IP_PRIMARY
        transcript = [_assistant_text(f"ssh root@{ip}:22 to reach a host.")]
        out = ws.collect_endpoints(transcript, [])
        self.assertTrue(any(extracted.startswith(ip)
                            for extracted in out["ips"]))

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
        ip = FIXTURE_REGEX_IP_SECONDARY
        transcript = [
            _assistant_text(f"Pod: https://xyz12345ab-7860.proxy.runpod.net "
                            f"and IP {ip}"),
        ]
        md = ws.synthesize_markdown(transcript, cwd="", session_id="s")
        self.assertIn("Connection state", md)
        self.assertIn("xyz12345ab-7860.proxy.runpod.net", md)
        self.assertIn(ip, md)

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


def _assistant_bash(cmd: str) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": cmd}}],
        }
    }


class ReconnectPreservationTests(unittest.TestCase):
    """Initial + last-known-good reconnect command preservation across a
    compact (vast.ai / pod use case)."""

    def test_collect_ssh_invocations_keeps_full_command(self):
        ip = FIXTURE_REGEX_IP_PRIMARY
        cmds = [
            f"ssh -p 41022 root@{ip} -L 8080:localhost:8080 -i ~/.ssh/id_rsa",
            "nvidia-smi",
        ]
        inv = ws.collect_ssh_invocations(cmds)
        self.assertEqual(len(inv), 1)
        # The whole command survives — port, tunnel, and key included.
        self.assertIn("-p 41022", inv[0])
        self.assertIn("-L 8080:localhost:8080", inv[0])
        self.assertIn("-i ~/.ssh/id_rsa", inv[0])

    def test_collect_ssh_invocations_excludes_keygen_and_help(self):
        cmds = ["ssh-keygen -t ed25519", "ssh --help", "ssh -V"]
        self.assertEqual(ws.collect_ssh_invocations(cmds), [])

    def test_build_reconnect_lines_initial_and_last(self):
        cmds = [
            "ssh -p 41022 root@ssh5.vast.ai",          # initial (proxy)
            "ls",
            f"ssh -p 41022 root@{FIXTURE_REGEX_IP_PRIMARY} -L 9000:localhost:9000",  # direct
        ]
        lines = ws.build_reconnect_lines(cmds)
        self.assertEqual(len(lines), 2)
        self.assertIn("ssh5.vast.ai", lines[0])
        self.assertIn(FIXTURE_REGEX_IP_PRIMARY, lines[1])

    def test_build_reconnect_lines_single(self):
        lines = ws.build_reconnect_lines(["ssh -p 22 root@ssh5.vast.ai"])
        self.assertEqual(len(lines), 1)

    def test_build_reconnect_lines_empty_when_no_ssh(self):
        self.assertEqual(ws.build_reconnect_lines(["ls", "git status"]), [])

    def test_vast_ai_pod_host_detected(self):
        out = ws.collect_endpoints(
            [_assistant_text("Pod is at ssh5.vast.ai now.")], [])
        self.assertIn("ssh5.vast.ai", out["pod_ids"])

    def test_synth_emits_sentinels_only_with_connection(self):
        t = [_assistant_bash("ssh -p 41022 root@ssh5.vast.ai")]
        md = ws.synthesize_markdown(t, cwd="", session_id="s")
        self.assertIn(ws.RECONNECT_SENTINEL_BEGIN, md)
        self.assertIn("-p 41022", md)
        # No connection → no sentinels at all (zero-token contract).
        md2 = ws.synthesize_markdown([_assistant_bash("ls")], cwd="", session_id="s")
        self.assertNotIn(ws.RECONNECT_SENTINEL_BEGIN, md2)

    def test_extract_reconnect_block_roundtrip(self):
        t = [
            _assistant_bash("ssh -p 41022 root@ssh5.vast.ai"),
            _assistant_bash(f"ssh -p 41022 root@{FIXTURE_REGEX_IP_PRIMARY}"),
        ]
        md = ws.synthesize_markdown(t, cwd="", session_id="s")
        inner = wr._extract_reconnect_block(md)
        self.assertIn("ssh -p 41022 root@ssh5.vast.ai", inner)
        self.assertIn(FIXTURE_REGEX_IP_PRIMARY, inner)
        # No sentinels → empty.
        self.assertEqual(wr._extract_reconnect_block("# just a heading"), "")

    def test_recovery_block_inlines_reconnect_commands(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            t = [_assistant_bash("ssh -p 41022 root@ssh5.vast.ai")]
            md = ws.synthesize_markdown(t, cwd=td, session_id="s")
            (wolf / "wrapup-pre-compact-conn.md").write_text(md, encoding="utf-8")
            cfg = {"hooks": {"wrapup_recovery": {"enabled": True}}}
            block = wr.format_recovery_block(td, cfg, mark=False)
            self.assertIn("Reconnect", block)
            self.assertIn("ssh -p 41022 root@ssh5.vast.ai", block)

    def test_recovery_block_stays_bare_pointer_without_connection(self):
        with tempfile.TemporaryDirectory() as td:
            wolf = Path(td) / ".wolf"
            wolf.mkdir()
            md = ws.synthesize_markdown([_assistant_bash("ls")], cwd=td, session_id="s")
            (wolf / "wrapup-pre-compact-noconn.md").write_text(md, encoding="utf-8")
            cfg = {"hooks": {"wrapup_recovery": {"enabled": True}}}
            block = wr.format_recovery_block(td, cfg, mark=False)
            self.assertNotIn("Reconnect", block)
            # Bare pointer stays compact — no token bloat when nothing to reconnect.
            self.assertLess(len(block), 200)


if __name__ == "__main__":
    unittest.main()
