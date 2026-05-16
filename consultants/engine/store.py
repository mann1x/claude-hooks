"""LangGraph BaseStore adapter over the claude-hooks providers.

M8: gives the council a shared, namespaced read/write surface for
findings — so a researcher lane in round 2 can recall what other
lanes (or earlier rounds) already discovered, and a follow-up
consultation can semantically replay the parent's research without
a chronological cold pre-seed.

Namespace convention
====================

Always use the :class:`Namespaces` factories — a typo in a hand-
rolled tuple silently splits the store and breaks cross-lane recall.

- ``(sid, "research")`` — within-session per-lane findings. Written
  by the researcher between iterations, read by sibling/later lanes.
- ``(sid, "tool_results")`` — within-session tool-call outputs, when
  the tool_executor role is on (M6). Off by default.
- ``("project", project_id)`` — cross-session per-project memory.
- ``("user", user_id)`` — cross-project user-global memory.

Backend
=======

The factory :func:`make_consultants_store` picks a backend from
``cfg.store.backend``:

- ``"memory"`` (default) — LangGraph's bundled :class:`InMemoryStore`.
  Zero-dependency, process-local, fine for tests and for users who
  don't want long-term recall.
- ``"pgvector"`` — wraps :class:`PgvectorProvider` (the same
  provider the recall pipeline uses for ``mcp__pgvector__*`` tools).
  Vector recall + KG share the same Postgres instance.
- ``"sqlite_vec"`` — wraps :class:`SqliteVecProvider`. Same shape,
  file-backed.

Effort gate
===========

The store is **off** at ``low`` / ``medium`` (zero-cost path) and
**on** at ``high`` / ``max`` / ``x*`` per the M8 spec. The factory
honours ``cfg.store.enable_at_efforts`` so the gate is data-driven,
not hard-coded; the default value matches the spec.

Failure mode
============

Every helper here is defensive: when the store is ``None`` (the
disabled case) or the underlying provider raises, the helper logs
and returns an empty result. Recall is advisory — a broken store
must never break the council.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Protocol


# Optional langgraph import — the BaseStore subclass only exists
# when langgraph is installed. The convenience helpers
# (recall_research / record_research / make_consultants_store) are
# safe to import either way; they degrade to no-ops when langgraph
# isn't available or when the caller passes ``store=None``.
try:
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        PutOp,
        SearchItem,
        SearchOp,
    )
    HAVE_LANGGRAPH_STORE = True
except ImportError:  # pragma: no cover — main env without consultants extras
    HAVE_LANGGRAPH_STORE = False
    BaseStore = object  # type: ignore[misc,assignment]


log = logging.getLogger("consultants.engine.store")


# Marker key on the provider's metadata. Lets a single provider
# (e.g. the user's pgvector instance) coexist with general
# claude-hooks recall memories without contamination: our search
# filter ignores rows missing this flag.
_STORE_MARKER = "_consultants_store"


# ============================================================== #
# Namespaces
# ============================================================== #

class Namespaces:
    """Canonical namespace tuples.

    Always go through these factories. Hand-rolled tuples are an
    easy way to silently split the store on a typo.
    """

    @staticmethod
    def research(sid: str) -> tuple[str, ...]:
        """Per-session research findings — within-session lane recall."""
        return (str(sid), "research")

    @staticmethod
    def tool_results(sid: str) -> tuple[str, ...]:
        """Per-session tool-call outputs (M6 tool_executor lane)."""
        return (str(sid), "tool_results")

    @staticmethod
    def project(project_id: str) -> tuple[str, ...]:
        """Cross-session per-project memory (follow-up + recall)."""
        return ("project", str(project_id))

    @staticmethod
    def user(user_id: str) -> tuple[str, ...]:
        """Cross-project user-global memory."""
        return ("user", str(user_id))


# ============================================================== #
# Provider protocol — duck-typed contract.
# ============================================================== #

class StoreProvider(Protocol):
    """The minimal surface a provider must expose to back the store.

    Both :class:`PgvectorProvider` and :class:`SqliteVecProvider`
    already implement this — no provider changes were needed for M8.
    """

    def store(self, content: str, metadata: dict) -> None: ...

    def recall_hybrid(self, query: str, k: int = 5) -> list[Any]: ...


# ============================================================== #
# ProviderBackedStore — concrete BaseStore over a provider.
# ============================================================== #

if HAVE_LANGGRAPH_STORE:

    class ProviderBackedStore(BaseStore):
        """LangGraph :class:`BaseStore` backed by a claude-hooks provider.

        - ``put`` writes to both an in-process ``(namespace, key) ->
          Item`` index AND the provider (for cross-session vector
          recall).
        - ``get`` / ``delete`` / ``list_namespaces`` hit the
          in-process index. They're O(1) and survive only the
          lifetime of this store instance — durability is the
          provider's job, recall is the use case.
        - ``search`` with a query string goes to
          ``provider.recall_hybrid`` and post-filters by namespace
          prefix + marker. ``search`` without a query (rare; mainly
          used by ``list_namespaces`` callers) falls back to the
          in-process index, ranked by ``updated_at`` descending.

        The class is sync-internal; LangGraph's async path
        (``abatch``) defers to ``batch``. The consultants runner
        already wraps node work via ``asyncio.to_thread`` so calling
        sync here is fine.
        """

        def __init__(
            self,
            provider: StoreProvider,
            *,
            marker: str = _STORE_MARKER,
        ):
            self._provider = provider
            self._marker = marker
            # ns -> key -> Item; only as durable as this object
            self._index: dict[tuple[str, ...], dict[str, Item]] = (
                defaultdict(dict)
            )

        # ---- BaseStore abstract surface ---- #

        def batch(self, ops: Iterable[Any]) -> list[Any]:
            results: list[Any] = []
            for op in ops:
                if isinstance(op, GetOp):
                    results.append(self._do_get(op))
                elif isinstance(op, PutOp):
                    self._do_put(op)
                    results.append(None)
                elif isinstance(op, SearchOp):
                    results.append(self._do_search(op))
                elif isinstance(op, ListNamespacesOp):
                    results.append(self._do_list_namespaces(op))
                else:  # pragma: no cover — defensive
                    results.append(None)
            return results

        async def abatch(self, ops: Iterable[Any]) -> list[Any]:
            return self.batch(ops)

        # ---- op handlers ---- #

        def _do_get(self, op):
            ns = tuple(op.namespace)
            return self._index.get(ns, {}).get(op.key)

        def _do_put(self, op):
            ns = tuple(op.namespace)
            if op.value is None:
                # PutOp(value=None) deletes per BaseStore contract.
                self._index.get(ns, {}).pop(op.key, None)
                return
            now = datetime.now(timezone.utc)
            existing = self._index.get(ns, {}).get(op.key)
            created_at = existing.created_at if existing else now
            item = Item(
                value=dict(op.value),
                key=op.key,
                namespace=ns,
                created_at=created_at,
                updated_at=now,
            )
            self._index[ns][op.key] = item

            # Persist a vector-indexable copy to the provider too.
            # ``index=False`` skips the provider write (LangGraph
            # convention for "exact-key only, don't search me").
            if op.index is False:
                return
            text = self._extract_indexable_text(op.value, op.index)
            if not text:
                return
            meta = {
                self._marker: True,
                "namespace": list(ns),
                "key": op.key,
                "value": op.value,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            try:
                self._provider.store(text, metadata=meta)
            except Exception:  # pragma: no cover — provider-side
                log.exception(
                    "ProviderBackedStore: provider.store raised; "
                    "in-process index updated but vector recall will "
                    "miss this item",
                )

        def _do_search(self, op):
            ns_prefix = tuple(op.namespace_prefix)

            if not op.query:
                # No query — fall back to in-process scan ranked by
                # updated_at descending. Useful for "list everything
                # under this namespace" patterns without polluting
                # the vector index.
                items: list[SearchItem] = []
                for ns, by_key in self._index.items():
                    if ns[: len(ns_prefix)] != ns_prefix:
                        continue
                    for it in by_key.values():
                        items.append(SearchItem(
                            namespace=it.namespace,
                            key=it.key,
                            value=it.value,
                            created_at=it.created_at,
                            updated_at=it.updated_at,
                            score=None,
                        ))
                items.sort(key=lambda i: i.updated_at, reverse=True)
                return items[op.offset: op.offset + op.limit]

            # Vector recall path. Over-fetch by 4× (capped at 25 min)
            # because the provider has no namespace awareness and we
            # filter post-hoc; the marker filter is defensive against
            # mixed-purpose providers.
            try:
                overfetch = max(op.limit * 4, 25)
                hits = self._provider.recall_hybrid(op.query, k=overfetch)
            except Exception:  # pragma: no cover — provider-side
                log.exception(
                    "ProviderBackedStore: recall_hybrid raised; "
                    "returning empty result",
                )
                return []

            seen: set[tuple] = set()
            out: list[SearchItem] = []
            for h in hits:
                meta = getattr(h, "metadata", None) or {}
                if not meta.get(self._marker):
                    continue
                ns = tuple(meta.get("namespace") or ())
                key = meta.get("key") or ""
                if not ns or not key:
                    continue
                if ns[: len(ns_prefix)] != ns_prefix:
                    continue
                if (ns, key) in seen:
                    continue
                seen.add((ns, key))
                value = meta.get("value")
                if not isinstance(value, dict):
                    # Older entries or non-dict payloads — surface
                    # the raw text so the caller still gets content.
                    value = {"text": getattr(h, "text", "")}
                created = _parse_dt(meta.get("created_at"))
                updated = _parse_dt(meta.get("updated_at"))
                score = _safe_float(
                    getattr(h, "score", None)
                    or meta.get("_score"),
                )
                out.append(SearchItem(
                    namespace=ns,
                    key=key,
                    value=value,
                    created_at=created,
                    updated_at=updated,
                    score=score,
                ))
                if len(out) >= op.limit + op.offset:
                    break
            return out[op.offset: op.offset + op.limit]

        def _do_list_namespaces(self, op):
            namespaces = sorted(self._index.keys())
            if op.max_depth is not None:
                namespaces = [ns[: op.max_depth] for ns in namespaces]
                seen: set = set()
                dedup: list = []
                for ns in namespaces:
                    if ns not in seen:
                        seen.add(ns)
                        dedup.append(ns)
                namespaces = dedup
            return namespaces[op.offset: op.offset + op.limit]

        # ---- helpers ---- #

        @staticmethod
        def _extract_indexable_text(value, index_fields):
            """Pull the text the provider should embed.

            ``index_fields`` follows LangGraph's contract:
            - ``None`` / ``[]`` -> index whole value (``$``)
            - ``False`` -> handled by caller (skip writing)
            - list of dotted paths -> concatenate matched strings
            """
            if not isinstance(value, dict):
                return json.dumps(value, default=str)
            fields = index_fields
            if not fields:  # None or empty
                fields = ["$"]
            parts: list[str] = []
            for path in fields:
                if path == "$":
                    parts.append(json.dumps(value, default=str))
                    continue
                cur: Any = value
                for segment in str(path).split("."):
                    if isinstance(cur, dict):
                        cur = cur.get(segment)
                    else:
                        cur = None
                        break
                if cur is None:
                    continue
                if isinstance(cur, str):
                    parts.append(cur)
                else:
                    parts.append(json.dumps(cur, default=str))
            return "\n\n".join(p for p in parts if p)

else:  # pragma: no cover — langgraph missing
    class ProviderBackedStore:  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            raise RuntimeError(
                "ProviderBackedStore requires langgraph (langgraph-checkpoint) "
                "to be installed in the consultants env",
            )


# ============================================================== #
# Factory
# ============================================================== #

# Default effort gate — store is on for the "deep thinking" tiers
# only. low/medium stay zero-cost; the x-tier diversity loop
# benefits the most from cross-lane recall.
_DEFAULT_ENABLE_AT_EFFORTS = (
    "high", "max",
    "xmedium", "xhigh", "xmax", "xauto",
)


def make_consultants_store(
    cfg: Any,
    *,
    sid: str,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    effort: Optional[str] = None,
    provider_loader: Optional[Callable[[Any], Any]] = None,
) -> Optional[Any]:
    """Build the consultants BaseStore from config, or return None.

    A ``None`` return is the explicit "no shared store" signal that
    callers must tolerate — recall and record helpers are no-ops in
    that case.

    Returns ``None`` when:

    - LangGraph isn't installed (main env without consultants extras).
    - ``cfg.store`` is missing.
    - ``cfg.store.enabled`` is False.
    - ``effort`` is set and not in ``cfg.store.enable_at_efforts``.
    - The configured backend can't be loaded.

    ``provider_loader`` is a test seam: tests pass a factory that
    returns a stub provider so they don't need a real Postgres /
    sqlite-vec install on disk.
    """
    if not HAVE_LANGGRAPH_STORE:
        return None
    store_cfg = getattr(cfg, "store", None)
    if store_cfg is None:
        return None
    if not getattr(store_cfg, "enabled", False):
        return None

    # Effort gate — opt-out by passing effort=None to the factory.
    if effort is not None:
        gate = tuple(getattr(
            store_cfg, "enable_at_efforts", _DEFAULT_ENABLE_AT_EFFORTS,
        ) or ())
        if gate and effort not in gate:
            return None

    backend = (getattr(store_cfg, "backend", "memory") or "memory").lower()

    if backend == "memory":
        from langgraph.store.memory import InMemoryStore
        return InMemoryStore()

    # Provider-backed paths.
    if provider_loader is not None:
        provider = provider_loader(store_cfg)
    elif backend == "pgvector":
        provider = _load_pgvector(store_cfg)
    elif backend == "sqlite_vec":
        provider = _load_sqlite_vec(store_cfg)
    else:
        log.warning(
            "make_consultants_store: unknown backend %r — returning None",
            backend,
        )
        return None

    if provider is None:
        return None
    return ProviderBackedStore(provider)


def _load_pgvector(store_cfg):  # pragma: no cover — runtime-only
    try:
        from claude_hooks.providers.pgvector import PgvectorProvider
        from claude_hooks.providers.base import ServerCandidate
    except Exception:
        log.warning("pgvector provider import failed", exc_info=True)
        return None
    dsn = (
        getattr(store_cfg, "pgvector_dsn", None)
        or getattr(store_cfg, "dsn", None)
    )
    options: dict = {}
    if dsn:
        options["dsn"] = dsn
    table = getattr(store_cfg, "table", None)
    if table:
        options["table"] = table
    cand = ServerCandidate(server_key="consultants-pgvector", url="")
    try:
        return PgvectorProvider(cand, options=options)
    except Exception:
        log.warning("PgvectorProvider init failed", exc_info=True)
        return None


def _load_sqlite_vec(store_cfg):  # pragma: no cover — runtime-only
    try:
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider
        from claude_hooks.providers.base import ServerCandidate
    except Exception:
        log.warning("sqlite_vec provider import failed", exc_info=True)
        return None
    path = (
        getattr(store_cfg, "sqlite_vec_path", None)
        or getattr(store_cfg, "path", None)
    )
    options: dict = {}
    if path:
        options["db_path"] = path
    cand = ServerCandidate(server_key="consultants-sqlite-vec", url="")
    try:
        return SqliteVecProvider(cand, options=options)
    except Exception:
        log.warning("SqliteVecProvider init failed", exc_info=True)
        return None


# ============================================================== #
# Convenience helpers — every function is safe to call when
# ``store`` is None.
# ============================================================== #

def recall_research(
    store,
    sid: str,
    query: str,
    *,
    limit: int = 5,
) -> list:
    """Search the ``(sid, "research")`` namespace.

    Returns ``[]`` when ``store`` is None, when ``query`` is empty
    or whitespace-only, or when the underlying search raises. The
    caller (researcher_node) can use the empty list unconditionally.
    """
    if store is None or not query or not str(query).strip():
        return []
    try:
        return list(store.search(
            Namespaces.research(sid), query=query, limit=limit,
        ))
    except Exception:  # pragma: no cover — defensive
        log.exception("recall_research: search raised; returning []")
        return []


def record_research(
    store,
    sid: str,
    *,
    lane_idx: Optional[int],
    plan_item: Optional[str],
    finding: str,
    extra_meta: Optional[dict] = None,
) -> Optional[str]:
    """Write a research finding to ``(sid, "research")``.

    Returns the ``key`` written (deterministic) so the caller can
    log it, or ``None`` when the store is disabled / the finding
    is empty. Idempotent: re-recording the same finding from the
    same lane is a content-hash collision and overwrites in place.
    """
    if store is None or not finding or not str(finding).strip():
        return None
    h = hashlib.sha1(finding.encode("utf-8")).hexdigest()[:12]
    lane_part = "L?" if lane_idx is None else f"L{int(lane_idx)}"
    key = f"{lane_part}-{h}"
    value: dict = {
        "text": finding,
        "plan_item": plan_item or "",
        "lane_idx": lane_idx,
    }
    if extra_meta:
        value["meta"] = extra_meta
    try:
        store.put(
            Namespaces.research(sid), key, value,
            index=["text"],
        )
    except Exception:  # pragma: no cover — defensive
        log.exception("record_research: put raised; ignored")
        return None
    return key


def recall_for_follow_up(
    store,
    parent_sid: str,
    question: str,
    *,
    limit: int = 10,
) -> list:
    """Follow-up fallback: when ``prior_messages_by_role`` is empty
    (cold parent without transcript.db, v1.0 parents), recall the
    parent's research namespace via vector search instead of
    pre-seeding chronologically.

    Returns ``[]`` on disabled / failure / empty query.
    """
    return recall_research(store, parent_sid, question, limit=limit)


def format_findings_block(
    items: list,
    *,
    max_chars_per_item: int = 800,
    max_items: int = 8,
) -> str:
    """Render search results into a single text block suitable for
    prepending to the researcher's prompt.

    Truncates each finding at ``max_chars_per_item`` to keep the
    prompt budget bounded; callers usually pass ``limit=5`` already
    so the cap rarely bites. The output is plain markdown; an empty
    list returns an empty string (caller must guard / skip).
    """
    if not items:
        return ""
    lines = ["## Peer findings (recalled from earlier lanes)\n"]
    seen_texts: set[str] = set()
    rendered = 0
    for it in items:
        if rendered >= max_items:
            break
        value = getattr(it, "value", None) or {}
        text = value.get("text") or ""
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        plan_item = value.get("plan_item") or ""
        lane = value.get("lane_idx")
        head_parts = []
        if lane is not None:
            head_parts.append(f"lane {lane}")
        if plan_item:
            head_parts.append(f"plan: {plan_item}")
        head = (" — " + ", ".join(head_parts)) if head_parts else ""
        body = text.strip()
        if len(body) > max_chars_per_item:
            body = body[:max_chars_per_item].rstrip() + " […]"
        lines.append(f"- **finding**{head}\n  {body}\n")
        rendered += 1
    if rendered == 0:
        return ""
    return "\n".join(lines).rstrip() + "\n"


# ============================================================== #
# Small datetime / float helpers (private).
# ============================================================== #

def _parse_dt(s):
    if not s:
        return datetime.now(timezone.utc)
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return datetime.now(timezone.utc)


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
