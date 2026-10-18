"""2026-10-18: the dashboard listed finished councils as LOST and showed
one as RUNNING. A test app's startup backfill outlived its test, upserted
60 finished councils into the real index as ``running`` and never
finished them; the list and the detail then read that row two ways."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from consultants.server import council_index, dashboard, inflight  # noqa: E402
from tests.test_council_dashboard import _app, _store  # noqa: E402


@pytest.fixture(autouse=True)
def _no_host_projects(monkeypatch):
    monkeypatch.setattr(council_index, "known_project_roots", lambda: [])


def _running(sid, root):
    council_index.record({"sid": sid, "cwd": str(root), "question": "q",
                          "started_at": 1000.0, "shell": {"mode": "write"}})


class TestBackfillNeverRewritesARow:

    def test_a_finished_row_stays_finished(self, tmp_path):
        _store(tmp_path, "csl-done", status="completed")
        _running("csl-done", tmp_path)
        council_index.finish("csl-done", "completed", at=1060.0)
        assert council_index.backfill([str(tmp_path)]) == 0
        row = council_index.get("csl-done")
        assert (row["status"], row["finished_at"]) == ("completed", 1060.0)

    def test_a_new_council_arrives_settled_in_one_write(self, tmp_path):
        _store(tmp_path, "csl-new", status="completed")
        assert council_index.backfill([str(tmp_path)]) == 1
        row = council_index.get("csl-new")
        assert (row["status"], row["finished_at"]) == ("completed", 1060.0)

    def test_a_row_that_appears_mid_backfill_is_kept(self, tmp_path, monkeypatch):
        _store(tmp_path, "csl-race", status="completed")
        real = council_index._read_meta

        def meanwhile(sdir):
            _running("csl-race", tmp_path)      # the engine registers it
            return real(sdir)
        monkeypatch.setattr(council_index, "_read_meta", meanwhile)
        council_index.backfill([str(tmp_path)])
        row = council_index.get("csl-race")
        assert row["status"] == "running" and row["shell"]

    def test_the_index_is_resolved_once(self, tmp_path, monkeypatch):
        """The test's index is gone mid-backfill (a fixture tore it down):
        the rest of the run still writes where it started."""
        _store(tmp_path, "csl-1", status="completed")
        _store(tmp_path, "csl-2", status="failed")
        first = council_index.index_path()
        other = tmp_path / "other.db"
        real = council_index._read_meta

        def switching(sdir):
            monkeypatch.setenv("CONSULTANTS_COUNCIL_INDEX", str(other))
            return real(sdir)
        monkeypatch.setattr(council_index, "_read_meta", switching)
        assert council_index.backfill([str(tmp_path)]) == 2
        assert not other.exists()
        monkeypatch.setenv("CONSULTANTS_COUNCIL_INDEX", str(first))
        assert council_index.get("csl-2")["status"] == "failed"


class TestStaleRows:

    def test_a_running_row_nothing_runs_takes_its_status_from_disk(self, tmp_path):
        _store(tmp_path, "csl-a", status="completed")
        _store(tmp_path, "csl-b", status="completed")
        _store(tmp_path, "csl-c", status=None, summary=False)
        for s in ("csl-a", "csl-b", "csl-c"):
            _running(s, tmp_path)
        assert council_index.settle_stale({"csl-b"}) == 1
        a = council_index.get("csl-a")
        assert (a["status"], a["finished_at"]) == ("completed", 1060.0)
        assert a["shell"]                                   # the rest kept
        assert council_index.get("csl-b")["status"] == "running"  # resuming
        assert council_index.get("csl-c")["status"] == "running"  # disk silent

    def test_list_and_detail_agree(self, tmp_path):
        _store(tmp_path, "csl-fin", status="completed")
        _store(tmp_path, "csl-gone", status=None, summary=False)
        _running("csl-fin", tmp_path)
        _running("csl-gone", tmp_path)
        rows = {r["sid"]: r for r in dashboard.council_list(_app())["councils"]}
        assert rows["csl-fin"]["status"] == "completed"
        assert rows["csl-gone"]["status"] == "lost"
        for sid, want in (("csl-fin", "completed"), ("csl-gone", "lost")):
            assert dashboard.council_detail(_app(), sid)["council"]["status"] == want

    def test_an_inflight_council_is_left_running(self, tmp_path):
        _store(tmp_path, "csl-res", status="completed")
        _running("csl-res", tmp_path)
        inflight.register({"sid": "csl-res", "cwd": str(tmp_path)})
        assert dashboard.council_detail(_app(), "csl-res")["council"]["status"] \
            == "running"


def test_the_startup_thread_is_off_in_tests(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from consultants.server import dashboard_routes
    calls = []

    class _Thread:
        def __init__(self, *a, **k):
            calls.append(k.get("name"))

        def start(self):
            pass
    monkeypatch.setattr(dashboard_routes.threading, "Thread", _Thread)
    app = FastAPI()
    app.state.sessions = {}
    dashboard_routes.register_dashboard_routes(app)
    with TestClient(app):
        pass
    assert calls == []
    monkeypatch.setenv("CONSULTANTS_INDEX_STARTUP", "1")
    with TestClient(app):
        pass
    assert calls == ["council-index"]
