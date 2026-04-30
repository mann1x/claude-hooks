"""Session-scoped LSP engine — Phase 0.

See ``docs/PLAN-lsp-engine.md`` for the full design. Phase 0 ships
the foundations:

- ``config`` — cclsp.json reader (canonical source for LSP commands +
  extensions) + ``.claude-hooks/lsp-engine.json`` schema (engine knobs).
- ``lsp`` — per-language LSP child wrapper. Spawns the LSP, runs the
  ``initialize`` handshake, sends ``textDocument/didOpen`` /
  ``textDocument/didChange``, and surfaces ``publishDiagnostics``
  notifications back to the caller.

No daemon, no IPC, no preload — those land in Phase 1+.
"""

from claude_hooks.lsp_engine.config import (
    EngineConfig,
    LspServerSpec,
    load_cclsp_config,
    load_engine_config,
    resolve_server_for_path,
)
from claude_hooks.lsp_engine.lsp import (
    Diagnostic,
    LspClient,
    LspError,
    LspProtocolError,
)

__all__ = [
    "Diagnostic",
    "EngineConfig",
    "LspClient",
    "LspError",
    "LspProtocolError",
    "LspServerSpec",
    "load_cclsp_config",
    "load_engine_config",
    "resolve_server_for_path",
]
