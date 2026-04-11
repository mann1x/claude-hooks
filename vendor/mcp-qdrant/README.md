# vendor/mcp-qdrant

Patched Docker build of [`mcp-server-qdrant`](https://github.com/qdrant/mcp-server-qdrant)
that adds a `QDRANT_SCORE_THRESHOLD` environment variable — **missing upstream**.

Without this, `qdrant-find` always returns `QDRANT_SEARCH_LIMIT` results (default
10), no matter how weak the cosine similarity. On a realistic memory store this
means every recall pulls in low-similarity noise that pollutes the model's
context on every prompt. The patch wires Qdrant's native `score_threshold` into
the MCP server so you can filter out anything below a confidence cutoff.

> **Tracking**: patch applies cleanly against `mcp-server-qdrant` as published
> on PyPI as of 2026-04. The upstream issue is
> [qdrant/mcp-server-qdrant#44](https://github.com/qdrant/mcp-server-qdrant/issues/44) —
> if/when it lands upstream, this vendor dir can be deleted.

---

## What you get

- Same image / endpoints as upstream (`qdrant-find`, `qdrant-store`)
- Plus: honors `QDRANT_SCORE_THRESHOLD=<float>` — drops any result below the
  given cosine similarity. Leave unset to behave exactly like upstream.
- Idempotent build-time patch — `docker compose build` re-applies it on every
  upstream bump without manual intervention.

---

## Usage

### 1. Build the image

```bash
cd vendor/mcp-qdrant
cp docker-compose.example.yaml docker-compose.yaml     # or symlink, or use -f
docker compose build
```

The build prints each patched file, then bakes the modified
`mcp_server_qdrant` package into the image:

```
[patch] settings.py: added score_threshold
[patch] qdrant.py: added score_threshold
[patch] mcp_server.py: forwarded score_threshold
[patch] settings module imports cleanly
```

### 2. Configure and start

Edit `docker-compose.yaml` to pick your threshold:

```yaml
environment:
  - QDRANT_URL=http://127.0.0.1:6333
  - COLLECTION_NAME=memory
  - EMBEDDING_PROVIDER=fastembed
  - QDRANT_SCORE_THRESHOLD=0.40   # <-- drop hits below this cosine score
```

Then:

```bash
docker compose up -d
```

### 3. Point claude-hooks at it

In `config/claude-hooks.json`:

```json
"providers": {
  "qdrant": {
    "enabled": true,
    "mcp_url": "http://YOUR-HOST:32775/mcp",
    "collection": "memory",
    "recall_k": 5
  }
}
```

The client-side `recall_k` still controls how many hits you inject, but the
server will already have filtered out anything below `score_threshold`, so
`recall_k` becomes a cap on *relevant* hits rather than a fixed count of noise.

---

## Picking a threshold

Score ranges depend on your embedding model. With `fastembed` defaults
(`sentence-transformers/all-MiniLM-L6-v2`):

| Range | Meaning |
|-------|---------|
| `> 0.6` | Strong match — topic-specific vocabulary hit |
| `0.4 – 0.6` | Relevant — same domain, different phrasing |
| `0.3 – 0.4` | Weak — vaguely related |
| `< 0.3` | Noise — unrelated text that merely shares stopwords |

**Starting point**: `0.40` — filters out obvious noise while still retrieving
borderline relevant memories. Tune down to `0.35` if recall feels too
aggressive, up to `0.45` if you're still seeing pollution.

### How to measure the score distribution for your own store

```bash
docker exec -it mcp-qdrant-mcp-qdrant-1 python - <<'PY'
import asyncio
from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider
from qdrant_client import AsyncQdrantClient

async def main():
    emb = FastEmbedProvider(model_name="sentence-transformers/all-MiniLM-L6-v2")
    client = AsyncQdrantClient(url="http://127.0.0.1:6333")
    for q in ["a real query you care about", "total garbage query"]:
        vec = await emb.embed_query(q)
        res = await client.query_points(
            collection_name="memory",
            query=vec,
            using=emb.get_vector_name(),
            limit=5,
        )
        print(q, [round(p.score, 3) for p in res.points])

asyncio.run(main())
PY
```

Pick a threshold that sits between your real queries' top scores and your
garbage queries' top scores.

---

## Files

- `Dockerfile` — `FROM python:3.12-slim`, installs `mcp-proxy` +
  `mcp-server-qdrant[fastembed]`, copies and runs the patch script, then sets
  up the same `ENTRYPOINT` upstream expects.
- `patch_score_threshold.py` — idempotent script that edits three files in
  the installed `mcp_server_qdrant` package:
  - `settings.py` — adds the `score_threshold` field bound to the env var
  - `qdrant.py` — adds `score_threshold` parameter to `QdrantConnector.search()`
    and forwards it to `query_points()`
  - `mcp_server.py` — passes `self.qdrant_settings.score_threshold` into the
    `search()` call
- `docker-compose.example.yaml` — full stack (Qdrant DB + patched MCP server),
  both on host networking so you don't need to manage Docker networks.

---

## Upstreaming

If you want to contribute this back to upstream, the diff is ~10 lines across
3 files. The patch script doubles as a reference for what would need to change
in a proper PR. The main design question upstream would be whether to accept
the threshold as an env var, a per-call tool argument, or both.
