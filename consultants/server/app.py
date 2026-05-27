"""FastAPI app for the /consultants engine.

The app exposes a small REST surface that the ``claude-consultants``
CLI drives. The actual council execution is *not* wired into this
module directly — it goes through ``app.state.run_council``, which
production code (``consultants.server.runner``) populates with a
LangGraph-backed implementation. Tests substitute a stub runner so
the app can be exercised without LangChain installed.

Routes:

- ``GET  /v1/health``                       liveness
- ``POST /v1/consult``                      start a session
- ``GET  /v1/consult/{sid}``                poll status (in-memory + disk fallback)
- ``GET  /v1/consult/{sid}/result``         final summary.md + metadata.json
- ``GET  /v1/sessions?cwd=<path>``          list past sessions in a project
- ``GET  /v1/config``                       current config snapshot

Sessions live in ``app.state.sessions`` (an in-memory dict) while
running. Once the runner finishes, the on-disk
``.claude-hooks/consultants/<sid>/`` directory is the source of
truth. Status polls fall back to disk when the in-memory entry is
gone (e.g. after a service restart).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

from consultants import config as cc
from consultants.engine import sessions_index, storage

log = logging.getLogger("consultants.server")


# M9 inject lifecycle records + status/routing constants live in
# ``control.py`` (the pure control-contract module) so both this
# module and ``control_routes`` can import them without a cycle
# (app imports control_routes at app-build time).
from consultants.server.control import (  # noqa: E402
    Injection,
    INJECT_STATUS_APPLIED,
    INJECT_STATUS_FAILED,
    INJECT_STATUS_PENDING,
    INJECT_STATUS_REJECTED,
    ROUTED_BEST_EFFORT_CAP_REACHED,
    ROUTED_IN_PLACE,
    ROUTED_QUEUED,
    ROUTED_REWOUND_TO_RESEARCHER,
)


# ----------------------- in-memory session state ----------------- #

@dataclass
class SessionState:
    sid: str
    cwd: str
    question: str
    effort: str
    topology: str
    status: str = "running"   # running | completed | failed
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    # Per-role progress for status polls. Keys: planner / researcher /
    # critic / synthesizer; values: "pending" | "in_progress" | "done".
    progress: dict[str, str] = field(default_factory=dict)
    # ---- live-session fields (retained after completion for follow-ups)
    # Populated by the runner once the consultation finishes; the
    # follow-up runner reads these instead of re-loading from disk so
    # warm ChatClient ``_probed_think`` caches are reused too.
    plan: str = ""
    plan_items: list[str] = field(default_factory=list)
    research: list[str] = field(default_factory=list)
    critique: Optional[str] = None
    final_answer: str = ""
    models: dict[str, str] = field(default_factory=dict)
    # Follow-up chain. ``parent_sid`` points at the prior consultation
    # whose state was reused; ``follow_up_sids`` records direct
    # children. The chain is reconstructable in either direction.
    parent_sid: Optional[str] = None
    follow_up_sids: list[str] = field(default_factory=list)
    # ---- consultancy review loop (engine-owned status machine) -----
    # A *consultancy* is the chain of sessions rooted at the first
    # ``ask`` (``root_sid``); followups are children. These five fields
    # are authoritative ONLY on the root session — children carry
    # ``root_sid`` (inherited from their parent) so any sid resolves to
    # its root in O(1) via ``_resolve_consultancy_root``. The status
    # advances: in_progress -> ready_to_review (council done) ->
    # accepted (terminal) | awaiting_approval (cap hit, needs user OK).
    # ``followup_count`` counts followups issued in this consultancy;
    # ``max_followups`` is resolved from config at creation and stored
    # so it's stable for the chain's life; ``extra_granted`` accumulates
    # one-off ``--allow-extra`` grants (never persisted to config).
    # Mirrored to ``consultancy.json`` in the root session dir on every
    # transition so the status survives idle reap / restart / compaction.
    root_sid: Optional[str] = None
    consultancy_status: str = "in_progress"
    followup_count: int = 0
    max_followups: int = 0
    extra_granted: int = 0
    # Consultancy membership (root only): every followup sid in the
    # chain, newest last. Distinct from ``follow_up_sids`` (direct
    # children of THIS node) — this is the flat union for the whole
    # consultancy, used for reconstruction / debugging.
    consultancy_children: list[str] = field(default_factory=list)
    # v1.8+: extra allowed directories for the tool sandbox. Set on
    # creation from the request body's ``extra_roots`` field (already
    # auto-unioned with settings-file discovery by the HTTP layer).
    # Follow-ups inherit this list and may extend it; ``run_follow_up``
    # in the runner merges the parent's roots with the follow-up's.
    extra_roots: list[str] = field(default_factory=list)
    # 2026-05-18: parallel display form of ``extra_roots``, holding the
    # user-facing pre-realpath path (``/shared/dev/<x>`` instead of
    # ``/srv/dev-disk-by-label-opt/dev/<x>`` when the user's settings
    # use the ``/shared`` symlink). ``extra_roots`` itself stays the
    # realpath form for tool-sandbox checks. Same length + same order
    # as ``extra_roots``. Empty list when display info wasn't
    # captured at session-creation time (legacy sessions on disk).
    extra_roots_display: list[str] = field(default_factory=list)
    # Display form of the primary cwd (pre-realpath) — used only for
    # log rendering, never for filesystem access. ``cwd`` itself
    # remains the original path the runner received (so subsequent
    # tool ops don't suddenly differ).
    cwd_display: Optional[str] = None
    # Lifecycle. ``closed`` flips on explicit close OR idle reap;
    # downstream follow-up requests against a closed sid 410.
    # ``last_activity_at`` is bumped on every poll, follow-up start,
    # and follow-up completion so the reaper doesn't kill an
    # actively-iterating session. Defaults to ``started_at``.
    closed: bool = False
    closed_at: Optional[float] = None
    last_activity_at: float = field(default_factory=time.time)
    # Warm engine handles. NOT serialized in public_dict — these are
    # opaque ChatClient instances per role with the
    # ``_probed_think`` / ``_unsupported_think`` caches populated.
    # The follow-up runner reuses these to skip the /api/show probe
    # and the upstream warmup.
    _chat_clients: Optional[dict] = field(default=None, repr=False)
    # Task #111: per-model raw ChatClients for the coder role's
    # failover chain. Same warm-reuse pattern as ``_chat_clients``
    # but keyed by Ollama model tag (not role). ``None`` on parents
    # that didn't enable per-language routing AND on v1.x sessions
    # reopened from disk (the follow-up runner cold-builds them
    # then). Empty dict means "enabled but only the default route
    # in use" — pre-built clients are stashed so the next follow-up
    # in the chain inherits them too.
    _coder_chat_clients_by_model: Optional[dict] = field(
        default=None, repr=False,
    )
    # v1.1 message-history fields. Populated from transcript.db on
    # reopen-from-disk and from the live recorder on warm-session
    # completion, so the disk-loaded path is indistinguishable from
    # warm. Phase 5's follow-up runner reads these and extends the
    # parent's threads instead of starting fresh.
    #
    # ``_role_messages[role]`` — flat per-role thread (last
    # llm_call's messages + final assistant message). Populated for
    # every role that issued at least one llm_call.
    # ``_role_lane_messages[role][lane_idx]`` — per-lane thread for
    # roles that fanned out (researcher in practice). When None for
    # a given role, that role didn't fan out.
    # Both are None on v1.0 sessions (no transcript.db) — the
    # follow-up runner branches on presence and falls back to the
    # turn-content reconstruction in that case.
    _role_messages: Optional[dict[str, list[dict]]] = field(
        default=None, repr=False,
    )
    _role_lane_messages: Optional[dict[str, dict[int, list[dict]]]] = field(
        default=None, repr=False,
    )
    # M9 (HTTP control surface): live LangGraph handles so the
    # control route handlers can read state, mutate runtime_control,
    # apply injects, schedule interrupts, and resume. Set by the
    # runner just before it calls ``compiled.stream(...)``. Cleared
    # to None when the session is closed/idle-reaped, freeing the
    # checkpointer file lock + ChatClient caches.
    #
    # ``_compiled`` — the LangGraph CompiledStateGraph object.
    # ``_thread_config`` — ``{"configurable": {"thread_id": sid}}``.
    # ``_recorder`` — MessageRecorder instance for SSE event replay
    # via runtime_events table. None when recorder is disabled.
    _compiled: Optional[Any] = field(default=None, repr=False)
    _thread_config: Optional[dict] = field(default=None, repr=False)
    _recorder: Optional[Any] = field(default=None, repr=False)
    # M9 inject lifecycle (2026-05-24). ``_injections`` is the full
    # registry (every inject ever received, in arrival order) surfaced
    # by GET /state and the CLI. ``_pending_injections`` is the subset
    # not yet successfully routed (spin-up race or deferred rewind);
    # the runner drains it once the live graph attaches, and the
    # inject/state handlers drain it opportunistically. Guarded by
    # ``_inject_lock`` because the HTTP handler thread and the runner
    # thread both touch it — see [[feedback_psycopg_not_thread_safe]]
    # for the analogous cross-thread-mutation lesson.
    _injections: list = field(default_factory=list, repr=False)
    _pending_injections: "deque" = field(default_factory=deque, repr=False)
    _inject_lock: Any = field(default_factory=threading.RLock, repr=False)
    # #314 rewind: a synthesis-phase inject that wants the council to
    # loop back through a researcher round before re-synthesizing sets
    # this flag (caps permitting). The runner's stream loop reads it at
    # the synthesizer interrupt boundary, performs the rewind, and
    # clears it. Lives on SessionState (not graph state) so the rewind
    # is driven entirely by the runner without a graph topology change.
    _revalidation_pending: bool = field(default=False, repr=False)

    def public_dict(self) -> dict:
        return {
            "sid": self.sid,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                (self.finished_at or time.time()) - self.started_at
            ),
            "error": self.error,
            "progress": dict(self.progress),
            "question": self.question,
            "effort": self.effort,
            "topology": self.topology,
            "parent_sid": self.parent_sid,
            "follow_up_sids": list(self.follow_up_sids),
            "closed": self.closed,
            "closed_at": self.closed_at,
            "last_activity_at": self.last_activity_at,
            "models": dict(self.models),
        }

    def bump_activity(self) -> None:
        """Defer the idle reaper. Called on every poll, follow-up
        start, and follow-up completion. Cheap; no lock needed
        (a stale read costs at most one reaper interval)."""
        self.last_activity_at = time.time()

    # ---- M9 inject registry ------------------------------------- #

    def register_injection(self, inj: "Injection") -> None:
        """Record an injection in the arrival-order registry. Does NOT
        enqueue it for draining — callers that applied the inject
        synchronously (graph was ready) register only; callers that
        need it drained later (spin-up race / deferred rewind) call
        :meth:`enqueue_injection`."""
        with self._inject_lock:
            self._injections.append(inj)

    def enqueue_injection(self, inj: "Injection") -> int:
        """Register ``inj`` AND mark it pending for the next drain.

        Returns the 1-based queue position so the caller can report it
        in the inject response body. Idempotent on ``content_hash``:
        re-enqueuing the same content returns the existing record's
        position without creating a duplicate.
        """
        with self._inject_lock:
            if inj.content_hash:
                for existing in self._injections:
                    if existing.content_hash == inj.content_hash:
                        # Surface the existing record's queue position
                        # (or 0 if it already drained).
                        try:
                            return list(self._pending_injections).index(
                                existing) + 1
                        except ValueError:
                            return 0
            self._injections.append(inj)
            inj.status = INJECT_STATUS_PENDING
            inj.routed = ROUTED_QUEUED
            self._pending_injections.append(inj)
            return len(self._pending_injections)

    def drain_injections(self, apply_fn) -> list["Injection"]:
        """Apply each pending injection via ``apply_fn(inj)``.

        ``apply_fn`` must mutate ``inj`` in place — set ``inj.status``
        to one of the terminal statuses (applied/rejected/failed) and
        fill ``routed`` / ``target_role`` / ``phase_at_apply`` /
        ``applied_at`` — OR leave ``inj.status`` as ``pending`` to keep
        it queued for a later drain. If ``apply_fn`` raises, the inject
        is marked ``failed`` (defensive — a drain must never crash the
        runner or an HTTP handler).

        Returns the list of injections that left the pending queue this
        call (their final status is on each record).
        """
        drained: list["Injection"] = []
        with self._inject_lock:
            remaining: "deque" = deque()
            while self._pending_injections:
                inj = self._pending_injections.popleft()
                try:
                    apply_fn(inj)
                except Exception as e:  # pragma: no cover — defensive
                    inj.status = INJECT_STATUS_FAILED
                    inj.error = f"{type(e).__name__}: {e}"
                    log.exception(
                        "drain_injections apply_fn raised for sid=%s "
                        "inj=%s", self.sid, inj.id,
                    )
                if inj.status == INJECT_STATUS_PENDING:
                    remaining.append(inj)
                else:
                    drained.append(inj)
            self._pending_injections = remaining
        return drained

    def injection_records(self) -> list[dict]:
        """Public snapshot of every injection for GET /state + CLI."""
        with self._inject_lock:
            return [inj.to_public() for inj in self._injections]

    # ---- #314 rewind signalling -------------------------------- #

    def request_revalidation(self) -> None:
        """Mark that a synthesis-phase inject wants the council to
        rewind to a researcher round before finalizing. Set by the
        inject handler (HTTP thread); read+cleared by the runner."""
        with self._inject_lock:
            self._revalidation_pending = True

    def take_revalidation(self) -> bool:
        """Atomically read-and-clear the revalidation flag. Returns
        True if a rewind was requested since the last take. The runner
        calls this at the synthesizer interrupt boundary."""
        with self._inject_lock:
            pending = self._revalidation_pending
            self._revalidation_pending = False
            return pending


# ----------------------- runner contract ------------------------- #
# Production runner is a Callable[[SessionState, dict], None] that
# blocks until the council finishes and writes summary.md /
# transcript.md / metadata.json under
# ``<cwd>/.claude-hooks/consultants/<sid>/``. It also updates the
# SessionState in place (status, progress, error). Tests provide a
# fake runner that just sets status="completed".

RunCouncilFn = Callable[[SessionState, dict], None]


# ----------------------- app factory ----------------------------- #

def _new_sid() -> str:
    """Generate a session id like ``csl-2026-05-06-1730-3f9a``."""
    import secrets
    ts = time.strftime("%Y-%m-%d-%H%M")
    return f"csl-{ts}-{secrets.token_hex(2)}"


def _merge_extra_roots(parent: list[str], follow_up: list[str]) -> list[str]:
    """Union parent's extra_roots with follow-up's, preserving order
    and de-duplicating. Used at follow-up creation time so the child
    SessionState records the full effective allow-list.

    Reused by tests so the merge contract is one definition.
    """
    seen: set[str] = set()
    out: list[str] = []
    for r in list(parent) + list(follow_up):
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


# Live-session lifecycle defaults — see EVALUATION.md and the
# /consultants skill. The reaper closes idle sessions to bound
# resource use; bump these for very long iteration cycles.
DEFAULT_IDLE_TIMEOUT_S = 1800.0      # 30 minutes
DEFAULT_REAPER_INTERVAL_S = 60.0     # check every minute


def create_app(*, run_council: Optional[RunCouncilFn] = None,
               max_workers: int = 4,
               idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
               reaper_interval_s: float = DEFAULT_REAPER_INTERVAL_S,
               run_follow_up: Optional[Callable[..., None]] = None,
               start_reaper: bool = True,
               cfg: Optional["cc.ConsultantsConfig"] = None,
               ollama_base_url: Optional[str] = None) -> "FastAPI":
    """Build a FastAPI app. ``run_council`` is the in-process
    council executor; if None, the app comes up but ``/v1/consult``
    returns 503 (useful for tests that only exercise the read-side
    routes).

    ``cfg`` + ``ollama_base_url`` enable the M14 store reaper. When
    ``cfg.store.enabled`` is True and either ``cfg.store.ttl.enabled``
    or ``cfg.store.distillation.enabled`` is set, a daemon
    :class:`~consultants.engine.store_reaper.StoreReaperThread` is
    spawned at startup and joined on shutdown. Tests / older callers
    that don't pass cfg just skip the reaper — opt-in by design.
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "fastapi is not installed. The consultants server "
            "requires the `claude-hooks-consultants` conda env."
        ) from e

    app = FastAPI(title="claude-hooks-consultants",
                  version="0.1.0")
    app.state.sessions = {}                       # type: dict[str, SessionState]
    app.state.run_council = run_council
    app.state.run_follow_up = run_follow_up
    app.state.executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="consultants-runner",
    )
    app.state.sessions_lock = threading.Lock()
    app.state.idle_timeout_s = idle_timeout_s
    app.state.reaper_interval_s = reaper_interval_s
    app.state.reaper_stop = threading.Event()
    if start_reaper:
        _start_idle_reaper(app)

    # M14: optionally spawn the store reaper (TTL sweep +
    # distillation-on-expiry). Off unless cfg + ollama_base_url are
    # both supplied AND cfg.store enables either TTL or
    # distillation. Failure to start is logged + non-fatal — the
    # app still comes up.
    app.state.store_reaper = None
    if cfg is not None and ollama_base_url:
        try:
            _maybe_start_store_reaper(app, cfg, ollama_base_url)
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "_maybe_start_store_reaper failed; "
                "M14 reaper unavailable",
            )

    # Shutdown hook — joins the store reaper if one was started.
    # Idle-reaper shutdown is already handled via reaper_stop.
    @app.on_event("shutdown")
    async def _shutdown_store_reaper() -> None:  # pragma: no cover
        reaper = getattr(app.state, "store_reaper", None)
        if reaper is not None:
            try:
                reaper.stop(timeout=5.0)
            except Exception:
                log.exception("store_reaper.stop failed")
        # Also signal the idle reaper to exit (uvicorn already does
        # this for the daemon thread, but we're explicit).
        try:
            app.state.reaper_stop.set()
        except Exception:
            pass

    # M9: register the control-surface routes (GET /state, POST
    # /inject / /control / /interrupt / /resume / /cancel, GET
    # /events SSE). Lazy import so test envs that only need the
    # builders (consultants.server.control) don't pay the routes
    # cost. Failure is non-fatal — the app comes up without M9
    # routes when the import path is unhappy.
    try:
        from consultants.server.control_routes import (
            register_control_routes,
        )
        register_control_routes(app)
    except Exception:  # pragma: no cover — defensive
        log.exception(
            "register_control_routes failed; M9 endpoints unavailable",
        )

    # ----------------------- health ------------------------------ #
    @app.get("/v1/health")
    def health() -> dict:
        return {
            "status": "ok",
            "runner_available": app.state.run_council is not None,
            "active_sessions": len([
                s for s in app.state.sessions.values()
                if s.status == "running"
            ]),
        }

    # ----------------------- start ------------------------------- #
    @app.post("/v1/consult")
    def consult(body: dict) -> dict:
        if app.state.run_council is None:
            raise HTTPException(
                status_code=503,
                detail="council runner not configured",
            )
        question = (body.get("message") or "").strip()
        if not question:
            raise HTTPException(
                status_code=400,
                detail="message is required",
            )
        cwd = body.get("cwd") or "."
        cwd_path = Path(cwd).resolve()
        if not cwd_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"cwd does not exist: {cwd_path}",
            )
        cfg = cc.load_config(cwd_path)
        effort_override = body.get("effort")
        if effort_override:
            if effort_override not in cc.EFFORT_BUDGETS:
                raise HTTPException(
                    status_code=400,
                    detail=(f"invalid effort: {effort_override}; "
                            f"valid: {sorted(cc.EFFORT_BUDGETS)}"),
                )
            cfg.effort = effort_override

        err = cc.validate_pipeline(cfg)
        if err:
            raise HTTPException(status_code=400, detail=err)

        # v1.8+: union of (a) extra_roots from the body (CLI's --add-dir)
        # and (b) auto-discovered settings.json roots. The HTTP server
        # is invoked by the CLI which is operator-driven (not LLM-driven
        # like caliber-grounding-proxy), so body-supplied roots are
        # operator-trusted here. Settings-file values come from disk
        # files the operator wrote.
        from claude_hooks.allowed_roots import (
            discover_allowed_roots_with_display,
        )
        body_extras = body.get("extra_roots") or []
        if not isinstance(body_extras, list) or not all(
            isinstance(x, str) for x in body_extras
        ):
            raise HTTPException(
                status_code=400,
                detail="extra_roots must be a list of strings",
            )
        # 2026-05-18 (#199): pass the user-typed cwd (``~`` expanded
        # but symlinks NOT resolved) so the discoverer can record both
        # forms — display = ``/shared/dev/claude-hooks``, real =
        # ``/srv/dev-disk-by-label-opt/dev/claude-hooks``. Passing
        # ``str(cwd_path)`` (already realpath-resolved) would make
        # display == real and the primary log line would drop the
        # alias. Validation via ``cwd_path.is_dir()`` above already
        # confirmed the resolved form exists.
        cwd_for_display = str(Path(cwd).expanduser())
        discovered, discovered_display = discover_allowed_roots_with_display(
            cwd_for_display, add_dirs=body_extras,
        )
        # discover_allowed_roots prepends the primary cwd; the runner
        # wants extras only. Both lists share order so the parallel
        # ``[1:]`` slices stay aligned.
        session_extra_roots: list[str] = list(discovered[1:])
        session_extra_roots_display: list[str] = list(
            discovered_display[1:]
        )

        sid = _new_sid()
        state = SessionState(
            sid=sid,
            cwd=str(cwd_path),
            cwd_display=discovered_display[0],
            question=question,
            effort=cfg.effort,
            topology=cfg.topology,
            progress={r: "pending" for r in cc.enabled_roles(cfg)},
            extra_roots=session_extra_roots,
            extra_roots_display=session_extra_roots_display,
        )
        # Consultancy review loop: a fresh ask is its own root. Seed the
        # cap from config (stable for the chain) and persist the sidecar
        # up front so the status survives even an immediate reap.
        state.root_sid = sid
        state.max_followups = cfg.max_followups
        state.consultancy_status = CONSULTANCY_IN_PROGRESS
        with app.state.sessions_lock:
            app.state.sessions[sid] = state
        _persist_consultancy(state)

        # Append to the per-project sessions index up front so
        # /v1/sessions surfaces in-progress runs too.
        sessions_index.append(
            cwd_path,
            sessions_index.SessionEntry(
                session_id=sid,
                created=time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.localtime(state.started_at)),
                question=question,
                topology=cfg.topology,
                effort=cfg.effort,
                status="running",
                duration_seconds=0.0,
            ),
        )

        # Per-request trace override. ``body["trace"]`` (bool) wins over
        # the process-wide ``CONSULTANTS_TRACE`` env var. ``None`` =
        # fall through to env-var default.
        trace_flag = body.get("trace")
        if trace_flag is not None and not isinstance(trace_flag, bool):
            raise HTTPException(
                status_code=400,
                detail="trace must be a bool",
            )

        runner_input = {
            "config": cfg,
            "cwd": str(cwd_path),
            "cwd_display": discovered_display[0],
            "question": question,
            "trace": trace_flag,
            "extra_roots": session_extra_roots,
            "extra_roots_display": session_extra_roots_display,
        }

        # Hand off to the executor. The runner mutates ``state`` and
        # writes the on-disk artifacts; we just track completion.
        future = app.state.executor.submit(
            _run_with_state, app, app.state.run_council, state,
            runner_input,
        )
        # Don't block on future; CLI polls.
        del future

        return {
            "sid": sid,
            "status": "running",
            "status_url": f"/v1/consult/{sid}",
        }

    # ----------------------- poll -------------------------------- #
    @app.get("/v1/consult/{sid}")
    def poll(sid: str) -> dict:
        state = app.state.sessions.get(sid)
        if state is not None:
            # Bump activity so polling alone keeps the session warm.
            # The skill polls every ~10s; a stuck Claude session
            # therefore can't accidentally time out under us.
            state.bump_activity()
            return _attach_consultancy(app, sid, state.public_dict())
        # Fall back to disk: maybe the service restarted.
        disk = _load_session_from_disk(sid)
        if disk is None:
            raise HTTPException(
                status_code=404,
                detail=f"session not found: {sid}",
            )
        return disk

    # ----------------------- follow-up --------------------------- #
    # Live-session iteration: spawn a NEW consultation that reuses
    # the parent's plan + research + critique + warm ChatClient
    # instances. Per docs/consultants-iteration.md, this is the
    # main cost-saver vs re-running a full consult — we skip
    # planning + fan-out and the warm clients skip /api/show.
    @app.post("/v1/consult/{sid}/follow-up")
    def follow_up(sid: str, body: dict) -> dict:
        if app.state.run_follow_up is None:
            raise HTTPException(
                status_code=503,
                detail="follow-up runner not configured",
            )
        # _resolve_parent_for_follow_up handles all four cases:
        #   warm + ready  → return as-is
        #   warm + closed → flip closed=False in place
        #   cold + cwd    → load from disk and register
        #   cold + no cwd → 400
        # And 409 when parent is still running. 410 (was emitted by
        # the original implementation when the parent was closed)
        # no longer occurs — closed parents auto-reopen.
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(
                status_code=400,
                detail="message is required",
            )
        cwd_hint = body.get("cwd")  # optional; needed only for cold path
        parent = _resolve_parent_for_follow_up(app, sid, cwd_hint)
        cwd_path = Path(parent.cwd)
        cfg = cc.load_config(cwd_path)
        # Effort override per follow-up; defaults to parent's effort.
        effort_override = body.get("effort") or parent.effort
        if effort_override not in cc.EFFORT_BUDGETS:
            raise HTTPException(
                status_code=400,
                detail=(f"invalid effort: {effort_override}; "
                        f"valid: {sorted(cc.EFFORT_BUDGETS)}"),
            )
        cfg.effort = effort_override

        # ---- consultancy review-loop cap enforcement --------------- #
        # Resolve the consultancy root (the chain anchor), then enforce
        # the followup cap. A legacy / pre-review-loop consultancy
        # (no consultancy.json yet) is initialized from the current
        # config cap on first touch so it isn't accidentally capped at
        # 0. ``allow_extra`` (resolved by the CLI to the configured
        # default when the flag was bare) raises the cap one-off for
        # THIS consultancy only — it never touches persisted config.
        croot = _resolve_consultancy_root(app, sid, cwd_hint) or parent
        if storage.read_consultancy(Path(croot.cwd), croot.sid) is None:
            croot.max_followups = cfg.max_followups
            if croot.consultancy_status == CONSULTANCY_IN_PROGRESS \
                    and croot.status == "completed":
                croot.consultancy_status = CONSULTANCY_READY
            _persist_consultancy(croot)
        allow_extra = 0
        ae_raw = body.get("allow_extra")
        if ae_raw is not None:
            try:
                allow_extra = max(0, int(ae_raw))
            except (TypeError, ValueError):
                allow_extra = 0
        effective_cap = croot.max_followups + croot.extra_granted
        if croot.followup_count >= effective_cap and allow_extra <= 0:
            croot.consultancy_status = CONSULTANCY_AWAITING
            _persist_consultancy(croot)
            return {
                "ok": False,
                "reason": FOLLOWUP_LIMIT_REACHED,
                "sid": None,
                "parent_sid": sid,
                "consultancy": _consultancy_dict(croot),
            }

        trace_flag = body.get("trace")
        if trace_flag is not None and not isinstance(trace_flag, bool):
            raise HTTPException(
                status_code=400,
                detail="trace must be a bool",
            )

        child_sid = _new_sid()
        # Child session inherits cwd + topology from parent. Progress
        # only tracks roles wired into the SHORTENED follow-up graph
        # (researcher + synthesizer always; critic only at high).
        child_progress = {"researcher": "pending", "synthesizer": "pending"}
        if cfg.effort in ("high", "max") and "critic" in cc.enabled_roles(cfg):
            child_progress["critic"] = "pending"
        # v1.8+: follow-up may extend the parent's extra_roots with its
        # own --add-dir entries. Validate the body shape, then let the
        # runner do the parent+follow-up merge so both lists round-trip
        # cleanly.
        followup_body_extras = body.get("extra_roots") or []
        if not isinstance(followup_body_extras, list) or not all(
            isinstance(x, str) for x in followup_body_extras
        ):
            raise HTTPException(
                status_code=400,
                detail="extra_roots must be a list of strings",
            )
        # 2026-05-18: parallel display-form merge so follow-up logs
        # also show the user-facing paths (the parent already carries
        # ``extra_roots_display`` for itself; we extend it with the
        # follow-up body's extras, treating them as their own display
        # form). Body entries that resolve to a parent entry's
        # realpath get dropped via ``_merge_extra_roots``; the display
        # list mirrors the same dedupe in step.
        merged_extra = _merge_extra_roots(
            parent.extra_roots, followup_body_extras,
        )
        if parent.extra_roots_display and len(parent.extra_roots_display) == len(parent.extra_roots):
            base_display = list(parent.extra_roots_display)
        else:
            base_display = list(parent.extra_roots)
        # Reconstruct display by stepping through ``merged_extra`` and
        # mapping each realpath back to (parent's display) if it came
        # from the parent, or to the user-supplied body extra otherwise.
        from claude_hooks.allowed_roots import _canonical  # internal
        body_real_to_display: dict[str, str] = {}
        for raw in followup_body_extras:
            real = _canonical(raw)
            if real:
                body_real_to_display.setdefault(real, raw)
        parent_real_to_display = dict(
            zip(parent.extra_roots, base_display)
        )
        merged_display = [
            parent_real_to_display.get(r) or body_real_to_display.get(r) or r
            for r in merged_extra
        ]
        child = SessionState(
            sid=child_sid,
            cwd=parent.cwd,
            cwd_display=parent.cwd_display or parent.cwd,
            question=message,
            effort=cfg.effort,
            topology=parent.topology,
            progress=child_progress,
            parent_sid=sid,
            # Consultancy anchor — every followup in the chain carries
            # the root sid so any sid resolves to its consultancy root.
            root_sid=croot.sid,
            # Stored extra_roots = parent's + this follow-up's, merged
            # in order with dedup. The runner does the same merge for
            # the in-flight executor; we persist it so disk-reopen of
            # the child surfaces the full set.
            extra_roots=merged_extra,
            extra_roots_display=merged_display,
        )
        with app.state.sessions_lock:
            app.state.sessions[child_sid] = child
            parent.follow_up_sids.append(child_sid)
        parent.bump_activity()  # iterating; defer reaper
        # Commit the followup against the consultancy: bank any one-off
        # override grant, count this round, record membership, and flip
        # the root back to in_progress (the auto-flip to ready_to_review
        # happens when this run completes). Persist the sidecar.
        if allow_extra > 0:
            croot.extra_granted += allow_extra
        croot.followup_count += 1
        croot.consultancy_children.append(child_sid)
        croot.consultancy_status = CONSULTANCY_IN_PROGRESS
        croot.bump_activity()
        _persist_consultancy(croot)

        # Index entry for the follow-up so /v1/sessions surfaces it.
        sessions_index.append(
            cwd_path,
            sessions_index.SessionEntry(
                session_id=child_sid,
                created=time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.localtime(child.started_at)),
                question=message,
                topology=parent.topology,
                effort=cfg.effort,
                status="running",
                duration_seconds=0.0,
            ),
        )

        runner_input = {
            "config": cfg,
            "cwd": parent.cwd,
            "cwd_display": parent.cwd_display or parent.cwd,
            "question": message,
            "trace": trace_flag,
            "parent_state": parent,   # warm ChatClients + prior data
            # New follow-up extras only; the runner merges with
            # parent_state.extra_roots so a follow-up always sees the
            # parent's reach plus whatever this turn added.
            "extra_roots": followup_body_extras,
            # Parallel pre-realpath display form of the body extras.
            "extra_roots_display": list(followup_body_extras),
        }
        future = app.state.executor.submit(
            _run_with_state, app, app.state.run_follow_up, child,
            runner_input,
        )
        del future

        return {
            "ok": True,
            "sid": child_sid,
            "parent_sid": sid,
            "status": "running",
            "status_url": f"/v1/consult/{child_sid}",
            "consultancy": _consultancy_dict(croot),
        }

    # ----------------------- accept ------------------------------ #
    # Consultancy review loop: mark the consultancy ACCEPTED (terminal)
    # once Claude is satisfied with the council's answer. Resolves to
    # the chain root and persists. Idempotent — accepting an already-
    # accepted consultancy is a no-op success.
    @app.post("/v1/consult/{sid}/accept")
    def accept(sid: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        cwd_hint = body.get("cwd")
        root = _resolve_consultancy_root(app, sid, cwd_hint)
        if root is None:
            raise HTTPException(
                status_code=404,
                detail=f"consultancy not found for sid {sid}; provide "
                       "cwd to load it from disk artifacts.",
            )
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(
                status_code=400, detail="note must be a string",
            )
        already = root.consultancy_status == CONSULTANCY_ACCEPTED
        root.consultancy_status = CONSULTANCY_ACCEPTED
        root.bump_activity()
        _persist_consultancy(root)
        return {
            "ok": True,
            "sid": sid,
            "root_sid": root.sid,
            "already_accepted": already,
            "consultancy": _consultancy_dict(root),
        }

    # ----------------------- close ------------------------------- #
    @app.post("/v1/consult/{sid}/close")
    def close(sid: str) -> dict:
        state = app.state.sessions.get(sid)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"session not found: {sid}",
            )
        if state.status == "running":
            raise HTTPException(
                status_code=409,
                detail="cannot close a running session; cancel via "
                       "the runner or wait for completion",
            )
        if state.closed:
            return {
                "ok": True, "sid": sid,
                "closed_at": state.closed_at,
                "already_closed": True,
            }
        _close_session(app, sid, reason="explicit")
        return {
            "ok": True, "sid": sid,
            "closed_at": state.closed_at,
            "already_closed": False,
        }

    # ----------------------- reopen ------------------------------ #
    # Restore a closed / evicted session to the in-memory pool.
    # Same disk-fallback path as follow-up's auto-reopen, exposed
    # explicitly for the inspect-before-iterate use case (the user
    # can `reopen` then `list-open` to confirm the chain before
    # firing a follow-up).
    @app.post("/v1/consult/{sid}/reopen")
    def reopen(sid: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        existing = app.state.sessions.get(sid)
        if existing is not None and not existing.closed:
            # Already warm — nothing to do beyond bumping activity.
            existing.bump_activity()
            return {
                "ok": True, "sid": sid,
                "source": "warm",
                "already_open": True,
            }
        if existing is not None and existing.closed:
            existing.closed = False
            existing.closed_at = None
            existing.bump_activity()
            log.info("reopened session %s in place (was closed)", sid)
            return {
                "ok": True, "sid": sid,
                "source": "in-memory-reopen",
                "already_open": False,
            }
        # Cold path: load from disk.
        cwd_hint = body.get("cwd")
        if not cwd_hint:
            raise HTTPException(
                status_code=400,
                detail=f"session {sid} not in memory; provide cwd "
                       "to load from disk artifacts.",
            )
        cwd_path = Path(cwd_hint).resolve()
        if not cwd_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"cwd does not exist: {cwd_path}",
            )
        state = _load_session_from_artifacts(sid, cwd_path)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"no artifact for sid {sid} under {cwd_path}.",
            )
        with app.state.sessions_lock:
            app.state.sessions[sid] = state
        return {
            "ok": True, "sid": sid,
            "source": "disk",
            "already_open": False,
            "cwd": str(cwd_path),
            "research_turns": len(state.research),
            "parent_sid": state.parent_sid,
        }

    # ----------------------- list open --------------------------- #
    @app.get("/v1/sessions/open")
    def list_open() -> dict:
        now = time.time()
        out = []
        # Snapshot under the lock so a concurrent close doesn't
        # produce a half-dropped entry.
        with app.state.sessions_lock:
            entries = list(app.state.sessions.values())
        for s in entries:
            if s.closed or s.status == "running":
                # Running sessions show in /v1/health.active_sessions;
                # closed sessions don't belong here.
                if s.status == "running":
                    pass  # show running ones too — caller can filter
                else:
                    continue
            out.append({
                "sid": s.sid,
                "parent_sid": s.parent_sid,
                "follow_up_sids": list(s.follow_up_sids),
                "status": s.status,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
                "last_activity_at": s.last_activity_at,
                "idle_seconds": now - s.last_activity_at,
                "question": s.question,
                "effort": s.effort,
            })
        # Sort newest-active first so the CLI's default view shows
        # what the user is most likely iterating on.
        out.sort(key=lambda d: -d["last_activity_at"])
        return {
            "open_sessions": out,
            "idle_timeout_s": app.state.idle_timeout_s,
        }

    # ----------------------- result ------------------------------ #
    @app.get("/v1/consult/{sid}/result")
    def result(sid: str) -> dict:
        # Prefer the in-memory session for cwd resolution; fall back to
        # the global registry if the entry was reaped.
        state = app.state.sessions.get(sid)
        if state is not None:
            cwd = Path(state.cwd)
        else:
            cwd = _find_session_cwd(sid)
            if cwd is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"session not found: {sid}",
                )
        sdir = storage.session_dir(cwd, sid)
        summary_path = sdir / storage.SUMMARY_FILENAME
        metadata_path = sdir / storage.METADATA_FILENAME
        if not summary_path.exists() or not metadata_path.exists():
            # 409 when the session is known and still running; 404
            # when there's no record of it at all.
            if state is not None and state.status == "running":
                raise HTTPException(
                    status_code=409,
                    detail="session not yet complete",
                )
            raise HTTPException(
                status_code=404,
                detail=f"artifacts missing for sid {sid}",
            )
        return _attach_consultancy(app, sid, {
            "sid": sid,
            "summary_markdown": summary_path.read_text(encoding="utf-8"),
            "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
        }, cwd_hint=str(cwd))

    # ----------------------- list -------------------------------- #
    @app.get("/v1/sessions")
    def list_sessions(cwd: str, limit: int = 50) -> dict:
        p = Path(cwd).resolve()
        if not p.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"cwd does not exist: {p}",
            )
        entries = sessions_index.list_recent(p, limit=limit)
        return {
            "cwd": str(p),
            "sessions": [asdict(e) for e in entries],
        }

    # ----------------------- config ------------------------------ #
    @app.get("/v1/config")
    def get_config(cwd: Optional[str] = None) -> dict:
        cwd_path = Path(cwd).resolve() if cwd else None
        if cwd_path is not None and not cwd_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"cwd does not exist: {cwd_path}",
            )
        cfg = cc.load_config(cwd_path)
        return _config_snapshot(cfg)

    return app


