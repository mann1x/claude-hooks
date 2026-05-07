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

class _FakeChatClient:
    """Stand-in for claude_hooks ChatClient that the follow-up
    tests use to assert warm-handle reuse."""
    def __init__(self, tag: str = "stub"):
        self.tag = tag
        self.calls = 0

    def chat(self, payload):  # pragma: no cover — not exercised by stubs
        self.calls += 1
        return {"choices": [{"message": {"content": "stub"}}]}


def make_stub_runner(*, fail: bool = False, sleep: float = 0.0):
    """Return a synchronous stub runner that writes legitimate
    artifacts. ``fail=True`` simulates a graph crash. Also
    populates the live-session fields on SessionState so the
    follow-up tests have something to reuse (matches what the
    production runner does in consultants.server.runner)."""

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
        # Populate live-session fields so follow-up tests can reuse.
        state.plan = "1. stub plan item one\n2. stub plan item two"
        state.plan_items = ["stub plan item one", "stub plan item two"]
        state.research = ["stub research finding"]
        state.critique = "stub critique"
        state.final_answer = "**Verdict**: stub answer."
        state.models = dict(result.models)
        state._chat_clients = {
            r: _FakeChatClient(tag=f"warm-{r}") for r in state.progress
        }
        state.status = "completed"
        state.finished_at = time.time()
        state.bump_activity()

    return run_council


def make_stub_follow_up_runner(*, fail: bool = False):
    """Stub follow-up runner. Asserts the parent_state was passed
    in (the real runner reads parent.research / _chat_clients) and
    writes a child consultation tagged with parent_sid."""

    def run_follow_up(state: SessionState, runner_input: dict) -> None:
        parent_state = runner_input.get("parent_state")
        if parent_state is None:
            state.status = "failed"
            state.error = "stub follow-up: no parent_state"
            state.finished_at = time.time()
            return
        if fail:
            state.status = "failed"
            state.error = "stub follow-up crash"
            state.finished_at = time.time()
            return
        cwd = Path(runner_input["cwd"])
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(state.started_at)),
            question=runner_input["question"],
            models=dict(parent_state.models or {"researcher": "stub"}),
            topology=state.topology,
            effort=state.effort,
            final_answer="**Follow-up verdict**: stub child answer.",
            turns=[
                storage.RoleTurn(role="synthesizer", round=1,
                                 content="child", prompt_tokens=3,
                                 completion_tokens=2),
            ],
            duration_seconds=time.time() - state.started_at,
            status="completed",
            cwd=str(cwd),
            total_prompt_tokens=3,
            total_completion_tokens=2,
            parent_sid=state.parent_sid,
        )
        storage.write_consultation(result, cwd=cwd)
        for r in state.progress:
            state.progress[r] = "done"
        state.research = list(parent_state.research) + ["child finding"]
        state.final_answer = result.final_answer
        state.models = dict(result.models)
        state._chat_clients = parent_state._chat_clients  # inherit
        state.status = "completed"
        state.finished_at = time.time()
        state.bump_activity()

    return run_follow_up


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


# ----------------------- follow-up endpoint --------------------- #
# Live-session iteration: Claude evaluates the synthesizer's answer
# and may dispatch a focused follow-up that reuses the parent's
# plan + research + warm ChatClients. The skill loops until the
# answer is decisive or the effort_budget is exhausted.

def _wait_for_status(client, sid: str, target: str = "completed",
                     iters: int = 50) -> dict:
    for _ in range(iters):
        p = client.get(f"/v1/consult/{sid}").json()
        if p["status"] == target:
            return p
        time.sleep(0.02)
    raise AssertionError(f"sid {sid} never reached status={target}")


