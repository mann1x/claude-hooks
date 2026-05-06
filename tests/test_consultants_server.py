"""Tests for ``consultants.server.app`` via FastAPI's TestClient.

Uses a stub runner so no LangChain / Ollama / external state is
touched. The runner mutates the SessionState in place and writes the
three artifact files synchronously so the tests can inspect both
the in-memory and on-disk views.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from consultants import config as cc
from consultants.engine import storage
from consultants.server.app import create_app, SessionState


# ----------------------- stub runner ----------------------------- #

def make_stub_runner(*, fail: bool = False, sleep: float = 0.0):
    """Return a synchronous stub runner that writes legitimate
    artifacts. ``fail=True`` simulates a graph crash."""

    def run_council(state: SessionState, runner_input: dict) -> None:
        if sleep > 0:
            time.sleep(sleep)
        if fail:
            state.status = "failed"
            state.error = "stub crash"
            state.finished_at = time.time()
            return
        cwd = Path(runner_input["cwd"])
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(state.started_at)),
            question=runner_input["question"],
            models={"planner": "stub", "researcher": "stub",
                    "critic": "stub", "synthesizer": "stub"},
            topology=state.topology,
            effort=state.effort,
            final_answer="**Verdict**: stub answer.",
            turns=[
                storage.RoleTurn(role="synthesizer", round=1,
                                 content="stub", prompt_tokens=10,
                                 completion_tokens=5),
            ],
            duration_seconds=time.time() - state.started_at,
            status="completed",
            cwd=str(cwd),
            total_prompt_tokens=10,
            total_completion_tokens=5,
        )
        storage.write_consultation(result, cwd=cwd)
        for r in state.progress:
            state.progress[r] = "done"
        state.status = "completed"
        state.finished_at = time.time()

    return run_council


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


# ----------------------- /v1/health ------------------------------ #

class TestHealth:
    def test_no_runner(self):
        app = create_app(run_council=None)
        with TestClient(app) as client:
            r = client.get("/v1/health")
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert data["runner_available"] is False

    def test_with_runner(self):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/health")
            assert r.json()["runner_available"] is True


# ----------------------- /v1/consult ----------------------------- #

class TestConsult:
    def test_503_when_no_runner(self, project_dir):
        app = create_app(run_council=None)
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            })
            assert r.status_code == 503

    def test_400_on_empty_message(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "", "cwd": str(project_dir),
            })
            assert r.status_code == 400

    def test_400_on_bad_cwd(self, isolated_home, tmp_path):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "q", "cwd": str(tmp_path / "missing"),
            })
            assert r.status_code == 400

    def test_400_on_bad_effort(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
                "effort": "nonsense",
            })
            assert r.status_code == 400

    def test_returns_sid_and_status_url(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "audit the function", "cwd": str(project_dir),
            })
            assert r.status_code == 200
            data = r.json()
            assert data["sid"].startswith("csl-")
            assert data["status_url"] == f"/v1/consult/{data['sid']}"
            assert data["status"] == "running"


# ----------------------- /v1/consult/{sid} ----------------------- #

class TestPoll:
    def test_404_unknown_sid(self):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/consult/missing")
            assert r.status_code == 404

    def test_status_lifecycle(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            })
            sid = r.json()["sid"]
            # Wait for the executor to finish (stub is ~instant).
            for _ in range(50):
                p = client.get(f"/v1/consult/{sid}").json()
                if p["status"] == "completed":
                    break
                time.sleep(0.02)
            assert p["status"] == "completed"
            assert p["sid"] == sid


# ----------------------- /v1/consult/{sid}/result ----------------- #

class TestResult:
    def test_returns_summary_and_metadata(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            for _ in range(50):
                if client.get(f"/v1/consult/{sid}").json()["status"] \
                        == "completed":
                    break
                time.sleep(0.02)
            r = client.get(f"/v1/consult/{sid}/result")
            assert r.status_code == 200
            data = r.json()
            assert "**Verdict**" in data["summary_markdown"]
            assert data["metadata"]["session_id"] == sid

    def test_404_on_missing_sid(self):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/consult/csl-nope/result")
            assert r.status_code == 404

    def test_409_when_artifacts_not_ready(self, isolated_home, project_dir):
        # Slow runner — fetch result before it finishes.
        app = create_app(run_council=make_stub_runner(sleep=0.5))
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            r = client.get(f"/v1/consult/{sid}/result")
            assert r.status_code == 409


# ----------------------- /v1/sessions ---------------------------- #

class TestSessionsList:
    def test_empty_initially(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/sessions",
                           params={"cwd": str(project_dir)})
            assert r.status_code == 200
            assert r.json()["sessions"] == []

    def test_lists_started_sessions(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            client.post("/v1/consult", json={
                "message": "q1", "cwd": str(project_dir),
            })
            client.post("/v1/consult", json={
                "message": "q2", "cwd": str(project_dir),
            })
            time.sleep(0.1)  # let stub runner finish
            r = client.get("/v1/sessions",
                           params={"cwd": str(project_dir)})
            data = r.json()
            assert len(data["sessions"]) == 2

    def test_400_on_bad_cwd(self, tmp_path):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/sessions",
                           params={"cwd": str(tmp_path / "missing")})
            assert r.status_code == 400


# ----------------------- /v1/config ------------------------------ #

class TestConfigEndpoint:
    def test_returns_current_snapshot(self, isolated_home):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/config")
            assert r.status_code == 200
            data = r.json()
            assert data["topology"] == cc.DEFAULT_TOPOLOGY
            assert data["effort"] == cc.DEFAULT_EFFORT
            assert "planner" in data["roles"]
            assert data["roles"]["synthesizer"]["enabled"] is True
            assert "synthesizer" in data["mandatory_roles"]

    def test_reflects_user_changes(self, isolated_home):
        cc.set_role("planner", model="custom:tag", scope="user")
        cc.set_effort("high", scope="user")
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            data = client.get("/v1/config").json()
            assert data["roles"]["planner"]["model"] == "custom:tag"
            assert data["effort"] == "high"
            assert data["effort_budget"] == 5

    def test_400_on_bad_cwd(self, tmp_path):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/config",
                           params={"cwd": str(tmp_path / "missing")})
            assert r.status_code == 400


# ----------------------- failure path ---------------------------- #

class TestRunnerFailure:
    def test_status_failed_on_runner_crash(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner(fail=True))
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            for _ in range(50):
                p = client.get(f"/v1/consult/{sid}").json()
                if p["status"] in ("failed", "completed"):
                    break
                time.sleep(0.02)
            assert p["status"] == "failed"
            assert "stub crash" in (p["error"] or "")
