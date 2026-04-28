"""
Migrate Qdrant + Memory-KG data into pgvector.

Idempotent on every level so the same script handles initial migration AND
ongoing delta sync. Tables are namespaced by embedding model so several
candidates can coexist while we benchmark them:

    memories_<model_short>            (vector(<dim>))
    kg_observations_<model_short>     (vector(<dim>))
    kg_entities                       (model-agnostic, shared)
    kg_relations                      (model-agnostic, shared)

Usage:

    # Single model — current recommended default for new installs is qwen3
    # (32k native ctx, 1024 dim, ~85 ms p50 embed). nomic remains the
    # speed-first fallback.
    python scripts/migrate_to_pgvector.py --embedder qwen3

    # Default if no flag is given is still nomic (kept for back-compat
    # with the original migration runs). New users should pass --embedder
    # qwen3 explicitly.
    python scripts/migrate_to_pgvector.py

    # All four models, both sources, no writes:
    python scripts/migrate_to_pgvector.py --embedder all --dry-run

    # Just delta-sync new Qdrant points since last run:
    python scripts/migrate_to_pgvector.py --embedder qwen3 --source qdrant

    # Explicit DSN override:
    python scripts/migrate_to_pgvector.py --embedder qwen3 \
        --dsn postgresql://claude:pass@127.0.0.1:5432/memory

Environment overrides (or pass --flag):
    PGVECTOR_DSN, QDRANT_URL, QDRANT_COLLECTION, MEMORY_KG_JSONL,
    OLLAMA_URL, OLLAMA_KEEP_ALIVE
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator, Optional

log = logging.getLogger("migrate_to_pgvector")

# ---------------------------------------------------------------------------
# Model registry — name → (ollama_model, dim, short)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ModelSpec:
    name: str           # CLI / config key
    ollama_model: str   # what to send to Ollama
    dim: int            # embedding dimension
    short: str          # table suffix
    max_chars: int      # safe input cap (≈ num_ctx_tokens * 3 chars/token)
    num_ctx: int        # passed as options.num_ctx — Ollama defaults to 2048,
                        # which is below the native limit for nomic/arctic


# Two truncation guards work together:
#   1. options.num_ctx is sent to Ollama so the server raises its context
#      window from the 2048 default to the model's native max.
#   2. max_chars truncates inputs in the script to stay safely below
#      num_ctx after tokenisation — token:char ratios vary (code or
#      non-English content tokenises tighter).
# We keep the original content intact in Postgres; only the embedding
# sees the truncated text.
MODELS: dict[str, ModelSpec] = {
    "minilm": ModelSpec("minilm", "locusai/all-minilm-l6-v2:latest", 384,  "minilm", 400,    256),    # 256-tok ctx
    "nomic":  ModelSpec("nomic",  "nomic-embed-text",                768,  "nomic",  5000,   8192),   # 8k tok ctx
    "arctic": ModelSpec("arctic", "snowflake-arctic-embed2",         1024, "arctic", 5000,   8192),   # 8k tok ctx
    "qwen3":  ModelSpec("qwen3",  "qwen3-embedding:0.6b",            1024, "qwen3",  30000,  32768),  # 32k tok ctx
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Config:
    dsn: str
    qdrant_url: str
    qdrant_collection: str
    memory_kg_jsonl: Path
    ollama_url: str
    ollama_keep_alive: str
    batch_size: int
    embed_batch_size: int
    dry_run: bool


def _load_pgvector_env_dotfile(path: str = "/shared/config/mcp-pgvector/.env") -> dict[str, str]:
    """Best-effort .env loader — populates os.environ-style dict from KEY=VAL lines."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def build_config(args: argparse.Namespace) -> Config:
    pg_env = _load_pgvector_env_dotfile()
    dsn = (
        args.dsn
        or os.environ.get("PGVECTOR_DSN")
        or _default_dsn_from_env(pg_env)
    )
    if not dsn:
        raise SystemExit(
            "no DSN: pass --dsn, set PGVECTOR_DSN, or populate "
            "/shared/config/mcp-pgvector/.env"
        )
    return Config(
        dsn=dsn,
        qdrant_url=args.qdrant_url or os.environ.get("QDRANT_URL", "http://192.168.178.2:6333"),
        qdrant_collection=args.qdrant_collection or os.environ.get("QDRANT_COLLECTION", "memory"),
        memory_kg_jsonl=Path(
            args.memory_kg_jsonl
            or os.environ.get("MEMORY_KG_JSONL", "/shared/config/mcp-memory/data/memory.jsonl")
        ),
        ollama_url=args.ollama_url or os.environ.get("OLLAMA_URL", "http://192.168.178.2:11434"),
        ollama_keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "15m"),
        batch_size=args.batch_size,
        embed_batch_size=args.embed_batch_size,
        dry_run=args.dry_run,
    )


