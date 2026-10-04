"""Read the end of a transcript, never the whole of it.

Every per-turn consumer — the Stop hook's summary, noteworthiness,
stop_guard, bug-fix detection, the task nudge — looks at the last turn:
the records after the last real user prompt. They used to load the
whole file to find it. Transcripts of long-lived sessions reach
gigabytes (opencoti 5.2 GB, backup_models 4.5 GB on solidpc), so every
Stop parsed gigabytes, and the daemon that serves the hooks sat at a
20.9 GB high-water mark: Python frees what it parsed, but glibc keeps
the pages, so the process stays as large as its largest turn.
``malloc_trim(0)`` took it to 312 MB.

:func:`read_tail` reads backwards in growing windows until ``stop_at``
matches a record — the turn boundary — or the file starts, or ``cap``
bytes have been read. A turn larger than the cap comes back cut at the
front, which every caller already tolerates: none of them needs more
than the end of the turn.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

#: The first window. Most turns fit; the boundary is usually in it.
START_BYTES = 4 * 1024 * 1024
#: The most read for one turn, however long it was.
CAP_BYTES = 128 * 1024 * 1024


def _parse(raw: bytes, cut_first: bool) -> list[dict]:
    lines = raw.split(b"\n")
    if cut_first:
        lines = lines[1:]           # the window starts mid-record
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def read_tail(path: str, *, stop_at: Optional[Callable[[dict], bool]] = None,
              start: int = START_BYTES, cap: int = CAP_BYTES
              ) -> Optional[list[dict]]:
    """The trailing records of the JSONL file at ``path``, oldest first.

    Without ``stop_at`` it returns the records in the first window.
    None when the file cannot be read.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            window = max(1, start)
            while True:
                begin = max(0, size - window)
                fh.seek(begin)
                rows = _parse(fh.read(size - begin), cut_first=begin > 0)
                if (begin == 0 or window >= cap or stop_at is None
                        or any(stop_at(r) for r in rows)):
                    return rows
                window = min(window * 4, cap)
    except OSError:
        return None