# ----------------------- helpers --------------------------------- #

def _run_with_state(app,
                    run_council: RunCouncilFn,
                    state: SessionState,
                    runner_input: dict) -> None:
    """Bridge into the runner so we can update SessionState on
    success / failure regardless of how the runner exits."""
    try:
        run_council(state, runner_input)
    except Exception as e:  # pragma: no cover — exercised in integration
        log.exception("council runner crashed: %s", e)
        state.status = "failed"
        state.error = f"runner crashed: {e}"
        state.finished_at = time.time()
    else:
        # Consultancy review loop: a successful council run flips the
        # consultancy root to ready_to_review so the skill knows there's
        # an answer to review. A failed run leaves the status untouched
        # (the skill reads the per-run status and handles it). Never
        # overrides a terminal (accepted) consultancy.
        if state.status == "completed":
            try:
                root = app.state.sessions.get(_consultancy_root_sid(state))
                if root is not None and \
                        root.consultancy_status not in _CONSULTANCY_TERMINAL:
                    root.consultancy_status = CONSULTANCY_READY
                    _persist_consultancy(root)
            except Exception:  # pragma: no cover — defensive
                log.exception(
                    "consultancy ready-flip failed for sid=%s", state.sid,
                )
    finally:
        # Best-effort: refresh the index entry's status + duration so
        # /v1/sessions reflects terminal state without re-reading the
        # full metadata.json.
        try:
            cwd_path = Path(state.cwd)
            sessions_index.append(
                cwd_path,
                sessions_index.SessionEntry(
                    session_id=state.sid,
                    created=time.strftime(
                        "%Y-%m-%dT%H:%M:%S",
                        time.localtime(state.started_at)),
                    question=state.question,
                    topology=state.topology,
                    effort=state.effort,
                    status=state.status,
                    duration_seconds=(state.finished_at or time.time())
                                     - state.started_at,
                ),
            )
        except Exception as e:  # pragma: no cover
            log.warning("sessions_index update failed: %s", e)


