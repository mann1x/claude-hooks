"""One council's shared cache of read-only tool results.

Fan-out lanes research the same repository in parallel and, left alone,
read the same files: the planner's items overlap, and every lane starts
with the same orientation calls (``survey_project``, ``list_files`` on
the root, the README). Each of those is paid for once per lane on the
tool side and, worse, once per lane in wall time while the lanes wait
on the same disk.

The cache sits between the graph and the session's executor, so it is
shared by every lane of one run and nothing else — a follow-up gets a
new one, because files may have changed between the two. Within a run:

* only the filesystem tools are cached. ``recall_memory`` is read-only
  too, but the council writes findings into that store as it goes, so a
  cached recall would hide a sibling lane's result;
* ``read_file`` is keyed on the target's size and mtime as well as its
  arguments, so an edit during the run is a miss rather than stale text;
* any call to a tool not known to be read-only clears the cache — it
  may have changed what the cached reads describe;
* results that report an error are not kept, so a transient failure
  (a grep timeout) is retried by the next caller rather than replayed;
* concurrent calls with the same key run the tool once; the others wait
  for that result instead of racing it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

#: Tools whose output depends only on the files under the session roots.
CACHEABLE_TOOLS: frozenset[str] = frozenset(
    {"survey_project", "list_files", "read_file", "glob", "grep"})

#: Bound on retained result text. A council reads well under this; the
#: bound exists so a pathological run cannot grow the engine unboundedly.
MAX_CACHED_BYTES: int = 32 * 1024 * 1024

#: Read-only but not cached; calling them leaves the cache alone.
READ_ONLY_UNCACHED: frozenset[str] = frozenset({"recall_memory"})

Executor = Callable[..., str]


def _is_error(output: str) -> bool:
    head = (output or "").lstrip()[:16].lower()
    return head.startswith("error") or head.startswith("[error")


class SharedReadCache:
    """Wrap ``executor`` (``(name, raw_args, cwd) -> str``) with a
    per-run cache of :data:`CACHEABLE_TOOLS` results."""

    def __init__(self, executor: Executor, *,
                 cacheable: frozenset[str] = CACHEABLE_TOOLS,
                 read_only: frozenset[str] = frozenset(),
                 max_bytes: int = MAX_CACHED_BYTES):
        self._executor = executor
        self._cacheable = cacheable
        self._read_only = frozenset(read_only) | READ_ONLY_UNCACHED | cacheable
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._results: dict[tuple, str] = {}
        self._inflight: dict[tuple, threading.Event] = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ #
    def _key(self, name: str, raw_args: str, cwd: str) -> Optional[tuple]:
        try:
            args = json.loads(raw_args) if raw_args else {}
        except (TypeError, ValueError):
            return None       # the tool will report the bad JSON itself
        if not isinstance(args, dict):
            return None
        norm = json.dumps(args, sort_keys=True, separators=(",", ":"))
        stamp = None
        if name == "read_file":
            path = args.get("path")
            if not isinstance(path, str) or not path:
                return None
            try:
                st = os.stat(os.path.join(cwd or "", path))
                stamp = (st.st_size, st.st_mtime_ns)
            except OSError:
                stamp = "missing"
        return (name, norm, cwd, stamp)

    def clear(self) -> None:
        with self._lock:
            self._results.clear()
            self._bytes = 0

    # ------------------------------------------------------------------ #
    def __call__(self, name: str, raw_args: str, cwd: str, **kw) -> str:
        if name not in self._cacheable:
            if name not in self._read_only:
                # Possibly effectful: whatever it did, the cached reads
                # may no longer describe the tree.
                self.clear()
            return self._executor(name, raw_args, cwd, **kw)
        key = self._key(name, raw_args, cwd)
        if key is None:
            return self._executor(name, raw_args, cwd, **kw)

        while True:
            with self._lock:
                if key in self._results:
                    self.hits += 1
                    return self._results[key]
                waiter = self._inflight.get(key)
                if waiter is None:
                    mine = self._inflight[key] = threading.Event()
                    break
            # Another lane is running this exact call; take its result.
            waiter.wait()

        self.misses += 1
        output = None
        try:
            output = self._executor(name, raw_args, cwd, **kw)
            return output
        finally:
            # Store before waking the waiters: woken first, they would
            # find neither a result nor a runner and all run it again.
            with self._lock:
                if isinstance(output, str) and not _is_error(output) \
                        and self._bytes + len(output) <= self._max_bytes:
                    self._results[key] = output
                    self._bytes += len(output)
                self._inflight.pop(key, None)
            mine.set()


__all__ = ["CACHEABLE_TOOLS", "READ_ONLY_UNCACHED", "SharedReadCache"]
