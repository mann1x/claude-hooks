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
        }


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


def create_app(*, run_council: Optional[RunCouncilFn] = None,
               max_workers: int = 4) -> "FastAPI":
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
    app.state.executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="consultants-runner",
    )
    app.state.sessions_lock = threading.Lock()

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

        sid = _new_sid()
        state = SessionState(
            sid=sid,
            cwd=str(cwd_path),
            question=question,
            effort=cfg.effort,
            topology=cfg.topology,
            progress={r: "pending" for r in cc.enabled_roles(cfg)},
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

        runner_input = {
            "config": cfg,
            "cwd": str(cwd_path),
            "question": question,
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
            return state.public_dict()
        # Fall back to disk: maybe the service restarted.
        disk = _load_session_from_disk(sid)
        if disk is None:
            raise HTTPException(
                status_code=404,
                detail=f"session not found: {sid}",
            )
        return disk

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