def _load_session_from_artifacts(sid: str,
                                 cwd: Path) -> Optional[SessionState]:
    """Reconstruct a SessionState from on-disk artifacts.

    Reads ``<cwd>/.claude-hooks/consultants/<sid>/metadata.json`` and
    extracts plan / research / critique from the structured ``turns``
    list, plus ``final_answer`` / ``models`` / ``parent_sid`` from
    top-level fields. Returns ``None`` when the directory or
    metadata file isn't present.

    What CANNOT be recovered from disk:
    - ``_chat_clients`` (runtime-only) — caller's first follow-up
      against a reopened sid will rebuild cold ChatClients. The
      ``_probed_think`` / ``_unsupported_think`` caches re-warm on
      that first call.
    - ``follow_up_sids`` (no global child-index in storage). The
      chain still works through ``parent_sid`` pointers; this is a
      lossy field on reopen.

    The reconstructed state has ``closed=False`` and
    ``last_activity_at=now`` so the reaper doesn't immediately
    re-evict it.
    """
    sdir = storage.session_dir(cwd, sid)
    md_path = sdir / storage.METADATA_FILENAME
    if not md_path.is_file():
        return None
    try:
        meta = json.loads(md_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("metadata.json for %s is not valid JSON", sid)
        return None

    turns = meta.get("turns") or []
    # Walk turns to reconstruct the role-specific fields. ``plan``
    # is the planner's content; ``research`` accumulates every
    # researcher turn (one per fan-out lane plus any re-routes);
    # ``critique`` is the critic's content (last one wins if there
    # were re-routes — the most recent critique is what the
    # synthesizer would have seen).
    plan_text = ""
    research_list: list[str] = []
    critique_text: Optional[str] = None
    for turn in turns:
        role = turn.get("role")
        content = turn.get("content") or ""
        if role == "planner" and not plan_text:
            plan_text = content
        elif role == "researcher":
            if content.strip():
                research_list.append(content)
        elif role == "critic":
            critique_text = content

    # plan_items is parsed from plan text so the original fan-out
    # router decision is reproducible (though follow-ups don't
    # fan out, so this is mostly informational).
    try:
        from consultants.engine import council
        plan_items = list(council.parse_plan_items(plan_text))
    except Exception:  # pragma: no cover — defensive
        plan_items = []

    # progress is purely cosmetic at this point (consultation is
    # done); set every model role to "done" for consistent shape.
    models = meta.get("models") or {}
    progress = {role: "done" for role in models}

    started = float(meta.get("started_at") or 0.0)
    if not started:
        # Fall back to created (ISO) — convert to epoch for the
        # SessionState.started_at field; failure-tolerant.
        try:
            from datetime import datetime
            created_iso = meta.get("created") or ""
            if created_iso:
                started = datetime.fromisoformat(
                    created_iso.replace("Z", "+00:00")
                ).timestamp()
        except Exception:
            pass
    duration = float(meta.get("duration_seconds") or 0.0)
    finished = (started + duration) if started and duration else None

    # v1.1: probe for transcript.db and reconstruct per-role message
    # threads. Missing / corrupt / v1.0-shape (no .db) gives back
    # (None, None) — Phase 5's follow-up branch on this and falls
    # back to today's turn-content reconstruction.
    role_messages = None
    role_lane_messages = None
    db_path = sdir / storage.TRANSCRIPT_DB_FILENAME
    if db_path.is_file():
        try:
            from consultants.engine.recorder import load_role_messages
            role_messages, role_lane_messages = load_role_messages(db_path)
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "transcript.db reconstruction failed for %s: %s; "
                "falling back to turn-content view", sid, exc,
            )

    state = SessionState(
        sid=sid,
        cwd=str(cwd),
        question=meta.get("question") or "",
        effort=meta.get("effort") or "medium",
        topology=meta.get("topology") or "council",
        status=meta.get("status") or "completed",
        started_at=started or time.time(),
        finished_at=finished,
        error=meta.get("error"),
        progress=progress,
        plan=plan_text,
        plan_items=plan_items,
        research=research_list,
        critique=critique_text,
        final_answer=meta.get("final_answer") or "",
        models=dict(models),
        parent_sid=meta.get("parent_sid"),
        # Consultancy anchor recovered from metadata (None on pre-
        # review-loop sessions → resolver treats the sid as its own
        # root). Lets a disk-reopened followup resolve to the root
        # whose dir holds consultancy.json.
        root_sid=meta.get("root_sid"),
        follow_up_sids=[],   # NOT recoverable; lossy on reopen
        closed=False,
        closed_at=None,
        last_activity_at=time.time(),
        _chat_clients=None,  # rebuilt cold on first follow-up
        _role_messages=role_messages,
        _role_lane_messages=role_lane_messages,
    )
    # If this session is its own consultancy root, hydrate the review-
    # loop status from consultancy.json (lives in the root's dir).
    # Children resolve their root separately; their own fields stay at
    # defaults (never authoritative).
    if (state.root_sid or state.sid) == state.sid:
        _hydrate_consultancy(state)
    log.info(
        "reopened session %s from disk (cwd=%s, %d research turns, "
        "%s)",
        sid, cwd, len(research_list),
        ("with transcript.db threads"
         if role_messages or role_lane_messages
         else "no transcript.db — turn-content fallback"),
    )
    return state


