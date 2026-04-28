"""Stdlib-only parallelism helpers for fan-out across providers.

Hooks are I/O-bound (HTTP to MCP servers, Ollama, etc.). The GIL releases
during socket reads, so a small ThreadPoolExecutor gives us near-linear
speedup across N providers without the rewrite cost of asyncio.

The two helpers here cover the common shapes:

- ``parallel_map(fn, items, ...)`` — call ``fn(item)`` on every item
  concurrently, return the list of results in the SAME ORDER as ``items``.
  Exceptions are caught and reported via ``on_error`` callback rather
  than aborting the whole batch — one slow / failing provider must not
  cancel the rest. Returns ``None`` in the result slot for failed items.

- ``parallel_for_each(fn, items, ...)`` — same but discards results,
  used for fire-and-watch patterns like Stop's per-provider store calls.

Both share a single ``ThreadPoolExecutor`` with a pool sized to the
number of items. Pool teardown waits for in-flight work or hits the
budget — whichever comes first.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TimeoutError
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar

log = logging.getLogger("claude_hooks._parallel")

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    max_workers: Optional[int] = None,
    timeout: Optional[float] = None,
    on_error: Optional[Callable[[T, BaseException], None]] = None,
) -> list[Optional[R]]:
    """Run ``fn(item)`` on every item concurrently.

    Returns a list parallel to ``items`` — failed / timed-out slots are
    ``None``. ``on_error`` is invoked for each exception so the caller
    can log per-item context (which provider, which query, …).

    With a single item, falls through to a synchronous call to avoid
    the threadpool overhead. With zero items, returns an empty list.
    """
    items = list(items)
    if not items:
        return []
    if len(items) == 1:
        try:
            return [fn(items[0])]
        except BaseException as exc:
            if on_error is not None:
                try:
                    on_error(items[0], exc)
                except Exception:
                    pass
            return [None]

    workers = max_workers or len(items)
    results: list[Optional[R]] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        try:
            # Iterate in submission order so we cleanly bound by `timeout`
            # but still preserve item-order in the return list.
            for fut, idx in futures.items():
                try:
                    results[idx] = fut.result(timeout=timeout)
                except BaseException as exc:
                    if on_error is not None:
                        try:
                            on_error(items[idx], exc)
                        except Exception:
                            pass
                    results[idx] = None
        except _TimeoutError:
            # Pool's __exit__ will still wait for outstanding workers
            # but we abandon their results.
            log.debug("parallel_map: timeout after %.2fs", timeout)
    return results


def parallel_for_each(
    fn: Callable[[T], Any],
    items: Iterable[T],
    *,
    max_workers: Optional[int] = None,
    timeout: Optional[float] = None,
    on_error: Optional[Callable[[T, BaseException], None]] = None,
) -> None:
    """Run ``fn(item)`` on every item concurrently, discard results.

    For fire-and-collect patterns. Same exception-isolation contract as
    ``parallel_map`` — one failure does not cancel the others.
    """
    parallel_map(
        fn, list(items),
        max_workers=max_workers, timeout=timeout, on_error=on_error,
    )
