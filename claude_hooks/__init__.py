"""claude-hooks: cross-platform Claude Code hooks for memory recall/store.

Pluggable provider backends: Qdrant, Memory KG, Postgres+pgvector, sqlite-vec.
Multiple backends can run simultaneously — the dispatcher fans out recall in
parallel and merges the result blocks into the prompt.
"""

__version__ = "1.0.2"
