"""Service settings.

Hand-authored synthetic fixture for hard-02-cite-correct-line.
The trap: there are two ``MAX_BATCH_SIZE`` references near each
other — one is a comment that names the OLD value, the other is
the live assignment. An off-by-one citation is a fail.

Keep the layout below stable; the oracle resolves the live
assignment dynamically but expects the OLD value to remain a
comment, not a real binding.
"""

from __future__ import annotations


# Application name
APP_NAME: str = "configdrift"

# Database
DB_HOST: str = "localhost"
DB_PORT: int = 5432

# Batching
# Historical note: MAX_BATCH_SIZE used to be 100 before the
# 2026-03 capacity tune; the production-tuned value lives at the
# next assignment statement below.
MAX_BATCH_SIZE: int = 250

# Retries
RETRY_LIMIT: int = 3
RETRY_BACKOFF_S: float = 1.5

# Feature flags
ENABLE_TRACING: bool = False
ENABLE_METRICS: bool = True
