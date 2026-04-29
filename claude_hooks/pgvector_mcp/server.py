"""Stdio JSON-RPC MCP server wrapping ``PgvectorProvider``.

Implements the minimum slice of the MCP protocol Claude Code expects:

- ``initialize`` request → returns ``protocolVersion``, ``capabilities``,
  ``serverInfo``.
- ``notifications/initialized`` → no-op.
- ``tools/list`` → returns the tool catalog.
- ``tools/call`` with ``{name, arguments}`` → dispatches to the matching
  handler and returns ``{content: [{type:"text", text}], isError}``.

Stateless: every request gets handled in isolation. The provider
instance is built once at startup from ``config/claude-hooks.json`` and
reused for the life of the process. Database and embedder connections
are managed by the provider's existing ``_ensure_ready`` lifecycle.

Errors never crash the loop. A handler exception becomes an MCP tool
error response (``isError: true``) so the client sees a useful message
instead of EOF.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any, Optional

from claude_hooks.config import load_config
from claude_hooks.dispatcher import build_providers
from claude_hooks.providers.base import Provider
from claude_hooks.providers.pgvector import PgvectorProvider

log = logging.getLogger("claude_hooks.pgvector_mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-hooks-pgvector"
SERVER_VERSION = "0.1.0"


def _tool_catalog() -> list[dict]:
    """Static schema description of all tools the server exposes.

    The shape mirrors what Claude Code's other MCP servers return — a
    list of ``{name, description, inputSchema}`` entries with JSON
    Schema for the arguments.
    """
    return [
        {
            "name": "pgvector-find",
            "description": (
                "Pure vector recall (cosine distance). Returns up to k closest "
                "memories. Use pgvector-find-hybrid for better factual queries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text query"},
                    "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        },
        {
            "name": "pgvector-find-hybrid",
            "description": (
                "Hybrid recall: RRF blend of vector cosine distance + BM25 "
                "keyword match against content_tsv. Best for factual / named "
                "queries that contain specific keywords."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                    "alpha": {
                        "type": "number",
                        "default": 0.5,
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Weight on vector signal (0=BM25 only, 1=vector only)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "pgvector-store",
            "description": (
                "Insert a single memory into the configured primary table. "
                "Idempotent on content_hash — re-storing identical content is "
                "a silent no-op."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "metadata": {"type": "object", "default": {}},
                },
                "required": ["content"],
            },
        },
        {
            "name": "pgvector-count",
            "description": "Count rows in the configured primary memories table.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "pgvector-kg-search",
            "description": (
                "Search KG entities by name (trigram) and observation content "
                "(hybrid). Returns nodes with their entity_type, metadata, and "
                "top observations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 30},
                },
                "required": ["query"],
            },
        },
        {
            "name": "pgvector-kg-create",
            "description": (
                "Bulk-create KG entities. Idempotent on entity name. Each "
                "entity: {name, entity_type, metadata?}. Returns rows actually inserted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "entity_type": {"type": "string"},
                                "metadata": {"type": "object", "default": {}},
                            },
                            "required": ["name", "entity_type"],
                        },
                    },
                },
                "required": ["entities"],
            },
        },
        {
            "name": "pgvector-kg-observe",
            "description": (
                "Add observations to existing entities. Each item: "
                "{entity_name, content}. Embeds and inserts into the configured "
                "kg_observations table. Idempotent on (entity_id, content_hash). "
                "Entity must already exist (call pgvector-kg-create first)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity_name": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["entity_name", "content"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
        {
            "name": "pgvector-kg-relate",
            "description": (
                "Create relations between entities. Each: {from, to, "
                "relation_type, metadata?}. Idempotent on (from, to, type)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                                "relation_type": {"type": "string"},
                                "metadata": {"type": "object", "default": {}},
                            },
                            "required": ["from", "to", "relation_type"],
                        },
                    },
                },
                "required": ["relations"],
            },
        },
    ]


class McpServer:
    """JSON-RPC dispatch surface around a single ``PgvectorProvider``.

    Public method ``handle(message)`` takes a parsed JSON-RPC dict and
    returns the response dict (or None for notifications). Kept
    transport-agnostic so the same logic can sit behind stdio, HTTP,
    or a test harness.
    """

    def __init__(self, provider: PgvectorProvider):
        self.provider = provider
        self._initialized = False

    def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            self._initialized = True
            return self._reply(rpc_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._reply(rpc_id, {"tools": _tool_catalog()})
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            try:
                payload = self._dispatch_tool(name, args)
                return self._reply(rpc_id, {
                    "content": [{"type": "text", "text": payload}],
                    "isError": False,
                })
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                log.warning("tool %s failed: %s\n%s", name, e, tb)
                return self._reply(rpc_id, {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True,
                })
        # Unknown method
        if rpc_id is not None:
            return self._reply(rpc_id, error={"code": -32601, "message": f"Method not found: {method}"})
        return None

    def _reply(self, rpc_id: Any, result: Optional[dict] = None,
               error: Optional[dict] = None) -> dict:
        out: dict = {"jsonrpc": "2.0", "id": rpc_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result or {}
        return out

    def _dispatch_tool(self, name: str, args: dict) -> str:
        if name == "pgvector-find":
            q = str(args.get("query") or "")
            k = int(args.get("k") or 5)
            return _format_memories(self.provider.recall(q, k=k))
        if name == "pgvector-find-hybrid":
            q = str(args.get("query") or "")
            k = int(args.get("k") or 5)
            alpha = float(args.get("alpha") if args.get("alpha") is not None else 0.5)
            return _format_memories(self.provider.recall_hybrid(q, k=k, alpha=alpha))
        if name == "pgvector-store":
            content = str(args.get("content") or "")
            metadata = args.get("metadata") or {}
            self.provider.store(content, metadata=metadata)
            return f"stored 1 memory ({len(content)} chars)"
        if name == "pgvector-count":
            return f"primary table count: {self.provider.count()}"
        if name == "pgvector-kg-search":
            q = str(args.get("query") or "")
            k = int(args.get("k") or 5)
            nodes = self.provider.kg_search_nodes(q, k=k)
            return _format_kg_nodes(nodes)
        if name == "pgvector-kg-create":
            entities = args.get("entities") or []
            n = self.provider.kg_create_entities(list(entities))
            return f"created {n} new entit{'y' if n == 1 else 'ies'} ({len(entities)} requested; collisions are no-ops)"
        if name == "pgvector-kg-observe":
            items = args.get("items") or []
            n = self.provider.kg_add_observations(list(items))
            return f"inserted {n} observation{'s' if n != 1 else ''} ({len(items)} requested)"
        if name == "pgvector-kg-relate":
            rels = args.get("relations") or []
            n = self.provider.kg_create_relations(list(rels))
            return f"created {n} new relation{'s' if n != 1 else ''} ({len(rels)} requested)"
        raise ValueError(f"unknown tool: {name}")


def _format_memories(mems: list) -> str:
    if not mems:
        return "(no results)"
    out = []
    for m in mems:
        meta = getattr(m, "metadata", None) or {}
        score = meta.get("_score")
        dist = meta.get("_distance")
        tbl = meta.get("_table") or "?"
        head = f"[{tbl}"
        if score is not None:
            head += f" score={score:.4f}"
        if dist is not None:
            head += f" dist={dist:.4f}"
        head += "]"
        out.append(f"{head} {getattr(m, 'text', '')}")
    return "\n\n---\n\n".join(out)


def _format_kg_nodes(nodes: list[dict]) -> str:
    if not nodes:
        return "(no results)"
    out = []
    for n in nodes:
        head = f"# {n['name']} ({n['entity_type']})  score={n.get('_score', 0):.3f} match={n.get('_match', '?')}"
        body = "\n".join(f"  - {o}" for o in n.get("observations", []))
        if not body:
            body = "  (no observations)"
        out.append(f"{head}\n{body}")
    return "\n\n".join(out)


def serve_stdio(provider: Optional[Provider] = None) -> int:
    """Run the JSON-RPC loop on stdin/stdout until EOF.

    Returns 0 on clean EOF, 1 on fatal init failure.
    """
    if provider is None:
        cfg = load_config()
        providers = build_providers(cfg)
        provider = next((p for p in providers if isinstance(p, PgvectorProvider)), None)
        if provider is None:
            sys.stderr.write("pgvector provider not configured / not enabled\n")
            return 1
    server = McpServer(provider)  # type: ignore[arg-type]
    sys.stderr.write(f"{SERVER_NAME} starting (protocol={PROTOCOL_VERSION})\n")
    sys.stderr.flush()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("malformed JSON: %s", e)
            continue
        resp = server.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0
