"""Network-identifier fixtures for the claude-hooks test suite.

The test suite never makes real network calls in unit-test mode — every
URL / DSN / host that appears in a fixture is fed to a function whose
HTTP / DB layer is mocked. But the *values* still have to live
somewhere, and historically they were hard-coded to whatever LAN the
maintainer happened to develop on. That made the suite leaky (real
LAN IPs in version control) and confusing for outside contributors,
who couldn't tell at a glance which IPs were real vs fixture noise.

This module centralizes those values. All defaults are drawn from
**RFC 5737** documentation address blocks
(``TEST-NET-1`` = 192.0.2.0/24, ``TEST-NET-2`` = 198.51.100.0/24,
``TEST-NET-3`` = 203.0.113.0/24) which are reserved for use in
documentation and cannot route on any real network — so a fixture
"leaking" one is obviously fake.

Every constant is overridable via an environment variable for the
small number of tests that actually exercise live infrastructure
(notably ``tests/test_pgvector_integration.py``). The override layer
is two-tier:

1. **Process environment** — ``CLAUDE_HOOKS_TEST_*`` variables read at
   import time. This is the CI-friendly path: set the env var, run
   ``pytest``, done.
2. **Local override file** — if ``tests/.env.local`` exists (which is
   gitignored), it's parsed as a simple ``KEY=value`` file *before*
   the env-var lookup, so a developer can drop their real DSN /
   URLs in one place without ever committing them. Process env still
   wins over the file so CI can override the developer override.

See ``tests/README.md`` for the full contribution guide.
"""

from __future__ import annotations

import os
from pathlib import Path


# --------------------------------------------------------------------- #
# Local .env.local loader (best-effort; never raises).
# --------------------------------------------------------------------- #
def _load_local_env() -> dict[str, str]:
    """Read ``tests/.env.local`` if present. Lines like ``KEY=value``;
    blank lines and ``#`` comments are skipped. Returns a dict.

    Process environment variables take precedence — we never overwrite
    something already set in ``os.environ``."""
    out: dict[str, str] = {}
    p = Path(__file__).resolve().parent / ".env.local"
    try:
        with open(p, encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    out[k] = v
    except OSError:
        # File missing is the common case; absent or unreadable both
        # fall back to the documentation defaults.
        pass
    return out


_LOCAL = _load_local_env()


def _get(key: str, default: str) -> str:
    """Resolution order: ``os.environ[key]`` -> ``tests/.env.local[key]``
    -> documentation default."""
    return os.environ.get(key) or _LOCAL.get(key) or default


# --------------------------------------------------------------------- #
# Documentation-network hosts.
#
# Three distinct hosts are provided because some fixtures need to
# represent "two different machines on a LAN" (e.g. proxy host vs
# embedder host). Anyone overriding via env vars can collapse them
# back to a single host if they prefer.
# --------------------------------------------------------------------- #
FIXTURE_LAN_HOST = _get("CLAUDE_HOOKS_TEST_LAN_HOST", "192.0.2.10")
FIXTURE_LAN_HOST_ALT = _get("CLAUDE_HOOKS_TEST_LAN_HOST_ALT", "198.51.100.5")
FIXTURE_LAN_HOST_THIRD = _get("CLAUDE_HOOKS_TEST_LAN_HOST_THIRD", "203.0.113.7")


def _u(host: str, port: int, path: str = "") -> str:
    return f"http://{host}:{port}{path}"


# --------------------------------------------------------------------- #
# Ollama URLs. claude-hooks defaults to talking to a local proxy on
# 11433 and to direct Ollama on 11434.
# --------------------------------------------------------------------- #
FIXTURE_OLLAMA_PROXY_BASE = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_PROXY_BASE",
    _u(FIXTURE_LAN_HOST, 11433),
)
FIXTURE_OLLAMA_PROXY_GENERATE = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_PROXY_GENERATE",
    f"{FIXTURE_OLLAMA_PROXY_BASE}/api/generate",
)
FIXTURE_OLLAMA_PROXY_TAGS = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_PROXY_TAGS",
    f"{FIXTURE_OLLAMA_PROXY_BASE}/api/tags",
)
FIXTURE_OLLAMA_DIRECT_BASE = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_DIRECT_BASE",
    _u(FIXTURE_LAN_HOST, 11434),
)
FIXTURE_OLLAMA_DIRECT_EMBEDDINGS = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_DIRECT_EMBEDDINGS",
    f"{FIXTURE_OLLAMA_DIRECT_BASE}/api/embeddings",
)
FIXTURE_OLLAMA_DIRECT_GENERATE = _get(
    "CLAUDE_HOOKS_TEST_OLLAMA_DIRECT_GENERATE",
    f"{FIXTURE_OLLAMA_DIRECT_BASE}/api/generate",
)


