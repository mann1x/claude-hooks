"""Config loaders for the LSP engine.

Two files, layered:

1. ``cclsp.json`` — canonical source for LSP commands and extensions.
   The same file ``cclsp`` (the multi-LSP MCP wrapper) reads, so a user
   who already has cclsp configured gets the engine for free. We never
   write to this file; only read. Stays JSON because that's cclsp's
   format upstream.

2. ``.claude-hooks/lsp-engine.toml`` (per-project, optional) or the
   ``hooks.lsp_engine`` block in ``config/claude-hooks.json`` (global,
   optional) — engine-specific knobs that have no place in cclsp.json:
   preload size, compile commands, debounce intervals, opt-in flags.
   TOML for the per-project file because users hand-edit it and the
   ``# reason: ...`` comment affordance is the whole point — JSON has
   no comment syntax, and a config that drifts from its rationale
   rots fast.

Resolution rule for engine knobs: per-project overrides global; missing
keys fall back to the dataclass defaults below. Both files are
optional; an absent project file with no global block produces a fully
default ``EngineConfig`` and the engine still runs.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — exercised only on 3.9 / 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as _e:
        raise ImportError(
            "claude_hooks.lsp_engine requires `tomli` on Python <3.11 "
            "(pip install tomli). 3.11+ has tomllib in the stdlib."
        ) from _e


@dataclass(frozen=True)
class LspServerSpec:
    """One language-server entry from cclsp.json.

    ``extensions`` are stored lowercased without leading dots so a
    lookup against ``Path(...).suffix`` is a straight string match.
    """

    extensions: tuple[str, ...]
    command: tuple[str, ...]
    root_dir: str = "."
    #: Passed through to ``initialize``. Several servers are inert
    #: without it — pylsp's plugin set, jdtls's runtime list,
    #: rust-analyzer's cargo settings all arrive this way and have no
    #: other channel. cclsp reads this key, so a config written for
    #: cclsp carries it; ignoring it would mean the field is present,
    #: documented, and does nothing.
    initialization_options: Optional[dict] = None
    #: Minutes between forced restarts, cclsp's workaround for servers
    #: that leak (it ships ``restartInterval: 5`` for pylsp). 0 = never.
    restart_interval_minutes: float = 0.0
    #: Seconds to wait for this server's *first* diagnostics publish.
    #: None means "use the built-in floor for this binary, raised by
    #: whatever we measure". Present because the built-in floors are
    #: guesses about a machine, and the operator watching a 20-minute
    #: cold index knows better than the table does.
    diagnostics_timeout: Optional[float] = None

    def matches(self, path: str | os.PathLike) -> bool:
        suffix = Path(path).suffix.lower().lstrip(".")
        return suffix in self.extensions


@dataclass(frozen=True)
class PreloadConfig:
    max_files: int = 200
    use_code_graph: bool = True


@dataclass(frozen=True)
class CompileAwareConfig:
    enabled: bool = False
    commands: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionLockConfig:
    debounce_seconds: float = 30.0
    query_timeout_ms: int = 500


@dataclass(frozen=True)
class MemoryConfig:
    max_files_per_lsp: int = 500


@dataclass(frozen=True)
class EngineConfig:
    preload: PreloadConfig = field(default_factory=PreloadConfig)
    compile_aware: CompileAwareConfig = field(default_factory=CompileAwareConfig)
    session_locks: SessionLockConfig = field(default_factory=SessionLockConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


class CclspConfigError(ValueError):
    """cclsp.json present but malformed."""


def load_cclsp_config(path: str | os.PathLike) -> list[LspServerSpec]:
    """Read ``cclsp.json`` and return the list of declared servers.

    Returns ``[]`` if the file does not exist — callers can treat that
    as "no LSPs configured" and degrade gracefully. Raises
    ``CclspConfigError`` on a present-but-broken file so the user gets
    a clear signal to fix it rather than the engine silently doing
    nothing.
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CclspConfigError(f"{p}: invalid JSON: {e}") from e

    servers_raw = data.get("servers")
    if not isinstance(servers_raw, list):
        raise CclspConfigError(
            f"{p}: top-level 'servers' must be an array",
        )

    out: list[LspServerSpec] = []
    for i, entry in enumerate(servers_raw):
        if not isinstance(entry, dict):
            raise CclspConfigError(
                f"{p}: servers[{i}] must be an object",
            )
        exts = entry.get("extensions")
        cmd = entry.get("command")
        if not isinstance(exts, list) or not exts:
            raise CclspConfigError(
                f"{p}: servers[{i}].extensions must be a non-empty array",
            )
        if not isinstance(cmd, list) or not cmd:
            raise CclspConfigError(
                f"{p}: servers[{i}].command must be a non-empty array",
            )
        init_opts = entry.get("initializationOptions")
        if init_opts is not None and not isinstance(init_opts, dict):
            raise CclspConfigError(
                f"{p}: servers[{i}].initializationOptions must be an object",
            )
        interval = entry.get("restartInterval", 0)
        try:
            interval = float(interval or 0)
        except (TypeError, ValueError):
            raise CclspConfigError(
                f"{p}: servers[{i}].restartInterval must be a number of "
                f"minutes",
            ) from None
        diag_timeout = entry.get("diagnosticsTimeout")
        if diag_timeout is not None:
            try:
                diag_timeout = float(diag_timeout)
            except (TypeError, ValueError):
                raise CclspConfigError(
                    f"{p}: servers[{i}].diagnosticsTimeout must be a number "
                    f"of seconds",
                ) from None
            if diag_timeout <= 0:
                raise CclspConfigError(
                    f"{p}: servers[{i}].diagnosticsTimeout must be positive "
                    f"— 0 would make every file report clean instantly",
                )
        out.append(
            LspServerSpec(
                extensions=tuple(s.lower().lstrip(".") for s in exts),
                command=tuple(str(c) for c in cmd),
                root_dir=str(entry.get("rootDir", ".")),
                initialization_options=init_opts,
                restart_interval_minutes=max(0.0, interval),
                diagnostics_timeout=diag_timeout,
            )
        )
    return out


