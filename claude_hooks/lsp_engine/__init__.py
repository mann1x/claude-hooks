"""Session-scoped LSP engine.

See ``docs/PLAN-lsp-engine.md`` for the full design. As of Phase 1
the package ships:

- ``config`` — cclsp.json reader (canonical source for LSP commands +
  extensions) + ``.claude-hooks/lsp-engine.toml`` schema (engine knobs).
- ``lsp`` — per-language LSP child wrapper. Spawns the LSP, runs the
  ``initialize`` handshake, sends ``textDocument/didOpen`` /
  ``textDocument/didChange``, and surfaces ``publishDiagnostics``
  notifications back to the caller.
- ``engine`` — multi-LSP routing. Lazy-starts the matching LSP per
  file extension; routes did_open / did_change / get_diagnostics to
  the right child.
- ``locks`` — per-file ``SessionLockManager``: implements Decision 5
  (session-affinity locks) so two Claude Code sessions on the same
  daemon don't clobber each other's edits to the same file.
- ``ipc`` — UNIX-socket newline-delimited JSON server + client.
- ``daemon`` — long-lived per-project process tying it all together.
- ``client`` — hook-side ``LspEngineClient`` + ``connect_or_spawn``.

CLI entry point: ``python -m claude_hooks.lsp_engine``
(``daemon`` / ``status`` subcommands).
"""

from claude_hooks.lsp_engine.client import (
    LspEngineClient,
    connect_or_spawn,
    daemon_pid,
)
from claude_hooks.lsp_engine.config import (
    EngineConfig,
    LspServerSpec,
    load_cclsp_config,
    load_engine_config,
    resolve_server_for_path,
)
from claude_hooks.lsp_engine.daemon import (
    Daemon,
    DaemonAlreadyRunning,
    load_daemon_config,
    project_dir,
    socket_path_for,
)
from claude_hooks.lsp_engine.engine import Engine
from claude_hooks.lsp_engine.ipc import (
    IpcClient,
    IpcProtocolError,
    IpcServer,
)
from claude_hooks.lsp_engine.locks import (
    QueuedChange,
    SessionLockManager,
)
from claude_hooks.lsp_engine.lsp import (
    Diagnostic,
    LspClient,
    LspError,
    LspProtocolError,
)

__all__ = [
    "Daemon",
    "DaemonAlreadyRunning",
    "Diagnostic",
    "Engine",
    "EngineConfig",
    "IpcClient",
    "IpcProtocolError",
    "IpcServer",
    "LspClient",
    "LspEngineClient",
    "LspError",
    "LspProtocolError",
    "LspServerSpec",
    "QueuedChange",
    "SessionLockManager",
    "connect_or_spawn",
    "daemon_pid",
    "load_cclsp_config",
    "load_daemon_config",
    "load_engine_config",
    "project_dir",
    "resolve_server_for_path",
    "socket_path_for",
]
