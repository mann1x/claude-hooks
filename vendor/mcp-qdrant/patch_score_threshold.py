"""
Patch mcp-server-qdrant to support QDRANT_SCORE_THRESHOLD env var.
Applied at Docker build time. Idempotent — safe to re-run on upstream upgrades.
"""
from pathlib import Path
import sys

base = Path("/usr/local/lib/python3.12/site-packages/mcp_server_qdrant")

# 1) settings.py — add score_threshold field
sf = base / "settings.py"
src = sf.read_text()
if "QDRANT_SCORE_THRESHOLD" not in src:
    src = src.replace(
        'search_limit: int = Field(default=10, validation_alias="QDRANT_SEARCH_LIMIT")',
        'search_limit: int = Field(default=10, validation_alias="QDRANT_SEARCH_LIMIT")\n'
        '    score_threshold: float | None = Field(default=None, validation_alias="QDRANT_SCORE_THRESHOLD")',
    )
    sf.write_text(src)
    print("[patch] settings.py: added score_threshold")
else:
    print("[patch] settings.py: already has score_threshold")

# 2) qdrant.py — add score_threshold param to search() and pass to query_points
qf = base / "qdrant.py"
src = qf.read_text()
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
if "score_threshold" not in src:
    src = src.replace(old_sig, new_sig).replace(old_call, new_call)
    qf.write_text(src)
    print("[patch] qdrant.py: added score_threshold")
else:
    print("[patch] qdrant.py: already patched")

# 3) mcp_server.py — forward settings.score_threshold into search()
mf = base / "mcp_server.py"
src = mf.read_text()
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
if "score_threshold=self.qdrant_settings.score_threshold" not in src:
    src = src.replace(old, new)
    mf.write_text(src)
    print("[patch] mcp_server.py: forwarded score_threshold")
else:
    print("[patch] mcp_server.py: already patched")

# Sanity: ensure the patched settings class can load
sys.path.insert(0, str(base.parent))
from mcp_server_qdrant.settings import QdrantSettings  # noqa
print("[patch] settings module imports cleanly")