def _default_dsn_from_env(pg_env: dict[str, str]) -> Optional[str]:
    user = pg_env.get("POSTGRES_USER")
    pw = pg_env.get("POSTGRES_PASSWORD")
    db = pg_env.get("POSTGRES_DB")
    if not (user and pw and db):
        return None
    return f"postgresql://{user}:{pw}@127.0.0.1:5432/{db}"


# ---------------------------------------------------------------------------
# Hashing — stable content_hash for idempotent upsert
# ---------------------------------------------------------------------------

def content_hash(text: str) -> bytes:
    """SHA-256 of the trimmed-and-collapsed UTF-8 text."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Ollama batch embedder (script-local — keeps embedders.py unchanged)
# ---------------------------------------------------------------------------

class OllamaBatchEmbedder:
    """POST /api/embed with input=[strings] for true batched embedding.

    Falls back to per-call /api/embeddings when /api/embed errors (older
    Ollama servers or unsupported models).
    """

    def __init__(
        self,
        url: str,
        model: str,
        keep_alive: str = "15m",
        timeout: float = 60.0,
        max_chars: Optional[int] = None,
        num_ctx: Optional[int] = None,
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.max_chars = max_chars
        self.num_ctx = num_ctx

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.max_chars:
            texts = [t[: self.max_chars] for t in texts]
        payload: dict = {"model": self.model, "input": texts, "keep_alive": self.keep_alive}
        if self.num_ctx:
            payload["options"] = {"num_ctx": self.num_ctx}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(
                f"ollama /api/embed HTTP {e.code} for model={self.model} "
                f"n_inputs={len(texts)} max_chars={max((len(t) for t in texts), default=0)}: "
                f"{err_body}"
            )
        embs = data.get("embeddings")
        if not isinstance(embs, list) or len(embs) != len(texts):
            raise RuntimeError(
                f"ollama /api/embed returned {len(embs) if embs else 0} embeddings for {len(texts)} inputs"
            )
        return embs


def _vec_to_pg_literal(vec: list[float]) -> str:
    """Render a Python list of floats as a pgvector literal: '[1.2,3.4,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ---------------------------------------------------------------------------
# Schema apply — per-model tables
# ---------------------------------------------------------------------------

