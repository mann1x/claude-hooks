"""
Local HTTP proxy in front of api.anthropic.com.

Phase P0 (this file set): pass-through forwarder that logs request +
response metadata (model, token usage, rate-limit headers, timing) to
JSONL. Lets you point Claude Code at it via ``ANTHROPIC_BASE_URL`` and
get the observability hooks can't reach — Warmup traffic, real weekly-
limit %, synthetic-RL detection.

See ``docs/PLAN-proxy-hook.md`` for the full P0..P4 roadmap.

Design constraints:

- stdlib only (``http.server`` + ``http.client`` + ``ssl``)
- default **off** in config; must be explicitly enabled
- local-only listener by default (127.0.0.1)
- streaming-safe: forward SSE responses chunk-by-chunk so extended
  thinking completes without buffering the whole body
- parses metadata from a **copy** of the first SSE event only, never
  modifies the bytes going back to Claude Code (pure passthrough)
"""

from claude_hooks.proxy.server import run, build_server

__all__ = ["run", "build_server"]
