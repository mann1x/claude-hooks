"""Expiry that keeps history, and a cap that cannot fill a disk.

Expiry does not delete: expired rows are appended to a quarterly
``.jsonl.zst`` first and removed only once that write has returned. Same
write-the-durable-copy-first ordering the consultants distillation
reaper already uses, for the same reason — the failure mode of the other
order is silent data loss that looks like successful housekeeping.

The 10 GB cap is not a retention policy. Messages are prose, they repeat
their own headers, and they compress to a few percent; at the size these
actually run the cap is years of traffic. It exists so an unattended
host cannot fill its disk with its own mail, and every drop is logged
with the file and the span it covered, because silently deleting history
is the failure it is meant to prevent.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("claude_hooks.mailbox.archive")

DEFAULT_CAP_BYTES = 10 * 1024 * 1024 * 1024      # 10 GB
_SUFFIX = ".jsonl.zst"


def archive_dir() -> Path:
    return Path(os.environ.get("CLAUDE_HOOKS_MAILBOX_ARCHIVE")
                or Path.home() / ".claude" / "mailbox-archive")


def quarter_of(ts) -> str:
    dt = _parse(ts) or datetime.now(timezone.utc)
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def _parse(ts) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _encode(rows: Sequence[dict]) -> bytes:
    payload = "\n".join(
        json.dumps(r, default=str, ensure_ascii=False) for r in rows
    ).encode("utf-8") + b"\n"
    try:
        import zstandard
    except ImportError:
        # Uncompressed rather than unwritten. The archive exists so the
        # rows can be deleted; refusing to write because a compressor is
        # missing would either block expiry forever or — worse — let the
        # caller delete without a durable copy.
        log.info("zstandard not installed; archiving uncompressed")
        return payload
    return zstandard.ZstdCompressor(level=19).compress(payload)


def _decode(blob: bytes) -> list[dict]:
    if blob[:4] == b"\x28\xb5\x2f\xfd":          # zstd magic
        import zstandard
        blob = zstandard.ZstdDecompressor().decompress(
            blob, max_output_size=1 << 30)
    out = []
    for line in blob.decode("utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def write(rows: Sequence[dict], *, directory: Optional[Path] = None) -> list[Path]:
    """Append rows to their quarter's file. Returns the files touched.

    Append means read-modify-write here, because a zstd frame cannot be
    extended in place. Quarterly files keep that bounded: a quarter's
    worth of messages is small, and the current quarter is the only file
    ever rewritten.
    """
    if not rows:
        return []
    d = directory or archive_dir()
    d.mkdir(parents=True, exist_ok=True)

    by_quarter: dict[str, list[dict]] = {}
    for r in rows:
        by_quarter.setdefault(quarter_of(r.get("created_at")), []).append(r)

    touched: list[Path] = []
    for quarter, batch in sorted(by_quarter.items()):
        path = d / f"{quarter}{_SUFFIX}"
        existing: list[dict] = []
        if path.is_file():
            try:
                existing = _decode(path.read_bytes())
            except Exception:
                # A corrupt archive must not become a reason to lose the
                # rows we are about to add. Keep the old file aside and
                # start a fresh one rather than overwriting it.
                salvage = path.with_suffix(path.suffix + ".corrupt")
                log.warning("archive %s unreadable; kept as %s",
                            path.name, salvage.name)
                try:
                    path.replace(salvage)
                except OSError:
                    log.exception("could not set aside %s", path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(_encode(existing + list(batch)))
        os.replace(tmp, path)
        touched.append(path)
        log.info("archived %d message(s) to %s", len(batch), path.name)
    return touched


def enforce_cap(*, directory: Optional[Path] = None,
                cap_bytes: int = DEFAULT_CAP_BYTES) -> list[str]:
    """Drop whole quarters, oldest first, once the cap is exceeded.

    Only once *actually* exceeded — a cap that trims early would delete
    history nobody asked it to.
    """
    d = directory or archive_dir()
    if not d.is_dir():
        return []
    files = sorted(d.glob(f"*{_SUFFIX}"))
    total = sum(f.stat().st_size for f in files)
    dropped: list[str] = []
    for f in files:                       # sorted -> oldest quarter first
        if total <= cap_bytes:
            break
        size = f.stat().st_size
        try:
            f.unlink()
        except OSError:
            log.exception("could not drop %s", f)
            continue
        total -= size
        dropped.append(f.name)
        log.warning("mailbox archive over %d bytes — dropped %s (%d bytes, "
                    "covering %s)", cap_bytes, f.name, size,
                    f.name[:-len(_SUFFIX)])
    return dropped


def sweep(store, *, directory: Optional[Path] = None,
          cap_bytes: int = DEFAULT_CAP_BYTES,
          registry_days: int = 30, limit: int = 1000,
          evict_hours: Optional[float] = None) -> dict:
    """Archive expired mail, delete it, then trim the archive.

    The order is the whole point: nothing is deleted from the table
    until its durable copy is on disk.
    """
    expired = store.expired(limit=limit)
    archived = write(expired, directory=directory) if expired else []
    deleted = 0
    if expired and archived:
        deleted = store.delete([r["id"] for r in expired])
    elif expired:
        log.error("mailbox: %d expired message(s) NOT deleted — the "
                  "archive write produced no file", len(expired))
    dropped = enforce_cap(directory=directory, cap_bytes=cap_bytes)
    forgotten = store.sweep_registry(days=registry_days)
    # Thirty days is the right horizon for archiving mail and the wrong
    # one for addressing: ten sessions that ran and ended inside ten
    # minutes left ten rows that a send fanned out over, and they would
    # have sat there for a month. Evicting on hours is what keeps the
    # registry a list of live sessions.
    try:
        from claude_hooks.mailbox.store import DEFAULT_EVICT_HOURS
        evicted = store.evict_stale(
            hours=DEFAULT_EVICT_HOURS if evict_hours is None else evict_hours)
    except Exception:  # pragma: no cover — older store, or unreachable
        log.debug("mailbox: stale eviction failed", exc_info=True)
        evicted = 0
    # Repair, not routine: ``send()`` can no longer fan out over
    # registrations, but the rows it already wrote are in every mailbox
    # that ran the old code, and an upgrade that fixes the cause without
    # clearing the effect leaves the recipient re-reading the same
    # message eleven times. Idempotent, so it costs one scan once the
    # duplicates are gone.
    try:
        deduped = store.dedupe_messages()
    except Exception:  # pragma: no cover — older store, or unreachable
        log.debug("mailbox: duplicate repair failed", exc_info=True)
        deduped = {"removed": 0, "groups_cleared": 0}
    return {"expired": len(expired), "archived": [p.name for p in archived],
            "deleted": deleted, "dropped": dropped,
            "sessions_forgotten": forgotten, "sessions_evicted": evicted,
            "duplicates_removed": deduped.get("removed", 0),
            "broadcast_groups_cleared": deduped.get("groups_cleared", 0)}
