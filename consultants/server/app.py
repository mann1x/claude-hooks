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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

from consultants import config as cc
from consultants.engine import sessions_index, storage

log = logging.getLogger("consultants.server")


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
    # v1.8+: extra allowed directories for the tool sandbox. Set on
    # creation from the request body's ``extra_roots`` field (already
    # auto-unioned with settings-file discovery by the HTTP layer).
    # Follow-ups inherit this list and may extend it; ``run_follow_up``
    # in the runner merges the parent's roots with the follow-up's.
    extra_roots: list[str] = field(default_factory=list)
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
               start_reaper: bool = True) -> "FastAPI":
    """Build a FastAPI app. ``run_council`` is the in-process
    council executor; if None, the app comes up but ``/v1/consult``
    returns 503 (useful for tests that only exercise the read-side
    routes)."""
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
        from claude_hooks.allowed_roots import discover_allowed_roots
        body_extras = body.get("extra_roots") or []
        if not isinstance(body_extras, list) or not all(
            isinstance(x, str) for x in body_extras
        ):
            raise HTTPException(
                status_code=400,
                detail="extra_roots must be a list of strings",
            )
        discovered = discover_allowed_roots(
            str(cwd_path), add_dirs=body_extras,
        )
        # discover_allowed_roots prepends the primary cwd; the runner
        # wants extras only.
        session_extra_roots: list[str] = list(discovered[1:])

        sid = _new_sid()
        state = SessionState(
            sid=sid,
            cwd=str(cwd_path),
            question=question,
            effort=cfg.effort,
            topology=cfg.topology,
            progress={r: "pending" for r in cc.enabled_roles(cfg)},
            extra_roots=session_extra_roots,
        )
        with app.state.sessions_lock:
            app.state.sessions[sid] = state

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
            "question": question,
            "trace": trace_flag,
            "extra_roots": session_extra_roots,
        }

        # Hand off to the executor. The runner mutates ``state`` and
        # writes the on-disk artifacts; we just track completion.
        future = app.state.executor.submit(
            _run_with_state, app.state.run_council, state, runner_input,
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
            return state.public_dict()
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
        child = SessionState(
            sid=child_sid,
            cwd=parent.cwd,
            question=message,
            effort=cfg.effort,
            topology=parent.topology,
            progress=child_progress,
            parent_sid=sid,
            # Stored extra_roots = parent's + this follow-up's, merged
            # in order with dedup. The runner does the same merge for
            # the in-flight executor; we persist it so disk-reopen of
            # the child surfaces the full set.
            extra_roots=_merge_extra_roots(
                parent.extra_roots, followup_body_extras,
            ),
        )
        with app.state.sessions_lock:
            app.state.sessions[child_sid] = child
            parent.follow_up_sids.append(child_sid)
        parent.bump_activity()  # iterating; defer reaper

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
            "question": message,
            "trace": trace_flag,
            "parent_state": parent,   # warm ChatClients + prior data
            # New follow-up extras only; the runner merges with
            # parent_state.extra_roots so a follow-up always sees the
            # parent's reach plus whatever this turn added.
            "extra_roots": followup_body_extras,
        }
        future = app.state.executor.submit(
            _run_with_state, app.state.run_follow_up, child, runner_input,
        )
        del future

        return {
            "sid": child_sid,
            "parent_sid": sid,
            "status": "running",
            "status_url": f"/v1/consult/{child_sid}",
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
        return {
            "sid": sid,
            "summary_markdown": summary_path.read_text(encoding="utf-8"),
            "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
        }

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

def _run_with_state(run_council: RunCouncilFn,
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
        follow_up_sids=[],   # NOT recoverable; lossy on reopen
        closed=False,
        closed_at=None,
        last_activity_at=time.time(),
        _chat_clients=None,  # rebuilt cold on first follow-up
        _role_messages=role_messages,
        _role_lane_messages=role_lane_messages,
    )
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
    log.info("closed session %s (reason=%s)", sid, reason)


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