# --------------------------------------------------------------------- #
# Local llamafile embedder.
# --------------------------------------------------------------------- #
FIXTURE_LLAMAFILE_URL = _get(
    "CLAUDE_HOOKS_TEST_LLAMAFILE_URL",
    _u(FIXTURE_LAN_HOST, 38092, "/embedding"),
)


# --------------------------------------------------------------------- #
# API proxy.
# --------------------------------------------------------------------- #
FIXTURE_PROXY_URL = _get(
    "CLAUDE_HOOKS_TEST_PROXY_URL",
    _u(FIXTURE_LAN_HOST, 38080),
)


# --------------------------------------------------------------------- #
# Episodic-memory server.
# --------------------------------------------------------------------- #
FIXTURE_EPISODIC_URL = _get(
    "CLAUDE_HOOKS_TEST_EPISODIC_URL",
    _u(FIXTURE_LAN_HOST, 11435),
)


# --------------------------------------------------------------------- #
# Postgres DSNs. The "alt" DSN is for fixtures that need to show that
# a different port is in use (e.g. the consultants checkpointer).
# --------------------------------------------------------------------- #
FIXTURE_PG_DSN = _get(
    "CLAUDE_HOOKS_TEST_PG_DSN",
    f"postgresql://test:test@{FIXTURE_LAN_HOST}:5432/test",
)
FIXTURE_PG_DSN_ALT_PORT = _get(
    "CLAUDE_HOOKS_TEST_PG_DSN_ALT_PORT",
    f"postgresql://test:test@{FIXTURE_LAN_HOST}:5433/test",
)


# --------------------------------------------------------------------- #
# Fake-but-plausible IPs that appear inside test-input strings
# (e.g. transcripts whose contents are scanned for IP-extraction).
# These are NOT infrastructure — they're regex test fodder — but we
# still draw them from the documentation pool so the fact stays
# legible.
# --------------------------------------------------------------------- #
FIXTURE_REGEX_IP_PRIMARY = _get(
    "CLAUDE_HOOKS_TEST_REGEX_IP_PRIMARY", "192.0.2.25"
)
FIXTURE_REGEX_IP_SECONDARY = _get(
    "CLAUDE_HOOKS_TEST_REGEX_IP_SECONDARY", "198.51.100.5"
)


__all__ = [
    "FIXTURE_LAN_HOST",
    "FIXTURE_LAN_HOST_ALT",
    "FIXTURE_LAN_HOST_THIRD",
    "FIXTURE_OLLAMA_PROXY_BASE",
    "FIXTURE_OLLAMA_PROXY_GENERATE",
    "FIXTURE_OLLAMA_PROXY_TAGS",
    "FIXTURE_OLLAMA_DIRECT_BASE",
    "FIXTURE_OLLAMA_DIRECT_EMBEDDINGS",
    "FIXTURE_OLLAMA_DIRECT_GENERATE",
    "FIXTURE_LLAMAFILE_URL",
    "FIXTURE_PROXY_URL",
    "FIXTURE_EPISODIC_URL",
    "FIXTURE_PG_DSN",
    "FIXTURE_PG_DSN_ALT_PORT",
    "FIXTURE_REGEX_IP_PRIMARY",
    "FIXTURE_REGEX_IP_SECONDARY",
]
