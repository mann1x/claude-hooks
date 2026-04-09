"""Tests for MCP server auto-detection from a synthetic ~/.claude.json."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.detect import detect_all
from claude_hooks.providers import MemoryKgProvider, QdrantProvider


SAMPLE_CONFIG = {
    "mcpServers": {
        "qdrant": {"type": "http", "url": "http://192.168.178.2:32775/mcp"},
        "memory": {"type": "http", "url": "http://192.168.178.2:32776/mcp"},
        "github-mcp": {"type": "http", "url": "http://192.168.178.2:32773/mcp"},
        "context7": {
            "type": "http",
            "url": "https://mcp.context7.com/mcp",
            "headers": {"X-API-Key": "secret"},
        },
        "stdio-tool": {"type": "stdio", "command": "some-bin"},
    },
    "projects": {
        "/srv/proj-a": {
            "mcpServers": {
                "qdrant-secondary": {"type": "http", "url": "http://other:32775/mcp"},
            }
        },
        "/srv/proj-b": {"mcpServers": {}},
    },
}


class TestDetect(unittest.TestCase):
    def test_qdrant_name_match(self):
        report = detect_all(SAMPLE_CONFIG)
        cands = report.candidates_for(QdrantProvider.name)
        # Both 'qdrant' and 'qdrant-secondary' should match
        urls = {c.url for c in cands}
        self.assertIn("http://192.168.178.2:32775/mcp", urls)
        self.assertIn("http://other:32775/mcp", urls)

    def test_memory_name_match(self):
        report = detect_all(SAMPLE_CONFIG)
        cands = report.candidates_for(MemoryKgProvider.name)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].url, "http://192.168.178.2:32776/mcp")

    def test_stdio_servers_excluded(self):
        report = detect_all(SAMPLE_CONFIG)
        urls = {s[1].get("url") for s in report.all_http_servers}
        # stdio-tool has no url and should not be in the http set
        self.assertTrue(all(u is not None for u in urls))

    def test_unmatched_servers(self):
        report = detect_all(SAMPLE_CONFIG)
        unmatched = report.unmatched_servers()
        keys = {k for (k, _, _) in unmatched}
        # github-mcp and context7 should be unmatched (no name keyword)
        self.assertIn("github-mcp", keys)
        self.assertIn("context7", keys)
        self.assertNotIn("qdrant", keys)

    def test_headers_preserved(self):
        # context7 doesn't match a provider, but if it did we'd want headers preserved.
        # Test by directly calling the provider's detect with a custom config.
        custom = {"mcpServers": {"qdrant-private": {
            "type": "http",
            "url": "https://x.example/mcp",
            "headers": {"Authorization": "Bearer t"},
        }}}
        cands = QdrantProvider.detect(custom)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].headers, {"Authorization": "Bearer t"})

    def test_empty_config_returns_empty(self):
        report = detect_all({})
        self.assertEqual(report.candidates_for(QdrantProvider.name), [])
        self.assertEqual(report.candidates_for(MemoryKgProvider.name), [])


if __name__ == "__main__":
    unittest.main()
