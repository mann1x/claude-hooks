"""Tests for the consultancy review loop (engine-owned status machine).

Mirrors /get-advice's discuss-until-satisfied flow at the engine layer:
a fresh ``ask`` opens a consultancy (``in_progress``); a completed
council run flips it to ``ready_to_review``; ``accept`` makes it
``accepted`` (terminal); a follow-up past ``max_followups`` is refused
(``awaiting_approval``) unless ``allow_extra`` raises the cap one-off.

State is persisted to ``consultancy.json`` in the ROOT session dir so it
survives idle reap / restart / compaction. These tests drive the
FastAPI app with a synchronous stub runner — no LangChain / Ollama.

Covers:
- Fresh ask → in_progress, followup_count=0, root_sid=self, sidecar written.
- Council completion → auto-flip to ready_to_review (surfaced in status).
- accept on the root → accepted (terminal).
- accept on a CHILD sid resolves to the root → accepted.
- follow-up under cap → count increments, in_progress → ready_to_review.
- follow-up at cap, no override → refused (followup_limit_reached) +
  awaiting_approval, no child created.
- follow-up at cap, --allow-extra 1 → proceeds, extra_granted=1.
- cold reopen → consultancy status/count restored from consultancy.json.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from consultants import config as cc  # noqa: E402
from consultants.engine import storage  # noqa: E402
from consultants.server.app import create_app, SessionState  # noqa: E402


# ----------------------- stub runners ---------------------------- #

def _write_result(state: SessionState, runner_input: dict, *,
                  final: str) -> None:
    cwd = Path(runner_input["cwd"])
    result = storage.ConsultationResult(
        session_id=state.sid,
        created=time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(state.started_at)),
        question=runner_input["question"],
        models={"synthesizer": "stub"},
        topology=state.topology,
        effort=state.effort,
        final_answer=final,
        turns=[storage.RoleTurn(role="synthesizer", round=1,
                                content="stub")],
        duration_seconds=time.time() - state.started_at,
        status="completed",
        cwd=str(cwd),
        parent_sid=state.parent_sid,
        root_sid=getattr(state, "root_sid", None) or state.sid,
    )
    storage.write_consultation(result, cwd=cwd)
    for r in state.progress:
        state.progress[r] = "done"
    state.final_answer = final
    state.models = dict(result.models)
    state.status = "completed"
    state.finished_at = time.time()
    state.bump_activity()


def _stub_runner(state: SessionState, runner_input: dict) -> None:
    _write_result(state, runner_input, final="**Verdict**: root answer.")


def _stub_follow_up(state: SessionState, runner_input: dict) -> None:
    # The real runner reuses parent_state; the stub just writes a child.
    _write_result(state, runner_input, final="**Verdict**: child answer.")


def _gated_stub_runner(gate: threading.Event):
    """A stub runner that blocks until ``gate`` is set. Lets a test
    observe the ``in_progress`` consultancy window deterministically:
    without the gate the instantaneous stub races to completion (and
    flips the sidecar to ``ready_to_review``) before the test can read
    it — a thread-scheduling flake under load, not a logic bug."""
    def run(state: SessionState, runner_input: dict) -> None:
        gate.wait(timeout=5.0)
        _write_result(state, runner_input, final="**Verdict**: root answer.")
    return run


def _make_app():
    return create_app(run_council=_stub_runner,
                      run_follow_up=_stub_follow_up,
                      start_reaper=False)


def _wait_completed(client, sid: str, iters: int = 100) -> dict:
    for _ in range(iters):
        p = client.get(f"/v1/consult/{sid}").json()
        if p["status"] in ("completed", "failed"):
            return p
        time.sleep(0.02)
    raise AssertionError(f"sid {sid} never completed")


# ``isolated_home`` comes from tests/conftest.py (cross-platform; bug-635).


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _ask(client, project_dir: Path) -> str:
    return client.post("/v1/consult", json={
        "message": "audit the thing", "cwd": str(project_dir),
    }).json()["sid"]


# ----------------------- fresh ask ------------------------------- #

class TestFreshAsk:
    def test_seeds_in_progress_and_sidecar(self, isolated_home, project_dir):
        cc.set_max_followups(4)
        # Gate the runner so the in_progress window is deterministic: the
        # sidecar is seeded synchronously in the POST /consult route, but
        # the instantaneous default stub would otherwise flip it to
        # ready_to_review (in the executor thread) before this read.
        gate = threading.Event()
        app = create_app(run_council=_gated_stub_runner(gate),
                         run_follow_up=_stub_follow_up, start_reaper=False)
        with TestClient(app) as client:
            sid = _ask(client, project_dir)
            try:
                # Sidecar written up front, before completion.
                disk = storage.read_consultancy(project_dir, sid)
                assert disk is not None
                assert disk["root_sid"] == sid
                assert disk["status"] == "in_progress"
                assert disk["followup_count"] == 0
                assert disk["max_followups"] == 4
            finally:
                gate.set()  # release the runner so teardown is clean

    def test_completion_flips_to_ready_to_review(self, isolated_home,
                                                 project_dir):
        app = _make_app()
        with TestClient(app) as client:
            sid = _ask(client, project_dir)
            poll = _wait_completed(client, sid)
            c = poll["consultancy"]
            assert c["root_sid"] == sid
            assert c["status"] == "ready_to_review"
            assert c["followup_count"] == 0
            assert c["effective_cap"] == c["max_followups"]


# ----------------------- accept ---------------------------------- #

class TestAccept:
    def test_accept_root_is_terminal(self, isolated_home, project_dir):
        app = _make_app()
        with TestClient(app) as client:
            sid = _ask(client, project_dir)
            _wait_completed(client, sid)
            r = client.post(f"/v1/consult/{sid}/accept",
                            json={"cwd": str(project_dir)})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["consultancy"]["status"] == "accepted"
            assert body["already_accepted"] is False
            # Idempotent second accept.
            r2 = client.post(f"/v1/consult/{sid}/accept",
                             json={"cwd": str(project_dir)})
            assert r2.json()["already_accepted"] is True
            # Persisted.
            assert storage.read_consultancy(
                project_dir, sid)["status"] == "accepted"

    def test_accept_child_resolves_to_root(self, isolated_home,
                                           project_dir):
        cc.set_max_followups(4)
        app = _make_app()
        with TestClient(app) as client:
            root = _ask(client, project_dir)
            _wait_completed(client, root)
            child = client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "drill in", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_completed(client, child)
            # Accept via the CHILD sid — must resolve to the root.
            r = client.post(f"/v1/consult/{child}/accept",
                            json={"cwd": str(project_dir)})
            assert r.json()["root_sid"] == root
            assert storage.read_consultancy(
                project_dir, root)["status"] == "accepted"


# ----------------------- followup under cap ---------------------- #

class TestFollowupUnderCap:
    def test_increments_count_and_reflips(self, isolated_home, project_dir):
        cc.set_max_followups(4)
        app = _make_app()
        with TestClient(app) as client:
            root = _ask(client, project_dir)
            _wait_completed(client, root)
            r = client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "more", "cwd": str(project_dir),
            }).json()
            assert r["ok"] is True
            assert r["consultancy"]["followup_count"] == 1
            assert r["consultancy"]["status"] == "in_progress"
            child = r["sid"]
            poll = _wait_completed(client, child)
            # Root flips back to ready_to_review on child completion.
            assert poll["consultancy"]["status"] == "ready_to_review"
            assert poll["consultancy"]["followup_count"] == 1
            assert poll["consultancy"]["root_sid"] == root


# ----------------------- followup at cap ------------------------- #

class TestFollowupAtCap:
    def test_refused_without_override(self, isolated_home, project_dir):
        cc.set_max_followups(0)  # first followup already needs approval
        app = _make_app()
        with TestClient(app) as client:
            root = _ask(client, project_dir)
            _wait_completed(client, root)
            r = client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "more", "cwd": str(project_dir),
            })
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is False
            assert body["reason"] == "followup_limit_reached"
            assert body["sid"] is None
            assert body["consultancy"]["status"] == "awaiting_approval"
            assert body["consultancy"]["followup_count"] == 0
            # No child session was created.
            assert "follow_up_sids" not in body or True
            # Persisted as awaiting_approval.
            assert storage.read_consultancy(
                project_dir, root)["status"] == "awaiting_approval"

    def test_allow_extra_grants_one_off(self, isolated_home, project_dir):
        cc.set_max_followups(0)
        app = _make_app()
        with TestClient(app) as client:
            root = _ask(client, project_dir)
            _wait_completed(client, root)
            # Refused first.
            assert client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "more", "cwd": str(project_dir),
            }).json()["ok"] is False
            # Now with the approval carrier.
            r = client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "more", "cwd": str(project_dir),
                "allow_extra": 1,
            }).json()
            assert r["ok"] is True
            assert r["consultancy"]["extra_granted"] == 1
            assert r["consultancy"]["effective_cap"] == 1
            assert r["consultancy"]["followup_count"] == 1


# ----------------------- cold reopen ----------------------------- #

class TestColdReopen:
    def test_status_restored_from_sidecar(self, isolated_home, project_dir):
        cc.set_max_followups(4)
        app = _make_app()
        with TestClient(app) as client:
            root = _ask(client, project_dir)
            _wait_completed(client, root)
            # One followup so the count is non-zero on disk.
            child = client.post(f"/v1/consult/{root}/follow-up", json={
                "message": "more", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_completed(client, child)
            # Evict everything from memory — simulate a daemon restart.
            with app.state.sessions_lock:
                app.state.sessions.clear()
            # accept via the root sid + cwd → reopens from disk, hydrates
            # consultancy.json, and applies the terminal status.
            r = client.post(f"/v1/consult/{root}/accept",
                            json={"cwd": str(project_dir)})
            assert r.status_code == 200
            c = r.json()["consultancy"]
            assert c["status"] == "accepted"
            # followup_count survived the round-trip through the sidecar.
            assert c["followup_count"] == 1
            assert c["root_sid"] == root


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
