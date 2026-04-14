"""
Rolling rate-limit state file.

Updated on every response that carries ``anthropic-ratelimit-unified-*``
headers. Downstream consumers (``scripts/weekly_token_usage.py``, a
statusline script, …) can read this file to get the authoritative
weekly-limit percentage without having to ask Claude Code's UI.

File path (default): ``<proxy log_dir>/ratelimit-state.json``
Write model: atomic replace (write tmp → os.replace). One writer
expected; concurrent writers race but the file always ends up well-
formed because each write is atomic.

Shape:

    {
      "last_updated": "2026-04-14T15:00:00Z",
      "five_hour_utilization": 0.42,      # normalised 0..1
      "seven_day_utilization": 0.18,
      "five_hour_remaining": 0.58,
      "seven_day_remaining": 0.82,
      "representative_claim": "five_hour",
      "source_request_ts": "2026-04-14T14:59:58.123Z",
      "raw_headers": { ... verbatim rate_limit block from the proxy ... }
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("claude_hooks.proxy.ratelimit_state")

_write_lock = threading.Lock()


def _parse_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update_state_file(
    state_path: Path,
    *,
    rate_limit_headers: Optional[dict[str, str]],
    request_ts: str,
) -> None:
    """Atomically write the state file from a fresh rate-limit header set.

    No-op when ``rate_limit_headers`` is None or contains no useful
    numeric fields.
    """
    if not rate_limit_headers:
        return

    h = {k.lower(): v for k, v in rate_limit_headers.items()}

    # Anthropic's documented subscription headers. Accept a few naming
    # variants because the exact keys have changed between API versions.
    five_util = (
        _parse_float(h.get("anthropic-ratelimit-unified-5h-utilization"))
        or _parse_float(h.get("anthropic-ratelimit-unified-fiveh-utilization"))
    )
    seven_util = (
        _parse_float(h.get("anthropic-ratelimit-unified-7d-utilization"))
        or _parse_float(h.get("anthropic-ratelimit-unified-sevend-utilization"))
    )
    claim = h.get("anthropic-ratelimit-unified-representative-claim")

    if five_util is None and seven_util is None:
        # Nothing we recognise — skip writing so we don't pollute a good
        # state with a partial one.
        return

    import datetime as _dt
    now_iso = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{_dt.datetime.utcnow().microsecond // 1000:03d}Z"

    state = {
        "last_updated": now_iso,
        "source_request_ts": request_ts,
        "representative_claim": claim,
        "raw_headers": rate_limit_headers,
    }
    if five_util is not None:
        state["five_hour_utilization"] = five_util
        state["five_hour_remaining"] = max(0.0, 1.0 - five_util)
    if seven_util is not None:
        state["seven_day_utilization"] = seven_util
        state["seven_day_remaining"] = max(0.0, 1.0 - seven_util)

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        with _write_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, state_path)
    except OSError as e:
        log.debug("rate-limit state write failed: %s", e)


def read_state_file(state_path: Path) -> Optional[dict]:
    """Read the latest state file. Returns None on any I/O or parse error."""
    if not state_path.exists():
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