def _resolve_parent_for_follow_up(
    app, sid: str, cwd_hint: Optional[str],
) -> SessionState:
    """Return a parent SessionState usable for a follow-up.

    Resolution order:
      1. In memory and not closed → use as-is.
      2. In memory and closed → reopen in place (clear ``closed``
         flag, bump ``last_activity_at``). ``_chat_clients`` was
         released on close; the runner's cold-client fallback
         rebuilds them.
      3. Not in memory but ``cwd_hint`` given → load from disk via
         ``_load_session_from_artifacts``; register in
         ``app.state.sessions`` so subsequent calls are warm.
      4. Otherwise raise ``HTTPException`` so the route can return
         the right status code.
    """
    state = app.state.sessions.get(sid)
    if state is not None:
        if state.status == "running":
            from fastapi import HTTPException
            raise HTTPException(
                status_code=409,
                detail=f"parent session {sid} is still running; "
                       "wait for completion before following up.",
            )
        if state.closed:
            # In-memory reopen — fields are still here, just flip
            # the closed flag back. _chat_clients was released; the
            # follow-up runner will build cold clients.
            state.closed = False
            state.closed_at = None
            state.bump_activity()
            log.info("reopened session %s in place (was closed)", sid)
        return state

    # Not in memory — disk fallback.
    if not cwd_hint:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"session {sid} not in memory; provide cwd to "
                   "load it from disk artifacts.",
        )
    cwd_path = Path(cwd_hint).resolve()
    if not cwd_path.is_dir():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"cwd does not exist: {cwd_path}",
        )
    reopened = _load_session_from_artifacts(sid, cwd_path)
    if reopened is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail=f"no artifact for sid {sid} under {cwd_path}.",
        )
    with app.state.sessions_lock:
        app.state.sessions[sid] = reopened
    return reopened


