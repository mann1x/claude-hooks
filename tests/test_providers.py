"""Tests for the qdrant and memory_kg provider parsing logic.

These tests don't talk to a real MCP server — they monkey-patch the
provider's MCP client and feed it canned responses captured from the live
servers (so the parsing matches reality).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.providers import MemoryKgProvider, QdrantProvider, ServerCandidate


# Real response from mcp-server-qdrant 1.27.0 (truncated for the test).
QDRANT_FIND_RAW = {
    "content": [
        {
            "type": "text",
            "text": json.dumps(
                [
                    "Results for the query 'bcache'",
                    '<entry><content>bcache fix: clamp insert sectors to <16-bit max</content>'
                    '<metadata>{"type": "bugfix", "subsystem": "bcache"}</metadata></entry>',
                    '<entry><content>Another entry without metadata</content></entry>',
                ]
            ),
        }
    ],
    "isError": False,
}

MEMORY_SEARCH_RAW = {
    "content": [{"type": "text", "text": '{"entities": [], "relations": []}'}],
    "structuredContent": {
        "entities": [
            {
                "name": "solidPC",
                "entityType": "server",
                "observations": ["RTX 3090", "AMD 5600G", "runs *arr stack"],
            }
        ],
        "relations": [
            {"from": "solidPC", "to": "Plex", "relationType": "hosts"},
        ],
    },
    "isError": False,
}


class TestQdrantProvider(unittest.TestCase):
    def _make(self):
        cand = ServerCandidate(server_key="qdrant", url="http://x/mcp")
        prov = QdrantProvider(cand, options={"collection": "memory"})
        return prov

    def test_recall_parses_entries(self):
        prov = self._make()
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {**QDRANT_FIND_RAW, "isError": False}
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        mems = prov.recall("bcache", k=5)
        self.assertEqual(len(mems), 2)
        self.assertIn("clamp insert sectors", mems[0].text)
        self.assertEqual(mems[0].metadata.get("type"), "bugfix")
        self.assertEqual(mems[1].metadata, {})

    def test_recall_empty_query_returns_empty(self):
        prov = self._make()
        prov._client = lambda timeout=5.0: MagicMock()  # type: ignore
        self.assertEqual(prov.recall("", k=5), [])
        self.assertEqual(prov.recall("   ", k=5), [])

    def test_recall_handles_mcp_error(self):
        from claude_hooks.mcp_client import McpError
        prov = self._make()
        mock_client = MagicMock()
        mock_client.call_tool.side_effect = McpError("boom")
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        self.assertEqual(prov.recall("anything", k=5), [])

    def test_store_calls_qdrant_store(self):
        prov = self._make()
        mock_client = MagicMock()
        # Simulate a server that accepts collection_name (returns isError=False).
        mock_client.call_tool.return_value = {"isError": False}
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        prov.store("hello world", metadata={"type": "test"})
        mock_client.call_tool.assert_called_once()
        args = mock_client.call_tool.call_args[0]
        self.assertEqual(args[0], "qdrant-store")
        self.assertEqual(args[1]["information"], "hello world")
        self.assertEqual(args[1]["collection_name"], "memory")
        self.assertEqual(args[1]["metadata"], {"type": "test"})

    def test_store_without_collection(self):
        """Servers that reject collection_name get a retry without it."""
        cand = ServerCandidate(server_key="qdrant", url="http://x/mcp")
        prov = QdrantProvider(cand, options={"collection": "memory"})
        mock_client = MagicMock()
        # First call with collection_name returns isError=True, second without succeeds.
        mock_client.call_tool.side_effect = [
            {"isError": True},
            {"isError": False},
        ]
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        prov.store("hello world")
        self.assertEqual(mock_client.call_tool.call_count, 2)
        # Second call should not have collection_name.
        second_args = mock_client.call_tool.call_args_list[1][0]
        self.assertEqual(second_args[0], "qdrant-store")
        self.assertNotIn("collection_name", second_args[1])

    def test_signature_tools(self):
        self.assertEqual(
            QdrantProvider.signature_tools(),
            {"qdrant-find", "qdrant-store"},
        )


class TestMemoryKgProvider(unittest.TestCase):
    def _make(self):
        cand = ServerCandidate(server_key="memory", url="http://x/mcp")
        return MemoryKgProvider(cand, options={})

    def test_recall_parses_structured_content(self):
        prov = self._make()
        mock_client = MagicMock()
        mock_client.call_tool.return_value = MEMORY_SEARCH_RAW
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        mems = prov.recall("solidPC", k=5)
        self.assertEqual(len(mems), 1)
        self.assertIn("solidPC", mems[0].text)
        self.assertIn("RTX 3090", mems[0].text)
        self.assertIn("hosts", mems[0].text)
        self.assertEqual(mems[0].metadata["entity_type"], "server")

    def test_recall_falls_back_to_text_content(self):
        prov = self._make()
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {
            "content": [{"type": "text", "text": '{"entities":[{"name":"x","entityType":"t","observations":["o"]}],"relations":[]}'}],
            "isError": False,
        }
        prov._client = lambda timeout=5.0: mock_client  # type: ignore
        mems = prov.recall("x", k=5)
        self.assertEqual(len(mems), 1)
        self.assertIn("x", mems[0].text)

    def test_signature_tools(self):
        self.assertEqual(
            MemoryKgProvider.signature_tools(),
            {"search_nodes", "create_entities", "add_observations"},
        )


if __name__ == "__main__":
    unittest.main()
