"""
Qdrant memory provider.

Talks to ``mcp-server-qdrant`` (https://github.com/qdrant/mcp-server-qdrant)
over Streamable HTTP. The two relevant tools are:

- ``qdrant-find(query, collection_name)``  → returns a JSON-encoded list of
  ``"<entry><content>...</content><metadata>{...}</metadata></entry>"`` strings
- ``qdrant-store(information, collection_name, metadata?)``

The collection name is *not* server-side configurable in mcp-server-qdrant
1.27 — every call must pass it. We get it from
``providers.qdrant.collection`` in ``claude-hooks.json``.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from claude_hooks.mcp_client import McpError, extract_text_content
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
    is_http_server,
    iter_mcp_servers,
)

ENTRY_RE = re.compile(
    r"<entry>\s*<content>(?P<content>.*?)</content>\s*"
    r"(?:<metadata>(?P<metadata>.*?)</metadata>)?\s*</entry>",
    re.DOTALL,
)

NAME_KEYWORDS = ("qdrant",)


class QdrantProvider(Provider):
    name = "qdrant"
    display_name = "Qdrant"

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #
    @classmethod
    def signature_tools(cls) -> set[str]:
        return {"qdrant-find", "qdrant-store"}

    @classmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        seen_urls: set[str] = set()
        candidates: list[ServerCandidate] = []
        for key, cfg, source in iter_mcp_servers(claude_config):
            if not is_http_server(cfg):
                continue
            url = cfg["url"]
            if url in seen_urls:
                continue
            if any(kw in key.lower() for kw in NAME_KEYWORDS):
                seen_urls.add(url)
                candidates.append(
                    ServerCandidate(
                        server_key=key,
                        url=url,
                        headers=cfg.get("headers") or {},
                        source=source,
                        confidence="name",
                        notes=f"key '{key}' contains qdrant",
                    )
                )
        return candidates

    # ------------------------------------------------------------------ #
    # Recall
    # ------------------------------------------------------------------ #
    def recall(self, query: str, k: int = 5) -> list[Memory]:
        if not query.strip():
            return []
        collection = self.options.get("collection") or "memory"
        timeout = float(self.options.get("timeout") or 5.0)
        client = self._client(timeout=timeout)

        try:
            result = client.call_tool(
                "qdrant-find",
                {"query": query, "collection_name": collection},
            )
        except McpError:
            return []

        text = extract_text_content(result)
        if not text:
            return []

        # qdrant-find returns content[0].text as a JSON-encoded list of strings.
        # First entry is "Results for the query 'X'" — drop it.
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []

        memories: list[Memory] = []
        for raw in items[1:]:
            if not isinstance(raw, str):
                continue
            mem = _parse_qdrant_entry(raw)
            if mem is not None:
                memories.append(mem)
            if len(memories) >= k:
                break
        return memories

    # ------------------------------------------------------------------ #
    # Store
    # ------------------------------------------------------------------ #
    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content.strip():
            return
        collection = self.options.get("collection") or "memory"
        timeout = float(self.options.get("timeout") or 5.0)
        client = self._client(timeout=timeout)
        args = {"information": content, "collection_name": collection}
        if metadata:
            args["metadata"] = metadata
        try:
            client.call_tool("qdrant-store", args)
        except McpError:
            # Storage failure should not break the hook — caller logs it.
            raise


def _parse_qdrant_entry(raw: str) -> Optional[Memory]:
    """
    Parse one ``<entry><content>...</content><metadata>{...}</metadata></entry>``
    string from qdrant-find. Returns None if the entry doesn't match the
    expected shape.
    """
    m = ENTRY_RE.search(raw)
    if not m:
        # Some entries may not be wrapped — return as-is.
        return Memory(text=raw.strip())
    content = m.group("content").strip()
    metadata: dict = {}
    raw_meta = m.group("metadata")
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
        except json.JSONDecodeError:
            metadata = {"_raw": raw_meta}
    return Memory(text=content, metadata=metadata)
