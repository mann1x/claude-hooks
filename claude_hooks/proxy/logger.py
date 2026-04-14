"""
JSONL logger for the proxy.

One line per upstream request. File rotated daily
(``YYYY-MM-DD.jsonl``). Old files pruned by ``retention_days`` on
startup and once an hour thereafter.

Log line shape (P0):

    {
      "ts": "2026-04-14T13:55:10.123Z",
      "method": "POST",
      "path": "/v1/messages",
      "query": "",
      "status": 200,
      "duration_ms": 2341,
      "upstream_host": "api.anthropic.com",
      "req_bytes": 12345,
      "resp_bytes": 67890,
      "model_requested": "claude-opus-4-6",
      "model_delivered": "claude-opus-4-6",
      "usage": {"input_tokens": 12, "output_tokens": 34, ...} | null,
      "rate_limit": {"five_hour_utilization": 0.65, ...} | null,
      "is_warmup": false,
      "synthetic": false,
      "agent_id": "..." | null,
      "session_id": "..." | null
    }

Files are append-only and safe for concurrent writers (one line per
``write()`` using O_APPEND).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("claude_hooks.proxy.logger")

_ROTATION_CHECK_INTERVAL_SEC = 3600  # one hour


class JsonlLogger:
    """Daily-rotated JSONL writer with retention pruning."""

    def __init__(self, log_dir: Path, retention_days: int = 14):
        self.log_dir = log_dir
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self._last_pruned_at = 0.0
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_prune(force=True)

    # ------------------------------------------------------------ #
    def _today_path(self) -> Path:
        return self.log_dir / f"{_dt.datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"

    def write(self, record: dict[str, Any]) -> None:
        """Append one JSON record. Never raises."""
        try:
            # Ensure a timestamp.
            record.setdefault(
                "ts", _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") +
                f"{_dt.datetime.utcnow().microsecond // 1000:03d}Z"
            )
            line = json.dumps(record, ensure_ascii=False) + "\n"
            path = self._today_path()
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            self._maybe_prune()
        except Exception as e:  # pragma: no cover
            log.debug("proxy log write failed: %s", e)

    # ------------------------------------------------------------ #
    def _maybe_prune(self, *, force: bool = False) -> None:
        import time
        now = time.time()
        if not force and (now - self._last_pruned_at) < _ROTATION_CHECK_INTERVAL_SEC:
            return
        self._last_pruned_at = now
        try:
            cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=self.retention_days)
            for p in self.log_dir.glob("*.jsonl"):
                try:
                    mt = _dt.datetime.utcfromtimestamp(p.stat().st_mtime)
                    if mt < cutoff:
                        p.unlink()
                except OSError:
                    continue
        except OSError as e:  # pragma: no cover
            log.debug("proxy log prune failed: %s", e)
