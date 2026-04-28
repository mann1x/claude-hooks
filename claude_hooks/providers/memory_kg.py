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

Storage strategy: when the caller supplies metadata classifying the turn
(``observation_type`` from the stop hook), promote the entity to a typed,
topic-named KG node — e.g. ``bug-fix-proxy-drain-2026-04-27`` instead of
the generic ``session-<timestamp>``. Same-topic turns on the same day
collide on name, fall back to ``add_observations``, and so accumulate
into a real growing entity instead of a heap of isolated session blobs.

Without classification metadata (ad-hoc stores), we still write
``session-<timestamp>`` so the legacy contract holds.
"""

from __future__ import annotations

import json
import re
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

# observation_type (from stop.py:_classify_observation) → KG entity type.
# These are the kinds we promote; anything else falls through to session.
_OBSERVATION_TYPE_TO_KG_KIND = {
    "fix": "bug-fix",
    "decision": "decision",
    "preference": "preference",
    "gotcha": "gotcha",
    # Some hooks pass through richer types from the XML classifier:
    "refactor": "refactor",
    "feature": "feature",
    "investigation": "investigation",
    "docs": "docs",
    "build": "build",
    "test": "test",
}

_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.IGNORECASE)
_RESULT_HEADING_RE = re.compile(
    r"^\s*##\s+Result\s*\n+(.+?)(?:\n##|\Z)", re.MULTILINE | re.DOTALL,
)
# Strong-signal headings — when the user / model has explicitly tagged
# the turn with a Bug/Fix/Root-cause subject line, that text is a
# better topic identifier than the result body.
_BUG_HEADING_RE = re.compile(
    # Heading body may sit on the same line after a colon
    # ("## Bug: <topic>") or on subsequent lines below the heading
    # ("## Bug\n\n<topic body>"). Single regex covers both.
    r"^\s*##\s+(?:Bug|Fix|Root\s*[Cc]ause|Issue)\s*[:\-]?\s*(.+?)(?:\n##|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
# Inline narrative markers — `Root cause: foo bar baz`.
_INLINE_FIX_RE = re.compile(
    r"(?:^|\n)\s*(?:Root\s*[Cc]ause|The\s+bug\s+(?:is|was)|"
    r"The\s+issue\s+(?:is|was)|The\s+fix\s+(?:is|was)|Fixed\s+by)\s*"
    r"[:\-]\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)
# Files touched section — the first listed file's basename is a
# strong topic identifier when no narrative heading is present.
_FILES_TOUCHED_RE = re.compile(
    r"^\s*##\s+Files\s+(?:touched|changed|modified)\s*\n+(.+?)(?:\n##|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_FIRST_FILE_LINE_RE = re.compile(r"^\s*[-*]\s+(\S+)", re.MULTILINE)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "from", "by", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "it", "its", "as",
})


def _slugify(text: str, max_len: int = 40) -> str:
    """Lowercase, strip non-alnum, drop stopwords, join with dashes,
    truncate. Returns empty string on empty input — caller decides
    fallback."""
    if not text:
        return ""
    lower = text.lower()
    # Replace non-alnum with spaces, then split.
    cleaned = _SLUG_STRIP_RE.sub(" ", lower)
    tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS]
    if not tokens:
        return ""
    slug = "-".join(tokens)
    if len(slug) <= max_len:
        return slug
    # Truncate at a token boundary so we don't slice mid-word.
    out: list[str] = []
    n = 0
    for tok in tokens:
        if n + len(tok) + (1 if out else 0) > max_len:
            break
        out.append(tok)
        n += len(tok) + (1 if len(out) > 1 else 0)
    return "-".join(out) if out else slug[:max_len].rstrip("-")


def _file_basename_slug(path: str) -> str:
    """Strip directories + extension, return slugified basename.

    Returns "" for paths we can't sensibly slug (empty, single dot, …)."""
    if not path:
        return ""
    p = path.strip().rstrip("/").rstrip("\\")
    p = p.split("/")[-1].split("\\")[-1]
    if not p or p == ".":
        return ""
    # Drop file extension(s): foo.test.py -> foo-test
    if "." in p:
        p = p.rsplit(".", 1)[0]
    return _slugify(p)


def _derive_topic_slug(content: str) -> str:
    """Extract a short topic slug from the summary content.

    Priority (strongest signal first):

    1. XML ``<title>`` tag — explicit user/model labelling.
    2. ``## Bug:`` / ``## Fix:`` / ``## Root cause:`` heading body —
       narrative tag means the topic is the bug being fixed.
    3. Inline ``Root cause: …`` / ``The bug is …`` / ``Fixed by: …``
       marker — same narrative signal in prose form.
    4. ``## Files touched`` first file's basename — when filenames
       are listed, the topic is almost always *that file*'s behaviour,
       not whatever leading words happen to start the result text.
    5. ``## Result`` first line (legacy fallback).
    6. First non-empty line of content (last-resort fallback).

    Returns empty string when nothing usable is found.
    """
    if not content:
        return ""

    # 1. Explicit XML title tag.
    m = _TITLE_RE.search(content)
    if m:
        slug = _slugify(m.group(1).strip())
        if slug:
            return slug

    # 2. ## Bug: / ## Fix: / ## Root cause: heading body.
    m = _BUG_HEADING_RE.search(content)
    if m:
        first_line = next(
            (ln for ln in m.group(1).splitlines() if ln.strip()), "",
        )
        slug = _slugify(first_line)
        if slug:
            return slug

    # 3. Inline narrative marker — `Root cause: foo bar baz`.
    m = _INLINE_FIX_RE.search(content)
    if m:
        slug = _slugify(m.group(1).strip())
        if slug:
            return slug

    # 4. First file under ## Files touched.
    m = _FILES_TOUCHED_RE.search(content)
    if m:
        f = _FIRST_FILE_LINE_RE.search(m.group(1))
        if f:
            slug = _file_basename_slug(f.group(1))
            if slug:
                return slug

    # 5. ## Result first non-empty line (legacy).
    m = _RESULT_HEADING_RE.search(content)
    if m:
        first_line = next(
            (ln for ln in m.group(1).splitlines() if ln.strip()), "",
        )
        slug = _slugify(first_line)
        if slug:
            return slug

    # 6. First non-empty line (last-resort fallback).
    for line in content.splitlines():
        s = line.strip()
        # Skip the markdown heading itself and any cwd: lines.
        if not s or s.startswith("#") or s.startswith("cwd:"):
            continue
        slug = _slugify(s)
        if slug:
            return slug
    return ""


def _classify_kg_entity(
    content: str, metadata: dict,
) -> tuple[str, str]:
    """Pick ``(entity_name, entity_type)`` for a stored summary.

    Returns the legacy ``("session-<ts>", "session")`` pair when the
    metadata doesn't classify the turn or no topic can be derived from
    the content. That keeps ad-hoc stores (no metadata) on the original
    schema and only promotes when we have real signal.
    """
    obs_type = (metadata or {}).get("observation_type") or ""
    kind = _OBSERVATION_TYPE_TO_KG_KIND.get(obs_type.lower())
    slug = _derive_topic_slug(content) if kind else ""
    if not kind or not slug:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"session-{ts}", "session"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{kind}-{slug}-{date}", kind


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

        md = metadata or {}
        explicit_name = md.get("entity_name")
        if explicit_name:
            entity_name = explicit_name
            entity_type = md.get("entity_type") or "session"
        else:
            entity_name, entity_type = _classify_kg_entity(content, md)

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
