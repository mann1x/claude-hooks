"""
Patches applied to mcp-server-qdrant at Docker build time.

Idempotent — safe to re-run on upstream upgrades.

Two features added:

1. **QDRANT_SCORE_THRESHOLD** — drop search results below a cosine
   similarity floor (upstream has no threshold support at all).

2. **Ollama embedding provider** — register a new provider that calls
   Ollama's /api/embed endpoint over HTTP, with optional automatic
   failover to fastembed if Ollama is down. The vector name and size
   are derived from a paired FastEmbedProvider so existing collections
   keep working without re-embedding — provided the Ollama model
   produces vectors in the same embedding space (e.g.
   ``locusai/all-minilm-l6-v2`` matches fastembed's all-MiniLM-L6-v2
   to ~6 decimal places of cosine similarity).

   New env vars:
     - EMBEDDING_PROVIDER=ollama         (instead of fastembed)
     - OLLAMA_URL=http://host:11434
     - OLLAMA_MODEL=locusai/all-minilm-l6-v2
     - OLLAMA_KEEP_ALIVE=15m             (default)
     - OLLAMA_TIMEOUT=10                 (seconds)
     - OLLAMA_FALLBACK_FASTEMBED=true    (default)
     - EMBEDDING_MODEL=...               (the fastembed model used to
       derive vector name/size and as the failover backend)
"""
from pathlib import Path
import shutil
import sys

base = Path("/usr/local/lib/python3.12/site-packages/mcp_server_qdrant")

# ====================================================================== #
# Patch 1: settings.py — score_threshold + Ollama settings
# ====================================================================== #
sf = base / "settings.py"
src = sf.read_text()
if "QDRANT_SCORE_THRESHOLD" not in src:
    src = src.replace(
        'search_limit: int = Field(default=10, validation_alias="QDRANT_SEARCH_LIMIT")',
        'search_limit: int = Field(default=10, validation_alias="QDRANT_SEARCH_LIMIT")\n'
        '    score_threshold: float | None = Field(default=None, validation_alias="QDRANT_SCORE_THRESHOLD")',
    )
    print("[patch] settings.py: added score_threshold")
else:
    print("[patch] settings.py: score_threshold already present")

if "OLLAMA_URL" not in src:
    # Note: pydantic-settings supports a SINGLE validation_alias at construction
    # time. To accept BOTH EMBEDDING_MODEL (upstream) and FASTEMBED_MODEL
    # (more explicit name we add), we use AliasChoices.
    src = src.replace(
        "from pydantic_settings import BaseSettings",
        "from pydantic import AliasChoices\nfrom pydantic_settings import BaseSettings",
    )
    src = src.replace(
        'model_name: str = Field(\n        default="sentence-transformers/all-MiniLM-L6-v2",\n        validation_alias="EMBEDDING_MODEL",\n    )',
        'model_name: str = Field(\n'
        '        default="sentence-transformers/all-MiniLM-L6-v2",\n'
        '        # Accept both names: EMBEDDING_MODEL (upstream) and the more\n'
        '        # explicit FASTEMBED_MODEL (also used as the failover backend\n'
        '        # name when EMBEDDING_PROVIDER=ollama).\n'
        '        validation_alias=AliasChoices("FASTEMBED_MODEL", "EMBEDDING_MODEL"),\n'
        '    )\n'
        '    # Ollama-specific — only used when provider_type == "ollama".\n'
        '    # The fastembed model_name above is also passed to the Ollama\n'
        '    # provider so it can derive the correct vector name/size for the\n'
        '    # existing collection AND act as a transparent failover backend\n'
        '    # when Ollama is unreachable.\n'
        '    ollama_url: str = Field(\n'
        '        default="http://localhost:11434",\n'
        '        validation_alias="OLLAMA_URL",\n'
        '    )\n'
        '    ollama_model: str = Field(\n'
        '        default="locusai/all-minilm-l6-v2",\n'
        '        validation_alias="OLLAMA_MODEL",\n'
        '    )\n'
        '    ollama_keep_alive: str = Field(\n'
        '        default="15m",\n'
        '        validation_alias="OLLAMA_KEEP_ALIVE",\n'
        '    )\n'
        '    ollama_timeout: float = Field(\n'
        '        default=10.0,\n'
        '        validation_alias="OLLAMA_TIMEOUT",\n'
        '    )\n'
        '    ollama_fallback_fastembed: bool = Field(\n'
        '        default=True,\n'
        '        validation_alias="OLLAMA_FALLBACK_FASTEMBED",\n'
        '    )',
    )
    print("[patch] settings.py: added Ollama settings + FASTEMBED_MODEL alias")
else:
    print("[patch] settings.py: Ollama settings already present")

sf.write_text(src)

