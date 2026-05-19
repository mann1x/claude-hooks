"""Configuration constants for the demo service.

Hand-authored synthetic fixture for the tool_executor bench
``trivial-01-find-symbol`` question. The single named constant
the question asks about is ``DEFAULT_TIMEOUT_S`` below — the line
number is what the oracle checks, so do NOT reflow this file
without updating the oracle accordingly.
"""

from __future__ import annotations


# Network / IO defaults
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8080
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_RETRIES: int = 3

# Cache defaults
CACHE_SIZE: int = 1024
CACHE_TTL_S: int = 600

# Logging defaults
LOG_LEVEL: str = "INFO"
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(message)s"


def is_valid_timeout(value: float) -> bool:
    """Return True when ``value`` is a positive finite timeout
    suitable for use in place of ``DEFAULT_TIMEOUT_S``.
    """
    return value > 0 and value < float("inf")
