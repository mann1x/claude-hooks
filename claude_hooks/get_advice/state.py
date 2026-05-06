"""Per-session state for /get-advice conversations.

A "session" is one continuous chat with the advisor model. Claude can
run multiple sessions per /get-advice invocation (effort budget). Each
session lives under ``~/.cache/claude-advisor/<sid>/state.json``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


def cache_root() -> Path:
    base = os.environ.get("CLAUDE_ADVISOR_CACHE_DIR", "").strip()
    if base:
        return Path(base)
    return Path.home() / ".cache" / "claude-advisor"


@dataclass
class SessionState:
    sid: str
    model: str
    ctx_max: Optional[int]
    reset_threshold: float
    messages: list[dict] = field(default_factory=list)
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turns: int = 0

    def state_path(self, root: Optional[Path] = None) -> Path:
        return (root or cache_root()) / self.sid / "state.json"

    def save(self, root: Optional[Path] = None) -> None:
        self.updated_at = time.time()
        p = self.state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n")

    def ctx_used_pct(self) -> Optional[float]:
        """Fraction of ctx_max used by the most recent prompt evaluation.

        We use ``last_prompt_tokens`` (not the cumulative sum) because
        Ollama's ``prompt_eval_count`` already reflects the full
        message-history token count for that turn — exactly the figure
        the model is going to evaluate against its context window."""
        if not self.ctx_max or self.ctx_max <= 0:
            return None
        return self.last_prompt_tokens / self.ctx_max

    def reset_recommended(self) -> bool:
        pct = self.ctx_used_pct()
        if pct is None:
            return False
        return pct >= self.reset_threshold


def load_session(sid: str, root: Optional[Path] = None) -> Optional[SessionState]:
    p = (root or cache_root()) / sid / "state.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    # Best-effort defensive load: ignore unknown fields, fill required.
    return SessionState(
        sid=raw.get("sid", sid),
        model=raw.get("model", ""),
        ctx_max=raw.get("ctx_max"),
        reset_threshold=float(raw.get("reset_threshold", 0.85)),
        messages=list(raw.get("messages") or []),
        cumulative_prompt_tokens=int(raw.get("cumulative_prompt_tokens", 0)),
        cumulative_completion_tokens=int(raw.get("cumulative_completion_tokens", 0)),
        last_prompt_tokens=int(raw.get("last_prompt_tokens", 0)),
        last_completion_tokens=int(raw.get("last_completion_tokens", 0)),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
        turns=int(raw.get("turns", 0)),
    )


def cleanup_old_sessions(root: Optional[Path] = None,
                         max_age_seconds: int = 86400) -> int:
    """Remove session dirs older than max_age. Returns count removed."""
    r = root or cache_root()
    if not r.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for child in r.iterdir():
        if not child.is_dir():
            continue
        sf = child / "state.json"
        try:
            mtime = sf.stat().st_mtime if sf.exists() else child.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                _rmtree(child)
                removed += 1
            except OSError:
                pass
    return removed


def _rmtree(p: Path) -> None:
    if p.is_file() or p.is_symlink():
        p.unlink()
        return
    for child in p.iterdir():
        _rmtree(child)
    p.rmdir()