# ----------------------- consultancy review loop ---------------- #
# Engine-owned status machine layered ABOVE the per-run status. A
# consultancy is the chain rooted at the first ``ask`` (root_sid);
# these helpers read/advance/persist the root's status. See
# SessionState's consultancy fields + storage.consultancy.json.

CONSULTANCY_IN_PROGRESS = "in_progress"
CONSULTANCY_READY = "ready_to_review"
CONSULTANCY_ACCEPTED = "accepted"
CONSULTANCY_AWAITING = "awaiting_approval"
_CONSULTANCY_TERMINAL = frozenset({CONSULTANCY_ACCEPTED})
# Structured reason the follow-up route returns when the cap is hit
# without an override — the skill keys on this to stop and ask the user.
FOLLOWUP_LIMIT_REACHED = "followup_limit_reached"


def _consultancy_root_sid(state: "SessionState") -> str:
    """The consultancy anchor for a session: its ``root_sid`` if set
    (warm sessions + followups + disk-recovered children), else the
    session's own sid (a fresh ask is its own root)."""
    return state.root_sid or state.sid


def _consultancy_dict(root: "SessionState") -> dict:
    """Serialisable snapshot of a root session's consultancy state —
    persisted to consultancy.json and surfaced by the read routes."""
    return {
        "root_sid": root.sid,
        "status": root.consultancy_status,
        "followup_count": root.followup_count,
        "max_followups": root.max_followups,
        "extra_granted": root.extra_granted,
        "effective_cap": root.max_followups + root.extra_granted,
        "child_sids": list(root.consultancy_children),
        "updated_at": time.time(),
    }


