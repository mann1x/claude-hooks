FROM python:3.12-slim
RUN pip install --no-cache-dir mcp-proxy "mcp-server-qdrant[fastembed]"
# Patch mcp-server-qdrant to add:
#   1. QDRANT_SCORE_THRESHOLD support (upstream has no threshold filter)
#   2. Ollama embedding provider with optional fastembed failover
# Both patches are idempotent — safe to re-run on upstream bumps.
COPY files/ollama_provider.py /tmp/ollama_provider.py
COPY patch_score_threshold.py /tmp/patch_score_threshold.py
RUN python /tmp/patch_score_threshold.py && \
    rm /tmp/patch_score_threshold.py /tmp/ollama_provider.py
# Defaults — can be overridden by docker-compose environment:. --pass-environment
# below is REQUIRED so that compose overrides reach the mcp-server-qdrant child.
ENV QDRANT_URL=http://127.0.0.1:6333 \
    COLLECTION_NAME=memory \
    EMBEDDING_PROVIDER=fastembed
ENTRYPOINT ["mcp-proxy", "--pass-environment", "--stateless", "--port", "32775", "--host", "0.0.0.0", "--", "mcp-server-qdrant"]