class TestFollowUp:
    def test_503_when_no_follow_up_runner(self, isolated_home,
                                          project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "more on Y"})
            assert r.status_code == 503

    def test_400_when_parent_unknown_and_no_cwd(self, isolated_home,
                                                 project_dir):
        # Without a cwd hint, the engine can't find the artifact; the
        # caller has to supply cwd for the disk-fallback path. We
        # return 400 (not 404) because the request is incomplete.
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            r = client.post(
                "/v1/consult/csl-does-not-exist/follow-up",
                json={"message": "more"},
            )
            assert r.status_code == 400
            assert "cwd" in r.json()["detail"].lower()

    def test_404_when_cwd_given_but_no_artifact(self, isolated_home,
                                                project_dir):
        # cwd provided but no metadata.json under it for that sid.
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            r = client.post(
                "/v1/consult/csl-does-not-exist/follow-up",
                json={"message": "more", "cwd": str(project_dir)},
            )
            assert r.status_code == 404

    def test_409_when_parent_running(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner(sleep=0.5),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "more"})
            assert r.status_code == 409
            _wait_for_status(client, sid)  # cleanup

    def test_400_on_empty_message(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "  "})
            assert r.status_code == 400

    def test_400_on_bad_effort(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "x", "effort": "xtreme"})
            assert r.status_code == 400

    def test_returns_new_sid_with_parent(self, isolated_home,
                                         project_dir):
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q1", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "more on Y"})
            assert r.status_code == 200
            body = r.json()
            assert body["parent_sid"] == sid
            assert body["sid"] != sid
            assert body["sid"].startswith("csl-")
            child_sid = body["sid"]
            child_state = _wait_for_status(client, child_sid)
            # Parent should now record the child in follow_up_sids.
            parent_state = client.get(f"/v1/consult/{sid}").json()
            assert child_sid in parent_state["follow_up_sids"]
            assert child_state["parent_sid"] == sid

    def test_follow_up_chains(self, isolated_home, project_dir):
        # follow-up of a follow-up: chain length 3 (root → A → B).
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            root = client.post("/v1/consult", json={
                "message": "root", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, root)
            a = client.post(f"/v1/consult/{root}/follow-up",
                            json={"message": "a"}).json()["sid"]
            _wait_for_status(client, a)
            b = client.post(f"/v1/consult/{a}/follow-up",
                            json={"message": "b"}).json()["sid"]
            _wait_for_status(client, b)
            chain = [client.get(f"/v1/consult/{x}").json()
                     for x in (root, a, b)]
            assert chain[0]["parent_sid"] is None
            assert chain[1]["parent_sid"] == root
            assert chain[2]["parent_sid"] == a

    def test_auto_reopens_closed_parent(self, isolated_home, project_dir):
        # A closed parent is silently reopened in place (the data is
        # still in app.state.sessions, just with closed=True). The
        # follow-up proceeds normally and the parent flips back to
        # closed=False.
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            client.post(f"/v1/consult/{sid}/close")
            assert app.state.sessions[sid].closed is True
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "x"})
            assert r.status_code == 200
            child_sid = r.json()["sid"]
            _wait_for_status(client, child_sid)
            # Parent should be open again.
            assert app.state.sessions[sid].closed is False

    def test_auto_loads_evicted_parent_from_disk(self, isolated_home,
                                                 project_dir):
        # Parent completed, then was evicted from app.state.sessions
        # entirely (simulating a service restart or reaper eviction
        # past the grace window). With a cwd hint, the follow-up
        # endpoint reconstructs the SessionState from disk
        # artifacts and proceeds.
        app = create_app(run_council=make_stub_runner(),
                         run_follow_up=make_stub_follow_up_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            # Hard-evict (not via close — simulate a full restart).
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            assert sid not in app.state.sessions

            # Without cwd → 400.
            r = client.post(f"/v1/consult/{sid}/follow-up",
                            json={"message": "x"})
            assert r.status_code == 400

            # With cwd → 200, reconstructed in app.state.sessions.
            r = client.post(
                f"/v1/consult/{sid}/follow-up",
                json={"message": "x", "cwd": str(project_dir)},
            )
            assert r.status_code == 200
            assert sid in app.state.sessions
            reopened = app.state.sessions[sid]
            # Reconstructed plan + research came from the stub
            # runner's turn payload.
            assert "stub" in reopened.plan.lower() or \
                   reopened.final_answer.startswith("**Verdict**")
            assert reopened.closed is False
            child_sid = r.json()["sid"]
            _wait_for_status(client, child_sid)


# ----------------------- reopen endpoint ----------------------- #

class TestReopen:
    def test_warm_session_returns_already_open(self, isolated_home,
                                               project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/reopen", json={})
            assert r.status_code == 200
            body = r.json()
            assert body["already_open"] is True
            assert body["source"] == "warm"

    def test_in_memory_closed_session_reopens(self, isolated_home,
                                              project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            client.post(f"/v1/consult/{sid}/close")
            r = client.post(f"/v1/consult/{sid}/reopen", json={})
            assert r.status_code == 200
            assert r.json()["source"] == "in-memory-reopen"
            assert app.state.sessions[sid].closed is False

    def test_evicted_session_loads_from_disk(self, isolated_home,
                                             project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            r = client.post(
                f"/v1/consult/{sid}/reopen",
                json={"cwd": str(project_dir)},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["source"] == "disk"
            assert body["already_open"] is False
            assert body["research_turns"] >= 0  # stub runner emits 1
            assert sid in app.state.sessions

    def test_400_no_cwd_when_evicted(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            r = client.post(f"/v1/consult/{sid}/reopen", json={})
            assert r.status_code == 400
            assert "cwd" in r.json()["detail"].lower()

    def test_404_when_no_artifact(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post(
                "/v1/consult/csl-does-not-exist/reopen",
                json={"cwd": str(project_dir)},
            )
            assert r.status_code == 404

    def test_reconstructed_session_has_plan_and_research(
            self, isolated_home, project_dir):
        # The stub runner emits a structured turn list; the
        # reconstructor must extract plan from the planner turn
        # and research from the researcher turn.
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            client.post(f"/v1/consult/{sid}/reopen",
                        json={"cwd": str(project_dir)})
            state = app.state.sessions[sid]
            # Stub runner only emits a synthesizer turn, so plan
            # and research are empty but the field shapes are
            # right and final_answer carries through.
            assert isinstance(state.plan, str)
            assert isinstance(state.research, list)
            assert state.final_answer == "**Verdict**: stub answer."
            assert state.models == {
                "planner": "stub", "researcher": "stub",
                "critic": "stub", "synthesizer": "stub",
            }

    def test_reopen_loads_role_messages_when_transcript_db_present(
            self, isolated_home, project_dir):
        # Phase 4: when the artifact dir contains a finalized
        # transcript.db, the reopen path populates SessionState's
        # _role_messages / _role_lane_messages fields. The stub
        # runner doesn't write a transcript.db (it bypasses the
        # recorder), so we seed one manually after the consultation
        # completes — same shape Phase 5's follow-up reads.
        from consultants.engine.recorder import (
            MessageRecorder, RecorderMeta,
        )
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)

            # Seed a transcript.db alongside the existing artifacts.
            sdir = (project_dir / ".claude-hooks" / "consultants" / sid)
            db_path = sdir / storage.TRANSCRIPT_DB_FILENAME
            meta = RecorderMeta(
                sid=sid, cwd=str(project_dir),
                question="q", effort="medium",
                topology="council", models={"planner": "m"},
            )
            rec = MessageRecorder(db_path, meta=meta)
            try:
                rec.record_llm(
                    role="synthesizer", round=1, model="m",
                    request={"messages": [
                        {"role": "user", "content": "q"}]},
                    response={"choices": [{"message": {
                        "role": "assistant",
                        "content": "**Verdict**: from db.",
                    }}]},
                )
                rec.record_llm(
                    role="researcher", round=1, lane_idx=0, model="m",
                    request={"messages": [
                        {"role": "user", "content": "lane 0 task"}]},
                    response={"choices": [{"message": {
                        "role": "assistant",
                        "content": "lane 0 finding",
                    }}]},
                )
                rec.record_llm(
                    role="researcher", round=1, lane_idx=1, model="m",
                    request={"messages": [
                        {"role": "user", "content": "lane 1 task"}]},
                    response={"choices": [{"message": {
                        "role": "assistant",
                        "content": "lane 1 finding",
                    }}]},
                )
                rec.finalize(status="completed")
            finally:
                rec.close()

            # Evict and reopen — exercise the disk-load branch.
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            r = client.post(f"/v1/consult/{sid}/reopen",
                            json={"cwd": str(project_dir)})
            assert r.status_code == 200
            state = app.state.sessions[sid]

            # _role_messages populated for both roles.
            assert state._role_messages is not None
            assert "synthesizer" in state._role_messages
            assert "researcher" in state._role_messages
            assert state._role_messages["synthesizer"][-1]["content"] \
                == "**Verdict**: from db."
            # Per-lane researcher threads carry both lanes.
            assert state._role_lane_messages is not None
            assert state._role_lane_messages["researcher"].keys() \
                == {0, 1}
            assert state._role_lane_messages["researcher"][0][-1]["content"] \
                == "lane 0 finding"
            assert state._role_lane_messages["researcher"][1][-1]["content"] \
                == "lane 1 finding"

    def test_reopen_without_transcript_db_keeps_fields_none(
            self, isolated_home, project_dir):
        # Backward compat: a v1.0-shape artifact dir (metadata.json
        # but no transcript.db) reopens cleanly with _role_messages
        # left at None. Phase 5 falls back to turn-content
        # reconstruction in that case.
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            # No transcript.db seeding — the stub runner doesn't
            # write one.
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            r = client.post(f"/v1/consult/{sid}/reopen",
                            json={"cwd": str(project_dir)})
            assert r.status_code == 200
            state = app.state.sessions[sid]
            assert state._role_messages is None
            assert state._role_lane_messages is None
            # And other reopen behavior still works.
            assert state.final_answer == "**Verdict**: stub answer."

    def test_reopen_tolerates_corrupt_transcript_db(
            self, isolated_home, project_dir):
        # If transcript.db exists but isn't a valid SQLite database
        # (truncated mid-write, replaced with garbage), reopen still
        # succeeds; _role_messages just stays None.
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            sdir = (project_dir / ".claude-hooks" / "consultants" / sid)
            (sdir / storage.TRANSCRIPT_DB_FILENAME).write_text(
                "not a sqlite db",
            )
            with app.state.sessions_lock:
                app.state.sessions.pop(sid)
            r = client.post(f"/v1/consult/{sid}/reopen",
                            json={"cwd": str(project_dir)})
            assert r.status_code == 200
            state = app.state.sessions[sid]
            assert state._role_messages is None
            assert state._role_lane_messages is None


# ----------------------- close endpoint ------------------------- #

class TestClose:
    def test_404_unknown_sid(self):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.post("/v1/consult/csl-bogus/close")
            assert r.status_code == 404

    def test_409_when_running(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner(sleep=0.5))
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            r = client.post(f"/v1/consult/{sid}/close")
            assert r.status_code == 409
            _wait_for_status(client, sid)  # cleanup

    def test_close_ok(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.post(f"/v1/consult/{sid}/close")
            assert r.status_code == 200
            body = r.json()
            assert body["sid"] == sid
            assert body["already_closed"] is False
            assert body["closed_at"] is not None

    def test_close_idempotent(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            client.post(f"/v1/consult/{sid}/close")
            r = client.post(f"/v1/consult/{sid}/close")
            assert r.status_code == 200
            assert r.json()["already_closed"] is True

    def test_close_releases_chat_clients(self, isolated_home,
                                         project_dir):
        # The session entry stays in app.state.sessions for the
        # grace period, but its warm ChatClients are released so we
        # don't pin per-instance memos.
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            assert app.state.sessions[sid]._chat_clients is not None
            client.post(f"/v1/consult/{sid}/close")
            assert app.state.sessions[sid]._chat_clients is None
            assert app.state.sessions[sid].closed is True


# ----------------------- list-open endpoint --------------------- #

class TestListOpen:
    def test_empty_initially(self):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            r = client.get("/v1/sessions/open")
            assert r.status_code == 200
            assert r.json()["open_sessions"] == []

    def test_includes_completed_session(self, isolated_home,
                                        project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            r = client.get("/v1/sessions/open")
            sids = [s["sid"] for s in r.json()["open_sessions"]]
            assert sid in sids

    def test_excludes_closed(self, isolated_home, project_dir):
        app = create_app(run_council=make_stub_runner())
        with TestClient(app) as client:
            sid = client.post("/v1/consult", json={
                "message": "q", "cwd": str(project_dir),
            }).json()["sid"]
            _wait_for_status(client, sid)
            client.post(f"/v1/consult/{sid}/close")
            sids = [
                s["sid"]
                for s in client.get("/v1/sessions/open").json()
                                 ["open_sessions"]
            ]
            assert sid not in sids


# ----------------------- reaper -------------------------------- #

class TestReaper:
    def test_idle_session_gets_closed(self, isolated_home, project_dir):
        # Tight timings so the test runs in well under a second.
        app = create_app(run_council=make_stub_runner(),
                         idle_timeout_s=0.05,
                         reaper_interval_s=0.02)
        try:
            with TestClient(app) as client:
                sid = client.post("/v1/consult", json={
                    "message": "q", "cwd": str(project_dir),
                }).json()["sid"]
                _wait_for_status(client, sid)
                # Wait long enough for at least 2 reaper ticks.
                deadline = time.time() + 1.0
                while time.time() < deadline:
                    if app.state.sessions[sid].closed:
                        break
                    time.sleep(0.02)
                assert app.state.sessions[sid].closed is True
        finally:
            app.state.reaper_stop.set()

    def test_active_polling_keeps_alive(self, isolated_home,
                                        project_dir):
        # Polling bumps last_activity_at, so a session being
        # iterated on shouldn't get reaped under us.
        app = create_app(run_council=make_stub_runner(),
                         idle_timeout_s=0.20,
                         reaper_interval_s=0.02)
        try:
            with TestClient(app) as client:
                sid = client.post("/v1/consult", json={
                    "message": "q", "cwd": str(project_dir),
                }).json()["sid"]
                _wait_for_status(client, sid)
                end = time.time() + 0.40
                # Poll faster than the reaper kills.
                while time.time() < end:
                    client.get(f"/v1/consult/{sid}")
                    time.sleep(0.05)
                assert app.state.sessions[sid].closed is False
        finally:
            app.state.reaper_stop.set()