def _persist_consultancy(root: "SessionState") -> None:
    """Write-through the root's consultancy state to consultancy.json.
    Soft-fail: a sidecar write must never break a request."""
    try:
        storage.write_consultancy(
            Path(root.cwd), root.sid, _consultancy_dict(root),
        )
    except Exception:  # pragma: no cover — defensive
        log.exception("write_consultancy failed for root=%s", root.sid)


def _hydrate_consultancy(root: "SessionState") -> None:
    """Populate a (disk-reopened) root's consultancy fields from
    consultancy.json. No-op when the sidecar is absent — the caller's
    defaults stand. Idempotent."""
    disk = storage.read_consultancy(Path(root.cwd), root.sid)
    if not disk:
        return
    root.consultancy_status = disk.get("status") or root.consultancy_status
    try:
        root.followup_count = int(disk.get("followup_count") or 0)
        root.max_followups = int(disk.get("max_followups") or 0)
        root.extra_granted = int(disk.get("extra_granted") or 0)
    except (TypeError, ValueError):
        pass
    children = disk.get("child_sids")
    if isinstance(children, list):
        root.consultancy_children = [str(c) for c in children]


def _resolve_consultancy_root(
    app, sid: str, cwd_hint: Optional[str] = None,
) -> Optional["SessionState"]:
    """Return the live ROOT SessionState for the consultancy ``sid``
    belongs to, reopening it from disk when necessary.

    Find ``sid`` (in memory, or via disk reopen using ``cwd_hint``);
    the root is ``state.root_sid`` (or the sid itself when unset). A
    warm root is authoritative and returned as-is. A cold root is
    reopened from disk and its consultancy fields hydrated. Returns
    None when neither the session nor its root can be located.
    """
    state = app.state.sessions.get(sid)
    if state is None and cwd_hint:
        cwd_path = Path(cwd_hint).resolve()
        if cwd_path.is_dir():
            state = _load_session_from_artifacts(sid, cwd_path)
            if state is not None:
                with app.state.sessions_lock:
                    app.state.sessions.setdefault(sid, state)
    if state is None:
        return None
    root_sid = _consultancy_root_sid(state)
    root = app.state.sessions.get(root_sid)
    if root is not None:
        return root
    # Root not warm. If sid IS its own root, reuse the (already disk-
    # hydrated) state; else reopen the root from disk and hydrate.
    if root_sid == state.sid:
        return state
    root = _load_session_from_artifacts(root_sid, Path(state.cwd))
    if root is None:
        return None
    _hydrate_consultancy(root)
    with app.state.sessions_lock:
        app.state.sessions.setdefault(root_sid, root)
    return root


