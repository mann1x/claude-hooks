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
        out.append(
            LspServerSpec(
                extensions=tuple(s.lower().lstrip(".") for s in exts),
                command=tuple(str(c) for c in cmd),
                root_dir=str(entry.get("rootDir", ".")),
            )
        )
    return out


def resolve_server_for_path(
    path: str | os.PathLike,
    servers: list[LspServerSpec],
) -> Optional[LspServerSpec]:
    """Return the first server whose extensions list covers ``path``,
    or ``None`` when no server claims the file.
    """
    for srv in servers:
        if srv.matches(path):
            return srv
    return None


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
