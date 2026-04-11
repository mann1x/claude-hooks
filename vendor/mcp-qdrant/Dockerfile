FROM python:3.12-slim
RUN pip install --no-cache-dir mcp-proxy "mcp-server-qdrant[fastembed]"
# Patch mcp-server-qdrant to honor QDRANT_SCORE_THRESHOLD (upstream has no threshold support).
# Idempotent script — safe to re-run on upstream upgrades.
COPY patch_score_threshold.py /tmp/patch_score_threshold.py
RUN python /tmp/patch_score_threshold.py && rm /tmp/patch_score_threshold.py
# Defaults — can be overridden by docker-compose environment:. --pass-environment
# below is REQUIRED so that compose overrides reach the mcp-server-qdrant child.
ENV QDRANT_URL=http://127.0.0.1:6333 \
    COLLECTION_NAME=memory \
    EMBEDDING_PROVIDER=fastembed
ENTRYPOINT ["mcp-proxy", "--pass-environment", "--stateless", "--port", "32775", "--host", "0.0.0.0", "--", "mcp-server-qdrant"]