def _attach_consultancy(app, sid: str, payload: dict,
                        cwd_hint: Optional[str] = None) -> dict:
    """Attach the consultancy snapshot to a response payload under the
    ``consultancy`` key (resolving the root). Tolerant: leaves the
    payload unchanged when the root can't be located."""
    try:
        root = _resolve_consultancy_root(app, sid, cwd_hint)
        if root is not None:
            payload["consultancy"] = _consultancy_dict(root)
    except Exception:  # pragma: no cover — defensive
        log.exception("_attach_consultancy failed for sid=%s", sid)
    return payload


def _close_session(app, sid: str, *, reason: str) -> None:
    """Mark a session closed and release its warm ChatClients.

    The SessionState entry stays in ``app.state.sessions`` for the
    grace period (until reaper eviction) so that polls + result
    fetches against the closed sid still work; the warm engine
    handles get released immediately so we don't pin sockets.

    ``reason`` is logged so we can tell explicit closes from
    idle reaps in production.
    """
    state = app.state.sessions.get(sid)
    if state is None or state.closed:
        return
    state.closed = True
    state.closed_at = time.time()
    # Drop ChatClient handles. They hold per-instance memo dicts
    # but no open sockets (urllib opens per-call), so this is just
    # a memory release.
    state._chat_clients = None
    state._coder_chat_clients_by_model = None
    log.info("closed session %s (reason=%s)", sid, reason)


