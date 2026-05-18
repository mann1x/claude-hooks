"""M14 — Daemon-side reaper for the consultants BaseStore.

When :class:`ProviderBackedStore` was extended with per-namespace
TTL in M14-F, the writes started carrying ``expires_at`` columns on
both pgvector and sqlite_vec. This module is the periodic sweep
that turns those columns into actual deletions — and, on
``research`` rows specifically, into a Caliber-style summary
written into the durable ``("project", project_id)`` namespace
before the deletion.

Episodic short-term → semantic long-term: every session's research
findings live for ``store.ttl.research_days`` (default 30 d), then
the next sweep collapses them into ONE durable entry under the
project namespace, and the originals are deleted. ``tool_results``
get deleted unconditionally after 24 h — they're cheap to drop and
nothing inside them is worth distilling.

The reaper mirrors the
:meth:`claude_hooks.embedding_manager.EmbeddingManager._reaper_loop`
0.5 s-slice shutdown pattern so the consultants FastAPI server can
shut down cleanly without waiting an hour for the next sweep tick.

Critical invariant — the reaper **only deletes research originals
after a successful distillation write**. If every model in the
configured fallback chain fails, the originals stay in place and
the next sweep tick retries (:class:`DistillationFailed`
propagation, caught here per-group). This guarantees the TTL never
silently throws away findings that the distiller couldn't capture.

The cost gate is two-fold:

- ``min_entries_per_distillation`` (default 3) — single- and
  double-finding sessions just get deleted without an LLM call.
- ``max_session_entries`` (default 50) — larger groups truncate
  before prompt assembly so a single bursty session can't blow up
  the prompt budget.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from consultants.engine.distillation import (
    DistillationFailed,
    Distiller,
    project_id_from_cwd,
    write_distilled_summary,
)

log = logging.getLogger("consultants.engine.store_reaper")


# Group key dispatch. ``research`` rows distill; ``tool_results``
# rows skip distillation and just delete; anything else (including
# project / user namespaces, which are TTL-less by default) is
# treated as ``unknown`` and deleted as a defensive fallback.
KIND_RESEARCH = "research"
KIND_TOOL_RESULTS = "tool_results"
KIND_UNKNOWN = "unknown"


# ============================================================== #
# Grouping helper
# ============================================================== #


def _group_by_sid_and_kind(
    rows: Sequence[Any],
) -> dict[tuple[str, str], list[Any]]:
    """Bucket ExpiringRow rows by (sid, namespace_kind).

    Rows whose metadata is missing or whose namespace shape is
    unrecognised land in ``("?", "unknown")`` — the reaper still
    deletes them so the next sweep doesn't keep finding them. This
    is the same defensive shape used elsewhere in the M8 / M14 code
    where bad metadata is logged and dropped, not crashed on.
    """
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        md = getattr(row, "metadata", None) or {}
        ns = md.get("namespace")
        sid = "?"
        kind = KIND_UNKNOWN
        if isinstance(ns, (list, tuple)) and len(ns) == 2:
            head, tail = ns[0], ns[1]
            if head in ("project", "user"):
                # Shouldn't normally show up — these namespaces have
                # no TTL by default — but if a user set a TTL on them
                # we still want to delete (no distillation).
                sid = str(tail)
                kind = KIND_UNKNOWN
            else:
                sid = str(head)
                if tail == KIND_RESEARCH:
                    kind = KIND_RESEARCH
                elif tail == KIND_TOOL_RESULTS:
                    kind = KIND_TOOL_RESULTS
        groups[(sid, kind)].append(row)
    return groups


def _cwd_hint_for_group(rows: Sequence[Any]) -> Optional[str]:
    """Pick the first non-empty cwd from the group's row metadata.

    M8's record_finding stamps ``cwd`` on the metadata when it's
    available, but legacy rows or rows from contexts that didn't
    have a cwd may omit it. The reaper just passes whatever it
    finds (or None) to the distiller.
    """
    for r in rows:
        md = getattr(r, "metadata", None) or {}
        cwd = md.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _question_hint_for_group(rows: Sequence[Any]) -> Optional[str]:
    """Pick the first non-empty user-question from the group."""
    for r in rows:
        md = getattr(r, "metadata", None) or {}
        q = md.get("question") or md.get("user_question")
        if isinstance(q, str) and q:
            return q
    return None


# ============================================================== #
# StoreReaperThread
# ============================================================== #


class StoreReaperThread:
    """Daemon thread that periodically sweeps expired store entries
    and (for the ``research`` namespace) writes a distilled summary
    before deletion.

    Shape mirrors :meth:`EmbeddingManager._reaper_loop` — a long
    interval sliced into 0.5 s sleeps so :meth:`stop` returns
    quickly. The sweep itself is wrapped in a bare ``try / except``
    so one bad iteration never poisons the thread.

    Args:
        store_cfg: ``cfg.store`` from the consultants config.
            Carries the TTL + distillation sub-configs.
        provider: a duck-typed provider with ``expire_before``,
            ``delete_by_hashes``, and ``store`` (the last only used
            when distillation lands a summary back into the project
            namespace via :func:`write_distilled_summary`).
        distiller: a :class:`Distiller` (or test double) that turns
            a research group into a single summary string. Pass
            ``None`` to disable distillation entirely — the reaper
            will still delete expired rows.
        store: the consultants BaseStore (used by the distiller's
            ``write_distilled_summary`` helper). Pass ``None`` to
            disable the project-namespace write — the reaper will
            then refuse to distill (since the summary would have
            nowhere to land).
    """

    def __init__(
        self,
        *,
        store_cfg: Any,
        provider: Any,
        distiller: Optional[Distiller] = None,
        store: Optional[Any] = None,
    ) -> None:
        self.cfg = store_cfg
        self._provider = provider
        self._distiller = distiller
        self._store = store
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_sweep_at: Optional[float] = None
        self._last_sweep_result: Optional[dict] = None
        self._lock = threading.Lock()

    # ---- lifecycle ---- #

    @property
    def interval_seconds(self) -> float:
        """Sweep cadence from config (default 3600 s)."""
        d = getattr(self.cfg, "distillation", None)
        if d is None:
            return 3600.0
        return float(getattr(d, "sweep_interval_seconds", 3600.0) or 3600.0)

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent — calling twice
        is a no-op while a thread is already alive."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="store-reaper",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join. The 0.5 s slice means the
        thread leaves within ~0.5 s of the event being set, well
        under the default timeout."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_sweep_result(self) -> Optional[dict]:
        """The most recent ``sweep_once`` return value, for ops +
        tests. ``None`` until the first sweep completes."""
        return self._last_sweep_result

    @property
    def last_sweep_at(self) -> Optional[float]:
        """``time.time()`` of the last sweep, or ``None``."""
        return self._last_sweep_at

    # ---- the loop ---- #

    def _loop(self) -> None:
        interval = self.interval_seconds
        while not self._stop_event.is_set():
            slept = 0.0
            while slept < interval:
                if self._stop_event.is_set():
                    return
                time.sleep(0.5)
                slept += 0.5
            try:
                self.sweep_once()
            except Exception:  # pragma: no cover - defensive
                log.exception("store-reaper sweep failed; continuing")

    # ---- the sweep ---- #

    def sweep_once(self) -> dict:
        """Run one sweep tick. Returns a stats dict — exposed publicly
        for ops + tests; the loop ignores the return value.

        Returns:
            ``{"expired": N, "distilled": K, "deleted": M,
            "groups_skipped_below_threshold": S,
            "groups_distill_failed": F,
            "rows_unknown_skipped": U,
            "rolled_over": R}``

            ``rolled_over`` (#215) counts research groups that the
            per-sweep cap (``max_groups_per_sweep``) deferred to the
            next sweep tick — their originals stay in place untouched.
        """
        now = datetime.now(timezone.utc)
        # 5-minute grace window — avoids racing rows whose ``put``
        # is in-flight from the same session.
        grace = timedelta(minutes=5)
        before_iso = (now - grace).isoformat()
        try:
            expiring = self._provider.expire_before(
                before_iso=before_iso, limit=1000,
            )
        except Exception:
            log.exception("store-reaper: expire_before failed")
            return {
                "expired": 0, "distilled": 0, "deleted": 0,
                "groups_skipped_below_threshold": 0,
                "groups_distill_failed": 0,
                "rows_unknown_skipped": 0,
                "rolled_over": 0,
            }
        if not expiring:
            self._stamp(now)
            return self._record_result({
                "expired": 0, "distilled": 0, "deleted": 0,
                "groups_skipped_below_threshold": 0,
                "groups_distill_failed": 0,
                "rows_unknown_skipped": 0,
                "rolled_over": 0,
            })

        groups = _group_by_sid_and_kind(expiring)
        distilled = 0
        deleted = 0
        skipped_below = 0
        distill_failed = 0
        unknown_skipped = 0
        rolled_over = 0
        min_entries = self._min_entries_per_distillation()
        distill_enabled = self._distillation_enabled()
        # #215: per-sweep cap + inter-group pacing. The cap counts
        # ONLY successful distillations (cost-gate skips, UNKNOWN
        # leaks, and tool_results deletes are cheap and don't pay
        # embedder or LLM cost, so they don't burn the budget).
        max_groups = self._max_groups_per_sweep()  # 0 = uncapped
        pace_seconds = self._pace_seconds_between_distillations()

        for (sid, kind), rows in groups.items():
            if not rows:
                continue
            if kind == KIND_RESEARCH and distill_enabled:
                if len(rows) < min_entries:
                    # Cost gate — drop without distilling. The
                    # findings are gone, but they were a thin
                    # session anyway.
                    skipped_below += 1
                    try:
                        deleted += self._delete_rows(rows)
                    except Exception:
                        log.exception("store-reaper: delete failed for sid=%s", sid)
                    continue
                # #215: bail out of the distillation loop once we
                # hit the cap. The remaining groups roll over to
                # the next sweep tick.
                if max_groups > 0 and distilled >= max_groups:
                    rolled_over += 1
                    continue
                # #215: pace consecutive distillations so the
                # embedder gets breathing room between groups.
                if pace_seconds > 0.0 and distilled > 0:
                    self._sleep_paced(pace_seconds)
                try:
                    summary = self._distill_group(sid, rows)
                    self._write_summary(sid, rows, summary, distilled_at=now)
                except DistillationFailed as e:
                    log.warning(
                        "store-reaper: distillation failed for sid=%s "
                        "(%s); keeping originals for next tick",
                        sid, e,
                    )
                    distill_failed += 1
                    continue
                # Successful distillation → safe to delete originals.
                try:
                    deleted += self._delete_rows(rows)
                except Exception:
                    log.exception(
                        "store-reaper: post-distill delete failed for sid=%s",
                        sid,
                    )
                    continue
                distilled += 1
            elif kind == KIND_UNKNOWN:
                # Post-#213: UNKNOWN means we couldn't classify the
                # namespace — either the metadata got corrupted on
                # disk OR a future schema added a new namespace kind
                # that this daemon version doesn't know about. The
                # rows DID expire, but "we don't know what kind it
                # is" is too thin a basis to delete: if the content
                # is recoverable, deleting loses it forever. So
                # leak-then-log: skip the delete, the TTL filter on
                # _do_search / _do_get already hides them from
                # consumers (they're already expired), and a human
                # can audit later via direct provider query.
                #
                # Cost is bounded: UNKNOWN only happens on corrupted
                # metadata or schema-version skew, both rare. The
                # alternative is silent data loss.
                log.warning(
                    "store-reaper: %d rows for sid=%s have "
                    "unrecognised namespace (KIND_UNKNOWN); "
                    "skipping delete to avoid data loss. "
                    "Inspect via the provider directly if this "
                    "persists.",
                    len(rows), sid,
                )
                unknown_skipped += len(rows)
            else:
                # tool_results — delete without distillation per
                # M14 spec (cheap to drop, nothing worth distilling).
                try:
                    deleted += self._delete_rows(rows)
                except Exception:
                    log.exception("store-reaper: delete failed for sid=%s", sid)
        self._stamp(now)
        return self._record_result({
            "expired": len(expiring),
            "distilled": distilled,
            "deleted": deleted,
            "groups_skipped_below_threshold": skipped_below,
            "groups_distill_failed": distill_failed,
            "rows_unknown_skipped": unknown_skipped,
            "rolled_over": rolled_over,
        })

    # ---- helpers ---- #

    def _stamp(self, now: datetime) -> None:
        self._last_sweep_at = time.time()

    def _record_result(self, result: dict) -> dict:
        with self._lock:
            self._last_sweep_result = result
        return result

    def _min_entries_per_distillation(self) -> int:
        d = getattr(self.cfg, "distillation", None)
        if d is None:
            return 3
        return int(getattr(d, "min_entries_per_distillation", 3) or 3)

    def _distillation_enabled(self) -> bool:
        if self._distiller is None or self._store is None:
            return False
        d = getattr(self.cfg, "distillation", None)
        if d is None:
            return False
        return bool(getattr(d, "enabled", False))

    def _max_groups_per_sweep(self) -> int:
        """#215: per-sweep distillation cap. ``0`` means uncapped.

        Caps the number of LLM-driven distillations per sweep tick so
        a backlog can't fan out into a synchronous batch that
        saturates the embedder (each successful distillation writes
        one project-namespace summary, and each researcher write in
        the original session already paid one embedder round-trip).
        Remaining groups roll over to the next tick (counted in the
        result dict under ``rolled_over``).
        """
        d = getattr(self.cfg, "distillation", None)
        if d is None:
            return 5
        return int(getattr(d, "max_groups_per_sweep", 5) or 0)

    def _pace_seconds_between_distillations(self) -> float:
        """#215: inter-group pacing — sleep N seconds between
        consecutive distillations within one sweep tick. Default 5 s.

        The pace gives the embedder breathing room between project-
        namespace summary writes; on a single-llamafile CPU embedder
        the rolling p95 sits around 1-3 s per write under load, and 5
        s leaves headroom for variance. ``0.0`` disables pacing
        entirely (back to back-to-back).
        """
        d = getattr(self.cfg, "distillation", None)
        if d is None:
            return 5.0
        return float(getattr(d, "pace_seconds_between_distillations", 5.0) or 0.0)

    def _sleep_paced(self, seconds: float) -> None:
        """Sleep ``seconds`` in 0.5 s slices, leaving early if
        :meth:`stop` is called. Same shape as the main ``_loop`` so
        the reaper still shuts down within ~0.5 s of the stop event."""
        if seconds <= 0.0:
            return
        slept = 0.0
        while slept < seconds:
            if self._stop_event.is_set():
                return
            slice_s = min(0.5, seconds - slept)
            time.sleep(slice_s)
            slept += slice_s

    def _distill_group(self, sid: str, rows: Sequence[Any]) -> str:
        """Run distillation for one group. The Distiller raises
        :class:`DistillationFailed` on total failure; the caller
        catches that and keeps originals."""
        assert self._distiller is not None  # _distillation_enabled gates this
        cwd_hint = _cwd_hint_for_group(rows)
        q_hint = _question_hint_for_group(rows)
        return self._distiller.distill_session(
            sid, rows, cwd_hint=cwd_hint, question_hint=q_hint,
        )

    def _write_summary(
        self,
        sid: str,
        rows: Sequence[Any],
        summary: str,
        *,
        distilled_at: datetime,
    ) -> str:
        """Write the distilled summary into the
        ``("project", project_id)`` namespace. Project id is derived
        from the cwd recorded on the rows' metadata (falling back to
        the sentinel id when no row has one)."""
        cwd_hint = _cwd_hint_for_group(rows) or ""
        pid = project_id_from_cwd(cwd_hint)
        extra = {
            "cwd": cwd_hint,
            "namespace": ["project", pid],
            "_consultants_store": True,
        }
        if self._distiller is not None:
            extra["distill_model"] = getattr(self._distiller.cfg, "model", "?")
        return write_distilled_summary(
            self._store,
            summary=summary,
            sid=sid,
            project_id=pid,
            original_count=len(rows),
            distilled_at=distilled_at.isoformat(),
            extra_meta=extra,
        )

    def _delete_rows(self, rows: Sequence[Any]) -> int:
        """Hard-delete by content_hash. Returns count.

        ``rows`` whose ``content_hash`` is None / empty are skipped
        defensively — the provider can't key off a missing hash, and
        the next sweep will see them again and try a different path."""
        hashes: list[bytes] = []
        for r in rows:
            h = getattr(r, "content_hash", None)
            if h:
                hashes.append(h)
        if not hashes:
            return 0
        try:
            return int(self._provider.delete_by_hashes(hashes))
        except Exception:
            log.exception("store-reaper: delete_by_hashes failed")
            return 0
