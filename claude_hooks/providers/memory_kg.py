"""
Memory knowledge-graph provider.

Talks to ``@modelcontextprotocol/server-memory`` over Streamable HTTP. The
graph is a set of entities (each with name, type, and a list of string
observations) and relations between them.

We use:

- ``search_nodes(query)``      → returns ``{entities: [...], relations: [...]}``
- ``read_graph()``             → full graph (used for empty-store probe)
- ``create_entities(entities)``→ create new entities
- ``add_observations(observations)`` → append observations to existing entity

For the ``store()`` path we treat each call as creating a "session" entity
named ``session-<timestamp>`` with the content as a single observation.
This keeps the schema simple — the model can later promote interesting
sessions into proper named entities by talking to the MCP server directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from claude_hooks.mcp_client import McpError, extract_text_content
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
    is_http_server,
    iter_mcp_servers,
)

NAME_KEYWORDS = ("memory", "memorykg", "mem-kg", "memorygraph", "mem_kg", "kg")


class MemoryKgProvider(Provider):
    name = "memory_kg"
    display_name = "Memory KG"

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #
    @classmethod
    def signature_tools(cls) -> set[str]:
        return {"search_nodes", "create_entities", "add_observations"}

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
                        notes=f"key '{key}' looks like a memory/KG server",
                    )
                )
        return candidates

    # ------------------------------------------------------------------ #
    # Recall
    # ------------------------------------------------------------------ #
    def recall(self, query: str, k: int = 5) -> list[Memory]:
        if not query.strip():
            return []
        timeout = float(self.options.get("timeout") or 5.0)
        client = self._client(timeout=timeout)

        try:
            result = client.call_tool("search_nodes", {"query": query})
        except McpError:
            return []

        # Prefer the structured result if present (newer servers).
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        entities: list[dict] = []
        relations: list[dict] = []
        if isinstance(structured, dict):
            entities = structured.get("entities") or []
            relations = structured.get("relations") or []
        else:
            text = extract_text_content(result)
            if text:
                try:
                    parsed = json.loads(text)
                    entities = parsed.get("entities") or []
                    relations = parsed.get("relations") or []
                except (ValueError, AttributeError):
                    pass

        memories: list[Memory] = []
        for ent in entities[:k]:
            if not isinstance(ent, dict):
                continue
            name = ent.get("name", "<unnamed>")
            ent_type = ent.get("entityType", "")
            obs = ent.get("observations") or []
            obs_text = "\n  - ".join(str(o) for o in obs[:5])
            text = f"**{name}** ({ent_type})"
            if obs_text:
                text += f"\n  - {obs_text}"
            # Attach related-relation summary if available.
            rels_for_ent = [
                r for r in relations
                if isinstance(r, dict) and (r.get("from") == name or r.get("to") == name)
            ]
            if rels_for_ent:
                rel_lines = [
                    f"  · {r.get('from','?')} →{r.get('relationType','?')}→ {r.get('to','?')}"
                    for r in rels_for_ent[:3]
                ]
                text += "\n" + "\n".join(rel_lines)
            memories.append(
                Memory(
                    text=text,
                    metadata={"entity_type": ent_type, "name": name},
                )
            )
        return memories

    # ------------------------------------------------------------------ #
    # Store
    # ------------------------------------------------------------------ #
    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content.strip():
            return
        timeout = float(self.options.get("timeout") or 5.0)
        client = self._client(timeout=timeout)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        entity_name = (metadata or {}).get("entity_name") or f"session-{ts}"
        entity_type = (metadata or {}).get("entity_type") or "session"

        # Create the entity (idempotent on the server side — if it exists
        # the server will tell us, in which case we add observations instead).
        try:
            client.call_tool(
                "create_entities",
                {
                    "entities": [
                        {
                            "name": entity_name,
                            "entityType": entity_type,
                            "observations": [content],
                        }
                    ]
                },
            )
            return
        except McpError as e:
            # Likely "entity already exists" — fall through to add_observations.
            if "exist" not in str(e).lower():
                raise

        client.call_tool(
            "add_observations",
            {
                "observations": [
                    {"entityName": entity_name, "contents": [content]}
                ]
            },
        )