def _maybe_start_store_reaper(app, cfg, ollama_base_url: str) -> None:
    """Spawn the M14 :class:`StoreReaperThread` when config opts in.

    Gates (any False short-circuits to no-op):

    - ``cfg.store`` exists.
    - ``cfg.store.enabled`` is True.
    - Either ``cfg.store.ttl.enabled`` or
      ``cfg.store.distillation.enabled`` is True.
    - A provider can be loaded for ``cfg.store.backend``.

    On success the reaper is stashed at ``app.state.store_reaper`` so
    the shutdown hook can join it. Failures are logged + non-fatal
    — the app still comes up without a reaper.
    """
    store_cfg = getattr(cfg, "store", None)
    if store_cfg is None or not getattr(store_cfg, "enabled", False):
        log.info(
            "store reaper: cfg.store.enabled is False "
            "(M14 default is True; admin set it false); not starting",
        )
        return
    ttl_enabled = bool(getattr(getattr(store_cfg, "ttl", None), "enabled", False))
    dist_enabled = bool(getattr(
        getattr(store_cfg, "distillation", None), "enabled", False,
    ))
    if not ttl_enabled and not dist_enabled:
        log.info(
            "store reaper: neither store.ttl nor store.distillation "
            "is enabled; not starting",
        )
        return

    # Reuse the same backend loaders the consultants store uses so
    # the daemon and the per-session stores write through the same
    # connection class. The reaper-side provider is independent of
    # any specific session — the reaper sweeps across sids.
    backend = (getattr(store_cfg, "backend", "memory") or "memory").lower()
    if backend == "memory":
        log.info(
            "store reaper: backend=memory is per-process; "
            "no cross-session sweep needed — not starting",
        )
        return
    try:
        from consultants.engine.store import (
            _load_pgvector, _load_sqlite_vec, make_consultants_store,
        )
    except Exception:
        log.exception("store reaper: store module import failed")
        return
    if backend == "pgvector":
        provider = _load_pgvector(store_cfg)
    elif backend == "sqlite_vec":
        provider = _load_sqlite_vec(store_cfg)
    else:
        log.warning(
            "store reaper: unknown backend %r; not starting", backend,
        )
        return
    if provider is None:
        log.warning(
            "store reaper: provider load failed for backend=%r; "
            "not starting", backend,
        )
        return

    # Build a long-lived store for the project-namespace writes
    # ``write_distilled_summary`` performs. Bypass the effort gate
    # (the reaper is daemon-side, not session-side) by passing
    # ``effort=None``.
    try:
        store = make_consultants_store(
            cfg, sid="<reaper>", effort=None,
        )
    except Exception:
        log.exception("store reaper: make_consultants_store raised")
        return
    if store is None:
        log.warning(
            "store reaper: make_consultants_store returned None; "
            "not starting",
        )
        return

    distiller = None
    if dist_enabled:
        try:
            from consultants.engine.distillation import Distiller
            distiller = Distiller(
                store_cfg.distillation, ollama_base_url,
            )
        except Exception:
            log.exception(
                "store reaper: Distiller init failed; sweep will "
                "delete-only, no distillation",
            )

    from consultants.engine.store_reaper import StoreReaperThread
    reaper = StoreReaperThread(
        store_cfg=store_cfg,
        provider=provider,
        distiller=distiller,
        store=store,
    )
    reaper.start()
    app.state.store_reaper = reaper
    log.info(
        "store reaper: started (backend=%s, ttl=%s, distill=%s, "
        "interval=%.0fs)",
        backend, ttl_enabled, dist_enabled, reaper.interval_seconds,
    )


def _start_idle_reaper(app) -> None:
    """Spawn the background thread that closes idle sessions.

    Loop: every ``reaper_interval_s`` scan
    ``app.state.sessions``; for each non-closed entry whose
    ``last_activity_at`` is older than ``idle_timeout_s``, mark
    closed via ``_close_session``. Closed entries linger for
    one full timeout window after closure (so polls returning the
    final state still work) before being evicted from the dict.

    The thread is a daemon — it won't block process shutdown.
    Stops cleanly when ``app.state.reaper_stop`` is set (used by
    tests to deterministically join).
    """
    def _loop() -> None:
        log.info(
            "idle reaper started (timeout=%.0fs interval=%.0fs)",
            app.state.idle_timeout_s, app.state.reaper_interval_s,
        )
        while not app.state.reaper_stop.wait(app.state.reaper_interval_s):
            try:
                _reaper_tick(app)
            except Exception as e:  # pragma: no cover — defensive
                log.exception("reaper tick crashed: %s", e)
        log.info("idle reaper stopped")

    t = threading.Thread(target=_loop, name="consultants-reaper",
                         daemon=True)
    t.start()
    app.state.reaper_thread = t


def _reaper_tick(app) -> None:
    """One pass over ``app.state.sessions``. Idle non-closed
    sessions get closed; long-closed ones get evicted entirely."""
    now = time.time()
    timeout = float(app.state.idle_timeout_s)
    # Two-stage. Snapshot under the lock so a concurrent follow-up
    # doesn't see a half-mutated dict; act outside the lock.
    with app.state.sessions_lock:
        entries = list(app.state.sessions.items())
    to_close: list[str] = []
    to_evict: list[str] = []
    for sid, s in entries:
        if s.status == "running":
            continue   # never reap a running consultation
        idle = now - s.last_activity_at
        if not s.closed and idle > timeout:
            to_close.append(sid)
        # Evict closed entries one timeout-window after closure so
        # late polls still work for a grace period.
        if s.closed and s.closed_at and (now - s.closed_at) > timeout:
            to_evict.append(sid)
    for sid in to_close:
        _close_session(app, sid, reason="idle")
    if to_evict:
        with app.state.sessions_lock:
            for sid in to_evict:
                app.state.sessions.pop(sid, None)
        for sid in to_evict:
            log.info("evicted closed session %s after grace period", sid)


def _load_session_from_disk(sid: str) -> Optional[dict]:
    """Locate a session on disk by scanning the global registry. We
    don't track every project the service has ever served, so this is
    best-effort: the CLI normally passes the right cwd via /v1/sessions."""
    return None  # disk fallback only matters when we know the cwd


def _find_session_cwd(sid: str) -> Optional[Path]:
    """Without a cwd hint we can't locate a session, so we return None
    here. The CLI's /result call always has the cwd in scope and will
    not actually rely on this path."""
    return None


def _config_snapshot(cfg: cc.ConsultantsConfig) -> dict:
    return {
        "topology": cfg.topology,
        "effort": cfg.effort,
        "effort_budget": cfg.effort_budget,
        "max_followups": cfg.max_followups,
        "allow_extra": cfg.allow_extra,
        "service": {
            "mode": cfg.service.mode,
            "http_port": cfg.service.http_port,
        },
        "roles": {
            r: {
                "enabled": cfg.roles[r].enabled,
                "model": cfg.roles[r].model,
                "ctx_max": cfg.roles[r].ctx_max,
                "ctx_max_explicit": cfg.roles[r].ctx_max_explicit,
            }
            for r in cc.ROLES
        },
        "mandatory_roles": sorted(cc.MANDATORY_ROLES),
        "valid_efforts": sorted(cc.EFFORT_BUDGETS),
        "valid_service_modes": sorted(cc.VALID_SERVICE_MODES),
    }