def schema_sql_for_model(spec: ModelSpec) -> str:
    """Render the per-model vector tables. Idempotent (CREATE … IF NOT EXISTS)."""
    s = spec.short
    d = spec.dim
    return f"""
        CREATE TABLE IF NOT EXISTS memories_{s} (
            id              BIGSERIAL PRIMARY KEY,
            content         TEXT NOT NULL,
            content_hash    BYTEA NOT NULL,
            metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding       vector({d}) NOT NULL,
            source          TEXT NOT NULL DEFAULT 'manual',
            source_id       TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT memories_{s}_content_hash_unique UNIQUE (content_hash)
        );
        CREATE INDEX IF NOT EXISTS memories_{s}_embedding_hnsw
            ON memories_{s} USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        ALTER TABLE memories_{s}
            ADD COLUMN IF NOT EXISTS content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
        CREATE INDEX IF NOT EXISTS memories_{s}_content_tsv_gin
            ON memories_{s} USING gin (content_tsv);
        CREATE INDEX IF NOT EXISTS memories_{s}_source_idx
            ON memories_{s} (source);
        CREATE INDEX IF NOT EXISTS memories_{s}_created_at_idx
            ON memories_{s} (created_at DESC);

        CREATE TABLE IF NOT EXISTS kg_observations_{s} (
            id              BIGSERIAL PRIMARY KEY,
            entity_id       BIGINT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
            content         TEXT NOT NULL,
            content_hash    BYTEA NOT NULL,
            embedding       vector({d}) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT kg_observations_{s}_entity_hash_unique UNIQUE (entity_id, content_hash)
        );
        CREATE INDEX IF NOT EXISTS kg_observations_{s}_embedding_hnsw
            ON kg_observations_{s} USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        ALTER TABLE kg_observations_{s}
            ADD COLUMN IF NOT EXISTS content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
        CREATE INDEX IF NOT EXISTS kg_observations_{s}_content_tsv_gin
            ON kg_observations_{s} USING gin (content_tsv);
        CREATE INDEX IF NOT EXISTS kg_observations_{s}_entity_idx
            ON kg_observations_{s} (entity_id);
    """


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

