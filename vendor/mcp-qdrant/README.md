# vendor/mcp-qdrant

Patched Docker build of [`mcp-server-qdrant`](https://github.com/qdrant/mcp-server-qdrant)
that adds two features missing upstream:

1. **`QDRANT_SCORE_THRESHOLD`** — drop search results below a cosine
   similarity floor. Without this, `qdrant-find` always returns
   `QDRANT_SEARCH_LIMIT` results no matter how weak the matches, which
   pollutes the model's context with noise on every prompt.
2. **Ollama embedding provider with FastEmbed failover** — call
   Ollama's `/api/embed` endpoint for GPU-accelerated embeddings, and
   fall back to in-process FastEmbed transparently when Ollama is
   unreachable. The vector name and size are derived from a paired
   FastEmbed model, so an existing FastEmbed-built collection keeps
   working **without re-embedding** — provided the Ollama model
   produces vectors in the same embedding space (more on that below).

> **Tracking**: patches apply cleanly against `mcp-server-qdrant` as
> published on PyPI as of 2026-04. The patch script is idempotent —
> upstream version bumps don't require rebasing. If/when these land
> upstream, this vendor dir can be deleted.

---

## What you get

- Same image / endpoints as upstream (`qdrant-find`, `qdrant-store`)
- `QDRANT_SCORE_THRESHOLD=<float>` filters out weak matches
- `EMBEDDING_PROVIDER=ollama` routes embeds through Ollama (GPU)
- `OLLAMA_FALLBACK_FASTEMBED=true` (default) — transparent CPU
  fallback when Ollama is down
- Idempotent build-time patches — `docker compose build` re-applies
  them on every upstream bump

---

## Usage

### 1. Build the image

```bash
cd vendor/mcp-qdrant
cp docker-compose.example.yaml docker-compose.yaml
docker compose build
```

The build prints each patched file:

```
[patch] settings.py: added score_threshold
[patch] settings.py: added Ollama settings + FASTEMBED_MODEL alias
[patch] qdrant.py: added score_threshold
[patch] mcp_server.py: forwarded score_threshold
[patch] embeddings/types.py: added OLLAMA enum
[patch] embeddings/ollama.py: installed (6168 bytes)
[patch] embeddings/factory.py: wired OLLAMA
[patch] available providers: ['fastembed', 'ollama']
[patch] all modules import cleanly
```

### 2. Configure and start

Edit `docker-compose.yaml`:

```yaml
environment:
  - QDRANT_URL=http://127.0.0.1:6333
  - COLLECTION_NAME=memory
  - QDRANT_SCORE_THRESHOLD=0.40       # drop hits below this cosine score

  # ----- Embedding provider -----
  # Either keep the upstream fastembed default:
  - EMBEDDING_PROVIDER=fastembed
  - FASTEMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2

  # OR switch to Ollama (with fastembed failover):
  # - EMBEDDING_PROVIDER=ollama
  # - OLLAMA_URL=http://YOUR-OLLAMA-HOST:11434
  # - OLLAMA_MODEL=locusai/all-minilm-l6-v2
  # - OLLAMA_KEEP_ALIVE=15m
  # - OLLAMA_FALLBACK_FASTEMBED=true
  # - FASTEMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2  # used for
  #     # vector name/size derivation AND as failover backend
```

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

## The Ollama embedding provider in detail

### When (and when NOT) to use Ollama

The intuitive assumption is "GPU > CPU, so route embeddings through
Ollama for speed". **For tiny embedding models, this is wrong.**
Ollama wins decisively for *large* models (≥300M params), and loses
just as decisively for *small* ones (≤100M params).

We benchmarked the same MCP `qdrant-find` call against the same
collection with both backends, using `all-MiniLM-L6-v2` (22M params):

| Backend | Avg latency (10 calls) |
|---|---|
| **fastembed CPU** | **~26 ms** ⭐ |
| Ollama GPU (locusai/all-minilm-l6-v2, same weights as fp32 GGUF) | ~65 ms |

For a 22M-param model:

- CPU inference is ~5 ms on a modern x86 with AVX2/AVX512
- Ollama adds ~50 ms of HTTP round-trip + JSON serialization +
  GPU dispatch + model context queueing
- The network overhead dwarfs the actual compute, so the "GPU win"
  never materializes

**Where Ollama IS the right choice — bigger embedding models:**

| Model | Params | CPU inf. (fastembed) | GPU inf. (Ollama) | Net winner |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | 22M | ~5 ms | ~10 ms +50 ms net | **fastembed +40 ms** |
| `bge-base-en-v1.5` | 110M | ~25 ms | ~12 ms +50 ms net | fastembed +13 ms |
| `bge-large-en-v1.5` | 335M | ~80 ms | ~15 ms +50 ms net | **Ollama +15 ms** |
| `snowflake-arctic-embed2` | 568M | ~150 ms | ~20 ms +50 ms net | **Ollama +80 ms** ⭐ |
| `bge-m3` | 568M | ~150 ms | ~20 ms +50 ms net | **Ollama +80 ms** ⭐ |

The crossover is around **~300M parameters**. Below that, network
overhead exceeds compute savings; above that, GPU inference wins
decisively.

**Quality scales with model size.** Models like `snowflake-arctic-embed2`
or `bge-m3` are widely considered SOTA open embedding models and
significantly improve recall quality on niche/technical queries. If you
hit a ceiling on the small model's recall quality, the migration path is:

1. Pull the bigger model: `ollama pull snowflake-arctic-embed2`
2. Re-embed your existing collection (one-shot job, ~minutes for hundreds of entries)
3. Set in compose:
   ```yaml
   - EMBEDDING_PROVIDER=ollama
   - OLLAMA_MODEL=snowflake-arctic-embed2
   - FASTEMBED_MODEL=BAAI/bge-small-en-v1.5  # any same-dim fallback
   - OLLAMA_FALLBACK_FASTEMBED=true
   ```
4. Higher recall quality **and** faster than fastembed CPU could deliver,
   with a small CPU model as a safety net during Ollama hiccups.

**Rule of thumb**: stay on `fastembed` for ≤100M-param embedders.
Switch to Ollama at ≥300M params or if you specifically want a unified
Ollama stack for a larger SOTA model.

### The catch: vector space compatibility

The big risk of swapping embedding providers on an existing collection
is that the new model will produce vectors in a **different embedding
space**. Even if dimensions match, the cosine similarity between the
"same text embedded by both providers" tells you whether they are
interchangeable. We tested several `all-MiniLM-L6-v2` Ollama variants
against fastembed's `sentence-transformers/all-MiniLM-L6-v2`:

| Ollama model | Cosine sim with fastembed (same text) | Verdict |
|---|---|---|
| `all-minilm:33m` (default Ollama tag, fp16 GGUF) | **0.44 – 0.58** | ❌ different space — search would break |
| `locusai/all-minilm-l6-v2` (fp32 GGUF) | **0.999998** | ✅ effectively identical |

`locusai/all-minilm-l6-v2` is the **only known fp32 GGUF variant** that
matches fastembed's ONNX output to floating-point precision. Use it if
you want to switch to Ollama on an existing collection without
re-embedding. Pull it with:

```bash
ollama pull locusai/all-minilm-l6-v2
```

### Failover behaviour

When `OLLAMA_FALLBACK_FASTEMBED=true` (default), any transient Ollama
error in `embed_query`/`embed_documents` is logged at WARNING level and
the call automatically falls through to the in-process FastEmbed
backend. This means:

- Search **keeps working** during Ollama restarts / model swaps
- The fastembed model stays loaded as a hot standby (it's the same
  process, no extra container)
- The fastembed model **must** be in the same embedding space as the
  Ollama model — otherwise failover would silently corrupt search
  results. Stick to the matched pair recommended above.

Set `OLLAMA_FALLBACK_FASTEMBED=false` to make Ollama failures hard
errors instead — useful if you'd rather see the breakage than have
half your queries served by a different model.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `EMBEDDING_PROVIDER` | `fastembed` | `fastembed` or `ollama` |
| `EMBEDDING_MODEL` / `FASTEMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | FastEmbed model name. Used for vector name/size derivation AND as the failover backend. Both names accepted. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `locusai/all-minilm-l6-v2` | Ollama model tag |
| `OLLAMA_KEEP_ALIVE` | `15m` | How long Ollama should keep the model resident in VRAM after the last request |
| `OLLAMA_TIMEOUT` | `10` | Per-request timeout in seconds |
| `OLLAMA_FALLBACK_FASTEMBED` | `true` | Transparent fallback to FastEmbed on Ollama errors |
| `QDRANT_SCORE_THRESHOLD` | unset | Drop hits below this cosine similarity |
| `QDRANT_URL`, `COLLECTION_NAME`, `QDRANT_SEARCH_LIMIT`, etc. | (upstream) | Standard upstream env vars, unchanged |

---

## Picking a score threshold

Score ranges depend on your embedding model. With `all-MiniLM-L6-v2`
(via either backend — they produce the same scores):

| Range | Meaning |
|-------|---------|
| `> 0.6` | Strong match — topic-specific vocabulary hit |
| `0.4 – 0.6` | Relevant — same domain, different phrasing |
| `0.3 – 0.4` | Weak — vaguely related |
| `< 0.3` | Noise — unrelated text that merely shares stopwords |

**Starting point**: `0.40` — filters out obvious noise while still
retrieving borderline-relevant memories. Tune down to `0.35` if recall
feels too aggressive, up to `0.45` if you're still seeing pollution.

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

Pick a threshold that sits between your real queries' top scores and
your garbage queries' top scores.

---

## Files

- `Dockerfile` — `FROM python:3.12-slim`, installs `mcp-proxy` +
  `mcp-server-qdrant[fastembed]`, copies the patch script and the
  Ollama provider source, runs the patch script at build time, then
  sets up the same `ENTRYPOINT` upstream expects.
- `patch_score_threshold.py` — idempotent script that:
  - Adds `score_threshold` to `settings.py` and threads it through
    `qdrant.py` → `mcp_server.py` → `query_points()`
  - Adds Ollama settings (`OLLAMA_*` env vars) and the
    `FASTEMBED_MODEL` alias on `EMBEDDING_MODEL` to `settings.py`
  - Registers `OLLAMA = "ollama"` in `embeddings/types.py`
  - Installs `embeddings/ollama.py` (the new provider, copied from
    `files/ollama_provider.py`)
  - Wires the `OLLAMA` enum into `embeddings/factory.py`
- `files/ollama_provider.py` — the new embedding provider. Calls
  Ollama's `/api/embed` over HTTP, delegates vector metadata to a
  paired `FastEmbedProvider`, and supports automatic failover.
- `docker-compose.example.yaml` — full stack (Qdrant DB + patched MCP
  server), both on host networking.

---

## Upstreaming

The score-threshold patch is ~10 lines across 3 files and would make
a clean upstream PR. The Ollama provider is larger (~150 lines + the
factory wiring) but follows the same pattern as the existing
`FastEmbedProvider`. The patch script doubles as a reference for what
would change in a proper PR — the main design question upstream is
whether failover should live inside the Ollama provider (current
approach) or be a separate "failover wrapper" that can compose
arbitrary providers.