def resolve_servers_for_path(
    path: str | os.PathLike,
    servers: list[LspServerSpec],
) -> list[LspServerSpec]:
    """Every server whose extensions cover ``path``, in config order.

    A list rather than a single winner, because one file is not one
    language. An ``.html`` document carries JavaScript and CSS; a
    ``.vue`` or ``.svelte`` file carries all three; ``.md`` carries
    whatever its fences say. Returning the first match made those
    documents the property of whichever server happened to be listed
    first, and silently discarded the rest.

    Extensions remain the declaration of *what is supported* — they
    are how an operator reads the config and how
    :meth:`Engine.configured_extensions` decides what to preload. What
    changes is that the declaration is no longer exclusive.
    """
    return [srv for srv in servers if srv.matches(path)]


def resolve_server_for_path(
    path: str | os.PathLike,
    servers: list[LspServerSpec],
) -> Optional[LspServerSpec]:
    """First server claiming ``path``, or ``None``.

    Retained for callers that genuinely want one server (and for the
    published API surface). New code should prefer
    :func:`resolve_servers_for_path`.
    """
    matches = resolve_servers_for_path(path, servers)
    return matches[0] if matches else None


def load_engine_config(
    project_path: Optional[str | os.PathLike] = None,
    global_block: Optional[dict] = None,
) -> EngineConfig:
    """Load engine knobs from per-project + global config, layered.

    ``project_path`` points at ``.claude-hooks/lsp-engine.toml`` (the
    full path, not the project root). Missing file is fine.
    ``global_block`` is the ``hooks.lsp_engine`` dict from the main
    ``config/claude-hooks.json``; pass ``None`` to skip it.

    Per-project keys override global keys; both override defaults.
    """
    merged: dict = {}
    if global_block:
        merged = _deep_merge(merged, global_block)
    if project_path:
        p = Path(project_path)
        if p.is_file():
            try:
                merged = _deep_merge(merged, tomllib.loads(p.read_text(encoding="utf-8")))
            except tomllib.TOMLDecodeError as e:
                raise CclspConfigError(f"{p}: invalid TOML: {e}") from e

    return EngineConfig(
        preload=PreloadConfig(
            max_files=int((merged.get("preload") or {}).get("max_files", 200)),
            use_code_graph=bool(
                (merged.get("preload") or {}).get("use_code_graph", True)
            ),
        ),
        compile_aware=CompileAwareConfig(
            enabled=bool((merged.get("compile_aware") or {}).get("enabled", False)),
            commands={
                k: tuple(v)
                for k, v in (
                    (merged.get("compile_aware") or {}).get("commands") or {}
                ).items()
            },
        ),
        session_locks=SessionLockConfig(
            debounce_seconds=float(
                (merged.get("session_locks") or {}).get("debounce_seconds", 30.0)
            ),
            query_timeout_ms=int(
                (merged.get("session_locks") or {}).get("query_timeout_ms", 500)
            ),
        ),
        memory=MemoryConfig(
            max_files_per_lsp=int(
                (merged.get("memory") or {}).get("max_files_per_lsp", 500)
            ),
        ),
    )


def _deep_merge(a: dict, b: dict) -> dict:
    """Recursive dict merge — values in ``b`` win, except where both
    are dicts (recurse). Lists in ``b`` replace lists in ``a``.
    """
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ─── where cclsp.json lives ──────────────────────────────────────────


def candidate_cclsp_paths(project_root: str | os.PathLike) -> list[Path]:
    """Every place a project's server list may live, best first.

    There is exactly one of these functions on purpose. The MCP server
    and the daemon used to resolve this independently and in opposite
    orders — the MCP preferring ``<root>/cclsp.json`` and the daemon
    preferring ``$CCLSP_CONFIG_PATH`` — so the MCP could validate one
    file while the daemon served another. Nothing failed: the two files
    happened to list the same servers, which is precisely how that kind
    of disagreement waits to bite.

    The project-local file wins because it is the more specific
    statement, and because it is the one ``sync_cclsp.py`` reconciles
    against the servers actually installed. ``$CCLSP_CONFIG_PATH`` is
    how cclsp itself was configured and stays as the shared fallback.
    """
    root = Path(project_root).resolve()
    out = [root / "cclsp.json"]
    env = os.environ.get("CCLSP_CONFIG_PATH")
    if env:
        out.append(Path(env).expanduser())
    out.append(Path.home() / ".config" / "cclsp" / "cclsp.json")
    return out


def resolve_cclsp_path(
    project_root: str | os.PathLike,
) -> Optional[Path]:
    """The first candidate that exists, or None."""
    for candidate in candidate_cclsp_paths(project_root):
        try:
            if candidate.is_file():
                return candidate
        except OSError:      # pragma: no cover — unreadable parent
            continue
    return None