def iter_qdrant_points(
    qdrant_url: str, collection: str, batch: int = 64
) -> Iterator[dict]:
    """Yield qdrant points one at a time using the scroll API."""
    offset = None
    while True:
        body = {"limit": batch, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        req = urllib.request.Request(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result") or {}
        points = result.get("points") or []
        for pt in points:
            yield pt
        offset = result.get("next_page_offset")
        if not offset or not points:
            return


def qdrant_point_to_memory(pt: dict) -> Optional[dict]:
    """Extract (content, metadata, source_id) from a Qdrant point payload.

    mcp-server-qdrant stores payloads as
        {"document": "<text>", "metadata": {...}}
    We tolerate other shapes by falling back to the first long string.
    """
    payload = pt.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    content = payload.get("document") or payload.get("content")
    if not isinstance(content, str) or not content.strip():
        # Last resort: any string value in the payload.
        for v in payload.values():
            if isinstance(v, str) and len(v) > 5:
                content = v
                break
        else:
            return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "content": content,
        "metadata": metadata,
        "source_id": str(pt.get("id")),
    }


def iter_memory_kg(jsonl_path: Path) -> Iterator[dict]:
    """Yield raw {type, ...} records from the Memory-KG jsonl file."""
    with open(jsonl_path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except json.JSONDecodeError as e:
                log.warning("memory_kg jsonl parse error: %s", e)


# ---------------------------------------------------------------------------
# Inserters
# ---------------------------------------------------------------------------

def upsert_memories(
    conn,
    spec: ModelSpec,
    rows: list[dict],
    embedder: OllamaBatchEmbedder,
    batch_size: int,
) -> int:
    """Bulk-upsert memories for one model. Returns rows actually inserted (excludes ON CONFLICT skips)."""
    if not rows:
        return 0
    # Embed in batches.
    inserted = 0
    table = f"memories_{spec.short}"
    sql = (
        f"INSERT INTO {table} (content, content_hash, metadata, embedding, source, source_id) "
        f"VALUES (%s, %s, %s, %s::vector, %s, %s) "
        f"ON CONFLICT (content_hash) DO NOTHING"
    )
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        vectors = embedder.embed([r["content"] for r in chunk])
        params = [
            (
                r["content"],
                content_hash(r["content"]),
                json.dumps(r.get("metadata") or {}),
                _vec_to_pg_literal(v),
                r.get("source", "manual"),
                r.get("source_id"),
            )
            for r, v in zip(chunk, vectors)
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, params)
            inserted += cur.rowcount or 0
        conn.commit()
    return inserted


def upsert_kg_entities(conn, entities: list[dict]) -> dict[str, int]:
    """Upsert entities by name. Returns name -> id mapping for ALL entities (existing + new)."""
    if not entities:
        return {}
    sql = (
        "INSERT INTO kg_entities (name, entity_type, metadata) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET entity_type = EXCLUDED.entity_type "
        "RETURNING id, name"
    )
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for e in entities:
            cur.execute(
                sql,
                (e["name"], e.get("entityType", "unknown"), json.dumps(e.get("metadata") or {})),
            )
            row = cur.fetchone()
            if row:
                eid, ename = row
                out[ename] = eid
    conn.commit()
    return out


def upsert_kg_observations(
    conn,
    spec: ModelSpec,
    obs: list[dict],
    embedder: OllamaBatchEmbedder,
    batch_size: int,
) -> int:
    """Bulk-insert KG observations for one model. Returns inserted count."""
    if not obs:
        return 0
    inserted = 0
    table = f"kg_observations_{spec.short}"
    sql = (
        f"INSERT INTO {table} (entity_id, content, content_hash, embedding) "
        f"VALUES (%s, %s, %s, %s::vector) "
        f"ON CONFLICT (entity_id, content_hash) DO NOTHING"
    )
    for i in range(0, len(obs), batch_size):
        chunk = obs[i : i + batch_size]
        vectors = embedder.embed([o["content"] for o in chunk])
        params = [
            (o["entity_id"], o["content"], content_hash(o["content"]), _vec_to_pg_literal(v))
            for o, v in zip(chunk, vectors)
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, params)
            inserted += cur.rowcount or 0
        conn.commit()
    return inserted


def upsert_kg_relations(conn, relations: list[dict], name_to_id: dict[str, int]) -> int:
    """Insert relations resolved by entity name. Returns inserted count."""
    if not relations:
        return 0
    sql = (
        "INSERT INTO kg_relations (from_entity_id, to_entity_id, relation_type) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO NOTHING"
    )
    inserted = 0
    skipped = 0
    with conn.cursor() as cur:
        for r in relations:
            f = name_to_id.get(r["from"])
            t = name_to_id.get(r["to"])
            if not f or not t:
                skipped += 1
                continue
            cur.execute(sql, (f, t, r["relationType"]))
            inserted += cur.rowcount or 0
    conn.commit()
    if skipped:
        log.warning("skipped %d relations with unknown endpoints", skipped)
    return inserted


# ---------------------------------------------------------------------------
# Main migration loop
# ---------------------------------------------------------------------------

def migrate_one_model(
    conn,
    spec: ModelSpec,
    cfg: Config,
    sources: list[str],
) -> dict:
    """Migrate selected sources into the per-model tables. Returns stats dict."""
    log.info("=== model %s (dim=%d) ===", spec.name, spec.dim)
    if not cfg.dry_run:
        with conn.cursor() as cur:
            cur.execute(schema_sql_for_model(spec))
        conn.commit()

    embedder = OllamaBatchEmbedder(
        cfg.ollama_url,
        spec.ollama_model,
        keep_alive=cfg.ollama_keep_alive,
        max_chars=spec.max_chars,
        num_ctx=spec.num_ctx,
    )
    stats = {"model": spec.name, "qdrant_inserted": 0, "kg_entities_seen": 0,
             "kg_observations_inserted": 0, "kg_relations_inserted": 0}

    if "qdrant" in sources:
        log.info("[%s] reading qdrant collection %s", spec.name, cfg.qdrant_collection)
        rows = []
        for pt in iter_qdrant_points(cfg.qdrant_url, cfg.qdrant_collection):
            mem = qdrant_point_to_memory(pt)
            if not mem:
                continue
            mem["source"] = "qdrant"
            rows.append(mem)
        log.info("[%s] qdrant points read: %d", spec.name, len(rows))
        if not cfg.dry_run:
            stats["qdrant_inserted"] = upsert_memories(
                conn, spec, rows, embedder, cfg.embed_batch_size
            )

    if "memory_kg" in sources:
        log.info("[%s] reading memory_kg jsonl %s", spec.name, cfg.memory_kg_jsonl)
        entities, relations = [], []
        for rec in iter_memory_kg(cfg.memory_kg_jsonl):
            t = rec.get("type")
            if t == "entity":
                entities.append(rec)
            elif t == "relation":
                relations.append(rec)
        log.info("[%s] memory_kg entities=%d relations=%d",
                 spec.name, len(entities), len(relations))
        stats["kg_entities_seen"] = len(entities)
        if cfg.dry_run:
            return stats

        # 1. Upsert entities to obtain stable IDs.
        name_to_id = upsert_kg_entities(conn, entities)
        # 2. Build observations list with entity_id.
        obs: list[dict] = []
        for e in entities:
            eid = name_to_id.get(e["name"])
            if not eid:
                continue
            for o in e.get("observations") or []:
                if isinstance(o, str) and o.strip():
                    obs.append({"entity_id": eid, "content": o})
        stats["kg_observations_inserted"] = upsert_kg_observations(
            conn, spec, obs, embedder, cfg.embed_batch_size
        )
        # 3. Resolve relations.
        stats["kg_relations_inserted"] = upsert_kg_relations(conn, relations, name_to_id)

    # 4. Update migration_state per source/model.
    if not cfg.dry_run:
        with conn.cursor() as cur:
            for src in sources:
                cur.execute(
                    """
                    INSERT INTO migration_state
                        (source, model, last_synced_at, rows_synced)
                    VALUES (%s, %s, now(), %s)
                    ON CONFLICT (source, model) DO UPDATE SET
                        last_synced_at = EXCLUDED.last_synced_at,
                        rows_synced = migration_state.rows_synced + EXCLUDED.rows_synced
                    """,
                    (
                        src,
                        spec.name,
                        stats["qdrant_inserted"] if src == "qdrant" else (
                            stats["kg_observations_inserted"] + stats["kg_relations_inserted"]
                        ),
                    ),
                )
        conn.commit()
    return stats


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--embedder",
        choices=list(MODELS.keys()) + ["all"],
        default="nomic",
        help="embedding model to use; 'all' runs every registered model",
    )
    p.add_argument(
        "--source",
        choices=["qdrant", "memory_kg", "all"],
        default="all",
        help="data source to import",
    )
    p.add_argument("--dsn", help="Postgres DSN (overrides PGVECTOR_DSN)")
    p.add_argument("--qdrant-url", help="Qdrant base URL")
    p.add_argument("--qdrant-collection", help="Qdrant collection name")
    p.add_argument("--memory-kg-jsonl", help="path to memory.jsonl")
    p.add_argument("--ollama-url", help="Ollama base URL")
    p.add_argument("--batch-size", type=int, default=128, help="DB executemany chunk")
    p.add_argument("--embed-batch-size", type=int, default=32, help="ollama batch")
    p.add_argument("--dry-run", action="store_true", help="parse + count, no writes")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = build_config(args)

    sources = ["qdrant", "memory_kg"] if args.source == "all" else [args.source]
    models = list(MODELS.values()) if args.embedder == "all" else [MODELS[args.embedder]]

    log.info("dsn=%s sources=%s models=%s dry_run=%s",
             _redact_dsn(cfg.dsn), sources, [m.name for m in models], cfg.dry_run)

    import psycopg
    all_stats = []
    with psycopg.connect(cfg.dsn) as conn:
        # Register pgvector adapter would be ideal, but the simple cast-with-text
        # approach in upsert_memories works against any psycopg version.
        for spec in models:
            t0 = time.perf_counter()
            stats = migrate_one_model(conn, spec, cfg, sources)
            stats["elapsed_s"] = round(time.perf_counter() - t0, 2)
            log.info("[%s] done in %.2fs: %s", spec.name, stats["elapsed_s"], stats)
            all_stats.append(stats)

    print(json.dumps({"stats": all_stats, "dry_run": cfg.dry_run}, indent=2))
    return 0


def _redact_dsn(dsn: str) -> str:
    """Strip password from a postgresql:// DSN for logging."""
    try:
        scheme, rest = dsn.split("://", 1)
        if "@" in rest:
            cred, hostpath = rest.split("@", 1)
            user = cred.split(":", 1)[0]
            return f"{scheme}://{user}:***@{hostpath}"
    except Exception:
        pass
    return "***"


if __name__ == "__main__":
    sys.exit(main())
