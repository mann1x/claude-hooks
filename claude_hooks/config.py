"""
Config file loading + defaults.

The config lives at ``<repo>/config/claude-hooks.json`` (gitignored). The
schema is documented in ``config/claude-hooks.example.json``.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "providers": {
        "qdrant": {
            "enabled": True,
            "mcp_url": "",
            "headers": {},
            "collection": "memory",
            "recall_k": 5,
            "store_mode": "auto",        # auto | off
            "timeout": 5.0,
        },
        "memory_kg": {
            "enabled": True,
            "mcp_url": "",
            "headers": {},
            "recall_k": 5,
            "store_mode": "auto",        # auto | off
            "timeout": 5.0,
        },
        # --- Experimental scaffolds: disabled by default ---
        "pgvector": {
            "enabled": False,
            "dsn": "",                   # postgres://user:pass@host:5432/db
            "table": "claude_hooks_memory",
            "embedder": "ollama",        # see claude_hooks/embedders.py
            "embedder_options": {
                "url": "http://localhost:11434/api/embeddings",
                "model": "nomic-embed-text",
            },
            "recall_k": 5,
            "store_mode": "auto",
            "timeout": 10.0,
        },
        "sqlite_vec": {
            "enabled": False,
            "db_path": "~/.claude/claude-hooks-memory.db",
            "table": "memory",
            "embedder": "ollama",
            "embedder_options": {
                "url": "http://localhost:11434/api/embeddings",
                "model": "nomic-embed-text",
            },
            "recall_k": 5,
            "store_mode": "auto",
            "timeout": 10.0,
        },
    },
    "hooks": {
        "user_prompt_submit": {
            "enabled": True,
            "min_prompt_chars": 30,
            "include_providers": ["qdrant", "memory_kg"],
            "max_total_chars": 4000,
            # --- v0.2 features ---
            "hyde_enabled": False,
            "hyde_model": "gemma4:e2b",
            "hyde_fallback_model": "qwen3:4b",
            "hyde_url": "http://localhost:11434/api/generate",
            "hyde_timeout": 3.0,
            "hyde_max_tokens": 150,
            "progressive": False,
            "decay_enabled": False,
            "decay_file": "~/.claude/claude-hooks-decay.json",
            "decay_recency_halflife_days": 14,
            "decay_frequency_cap": 5,
        },
        "session_start": {
            "enabled": True,
            "show_status_line": True,
            "compact_recall": True,
            "compact_recall_query": "session context, key decisions, and important patterns",
        },
        "stop": {
            "enabled": True,
            "store_threshold": "noteworthy",  # noteworthy | always | off
            "classify_observations": True,
            "extract_instincts": False,
            "instincts_dir": "~/.claude/instincts",
        },
        "session_end": {
            "enabled": True,
        },
        "pre_tool_use": {
            "enabled": False,
            "warn_on_tools": ["Bash", "Edit", "Write"],
            "warn_on_patterns": ["rm ", "DROP TABLE", "git reset --hard"],
        },
    },
    "reflect": {
        "enabled": True,
        "max_memories_to_analyze": 50,
        "min_pattern_count": 3,
        "output_path": "~/.claude/CLAUDE.md",
        "ollama_model": "gemma4:e2b",
        "ollama_url": "http://localhost:11434/api/generate",
    },
    "consolidate": {
        "enabled": False,
        "trigger": "manual",              # manual | session_start
        "min_sessions_between_runs": 10,
        "state_file": "~/.claude/claude-hooks-consolidate.json",
        "max_memories_to_scan": 200,
        "merge_similarity_threshold": 0.80,
        "prune_stale_days": 90,
        "ollama_model": "gemma4:e2b",
        "ollama_url": "http://localhost:11434/api/generate",
    },
    "episodic": {
        "mode": "off",                  # off | server | client
        "server_url": "",               # client: URL of the episodic-server
        "server_port": 11435,           # server: port to listen on
        "binary": "episodic-memory",    # server: path to episodic-memory binary
        "timeout": 10.0,               # client: push timeout in seconds
    },
    "logging": {
        "path": "~/.claude/claude-hooks.log",
        "level": "info",
        "max_bytes": 2097152,   # 2 MB per file
        "backup_count": 3,      # keep 3 rotated files (.log.1, .log.2, .log.3)
    },
    "disable_marker_filename": ".claude-hooks-disable",
}


def repo_root() -> Path:
    """Path to the claude-hooks repo root (one level above this file's package)."""
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return repo_root() / "config" / "claude-hooks.json"


def load_config(path: Optional[Path] = None) -> dict:
    """
    Load the config file from disk and merge it on top of DEFAULT_CONFIG.
    Missing file → returns the defaults unchanged.
    """
    cfg_path = path or default_config_path()
    merged: dict[str, Any] = deepcopy(DEFAULT_CONFIG)
    if not cfg_path.exists():
        return merged
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return merged
    _deep_merge(merged, user_cfg)
    return merged


def save_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Atomically write the config to disk and return the path."""
    cfg_path = path or default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, cfg_path)
    return cfg_path


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursively merge ``src`` into ``dst`` in place."""
    for key, val in src.items():
        if key in dst and isinstance(dst[key], dict) and isinstance(val, dict):
            _deep_merge(dst[key], val)
        else:
            dst[key] = val


def expand_user_path(p: str) -> Path:
    """Expand ``~`` and environment variables in a path string."""
    return Path(os.path.expanduser(os.path.expandvars(p)))


def project_disabled(cwd: str, marker_filename: str) -> bool:
    """
    Check whether ``cwd`` (or any ancestor up to root) contains the disable
    marker file. Walks up so that ``project/sub/dir`` inherits ``project``'s
    opt-out.
    """
    p = Path(cwd or ".").resolve()
    while True:
        if (p / marker_filename).exists():
            return True
        if p.parent == p:
            return False
        p = p.parent