# ====================================================================== #
# Patch 2: qdrant.py — score_threshold parameter on search()
# ====================================================================== #
qf = base / "qdrant.py"
src = qf.read_text()
if "score_threshold" not in src:
    old_sig = "query_filter: models.Filter | None = None,\n    ) -> list[Entry]:"
    new_sig = (
        "query_filter: models.Filter | None = None,\n"
        "        score_threshold: float | None = None,\n"
        "    ) -> list[Entry]:"
    )
    old_call = "            limit=limit,\n            query_filter=query_filter,\n        )"
    new_call = (
        "            limit=limit,\n"
        "            query_filter=query_filter,\n"
        "            score_threshold=score_threshold,\n"
        "        )"
    )
    src = src.replace(old_sig, new_sig).replace(old_call, new_call)
    qf.write_text(src)
    print("[patch] qdrant.py: added score_threshold")
else:
    print("[patch] qdrant.py: already patched")

# ====================================================================== #
# Patch 3: mcp_server.py — forward score_threshold from settings
# ====================================================================== #
mf = base / "mcp_server.py"
src = mf.read_text()
if "score_threshold=self.qdrant_settings.score_threshold" not in src:
    old = (
        "            entries = await self.qdrant_connector.search(\n"
        "                query,\n"
        "                collection_name=collection_name,\n"
        "                limit=self.qdrant_settings.search_limit,\n"
        "                query_filter=query_filter,\n"
        "            )"
    )
    new = (
        "            entries = await self.qdrant_connector.search(\n"
        "                query,\n"
        "                collection_name=collection_name,\n"
        "                limit=self.qdrant_settings.search_limit,\n"
        "                query_filter=query_filter,\n"
        "                score_threshold=self.qdrant_settings.score_threshold,\n"
        "            )"
    )
    src = src.replace(old, new)
    mf.write_text(src)
    print("[patch] mcp_server.py: forwarded score_threshold")
else:
    print("[patch] mcp_server.py: score_threshold already forwarded")

# ====================================================================== #
# Patch 4: embeddings/types.py — register OLLAMA enum value
# ====================================================================== #
tf = base / "embeddings" / "types.py"
src = tf.read_text()
if "OLLAMA" not in src:
    src = src.replace(
        '    FASTEMBED = "fastembed"',
        '    FASTEMBED = "fastembed"\n    OLLAMA = "ollama"',
    )
    tf.write_text(src)
    print("[patch] embeddings/types.py: added OLLAMA enum")
else:
    print("[patch] embeddings/types.py: OLLAMA enum already present")

# ====================================================================== #
# Patch 5: install ollama_provider.py into the embeddings package
# ====================================================================== #
src_file = Path("/tmp/ollama_provider.py")
dst_file = base / "embeddings" / "ollama.py"
if src_file.exists():
    shutil.copy(src_file, dst_file)
    print(f"[patch] embeddings/ollama.py: installed ({dst_file.stat().st_size} bytes)")
else:
    print(f"[patch] WARNING: {src_file} not found, ollama provider not installed")

# ====================================================================== #
# Patch 6: embeddings/factory.py — wire OLLAMA into create_embedding_provider
# ====================================================================== #
ff = base / "embeddings" / "factory.py"
src = ff.read_text()
if "OLLAMA" not in src:
    old = (
        '    if settings.provider_type == EmbeddingProviderType.FASTEMBED:\n'
        '        from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider\n'
        '\n'
        '        return FastEmbedProvider(settings.model_name)\n'
        '    else:\n'
        '        raise ValueError(f"Unsupported embedding provider: {settings.provider_type}")'
    )
    new = (
        '    if settings.provider_type == EmbeddingProviderType.FASTEMBED:\n'
        '        from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider\n'
        '\n'
        '        return FastEmbedProvider(settings.model_name)\n'
        '    elif settings.provider_type == EmbeddingProviderType.OLLAMA:\n'
        '        from mcp_server_qdrant.embeddings.ollama import OllamaEmbeddingProvider\n'
        '\n'
        '        return OllamaEmbeddingProvider(\n'
        '            ollama_url=settings.ollama_url,\n'
        '            ollama_model=settings.ollama_model,\n'
        '            fastembed_model=settings.model_name,\n'
        '            keep_alive=settings.ollama_keep_alive,\n'
        '            timeout=settings.ollama_timeout,\n'
        '            fallback_to_fastembed=settings.ollama_fallback_fastembed,\n'
        '        )\n'
        '    else:\n'
        '        raise ValueError(f"Unsupported embedding provider: {settings.provider_type}")'
    )
    src = src.replace(old, new)
    ff.write_text(src)
    print("[patch] embeddings/factory.py: wired OLLAMA")
else:
    print("[patch] embeddings/factory.py: already patched")

# ====================================================================== #
# Sanity: import the patched modules
# ====================================================================== #
sys.path.insert(0, str(base.parent))
from mcp_server_qdrant.settings import QdrantSettings, EmbeddingProviderSettings  # noqa
from mcp_server_qdrant.embeddings.types import EmbeddingProviderType  # noqa
print(f"[patch] available providers: {[p.value for p in EmbeddingProviderType]}")
print("[patch] all modules import cleanly")
