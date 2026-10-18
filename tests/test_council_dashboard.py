"""The council dashboard: the host-wide index and the read-only views."""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi.testclient")

from consultants.server import council_index, dashboard  # noqa: E402


def _store(root, sid, *, question="why?", summary=True, status=None):
    sdir = root / ".claude-hooks" / "consultants" / sid
    sdir.mkdir(parents=True)
    con = sqlite3.connect(sdir / "transcript.db")
    con.executescript("""
      CREATE TABLE meta (sid TEXT, cwd TEXT, question TEXT, effort TEXT,
        topology TEXT, parent_sid TEXT, started_at REAL, finished_at REAL,
        status TEXT, error TEXT, models_json TEXT);
      CREATE TABLE events (event_id INTEGER PRIMARY KEY, ts REAL, kind TEXT,
        role TEXT, round INTEGER, lane_idx INTEGER, model TEXT,
        request_json TEXT, response_json TEXT, prompt_tokens INTEGER,
        completion_tokens INTEGER, tool TEXT, args TEXT, output TEXT,
        output_chars INTEGER, duration_ms INTEGER, error TEXT);
      CREATE TABLE runtime_events (event_id INTEGER PRIMARY KEY, ts REAL,
        kind TEXT, role TEXT, round INTEGER, lane_idx INTEGER, payload TEXT);
    """)
    con.execute("INSERT INTO meta VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, str(root), question, "high", "v2", None, 1000.0,
                 1060.0, status, None, json.dumps({"researcher": ["m1"]})))
    con.executemany(
        "INSERT INTO events (ts, kind, role, round, lane_idx, model, "
        "prompt_tokens, completion_tokens, tool) VALUES (?,?,?,?,?,?,?,?,?)",
        [(1001, "llm_call", "researcher", 1, 0, "m1", 100, 10, None),
         (1002, "tool_call", "researcher", 1, 0, None, None, None, "run_command"),
         (1010, "llm_call", "researcher:compaction", 1, 0, "m1", 50, 5, None),
         (1050, "llm_call", "synthesizer", 1, None, "m2", 300, 30, None)])
    con.executemany(
        "INSERT INTO runtime_events (ts, kind, role, round, lane_idx, payload) "
        "VALUES (?,?,?,?,?,?)",
        [(1001, "node_enter", "researcher", 1, 0, "{}"),
         (1003, "llm_call", "researcher", 1, 0, "{}"),
         (1011, "node_exit", "researcher", 1, 0,
          json.dumps({"stop_reason": "iteration_cap",
                      "failure_class": None, "text": "x" * 10})),
         (1020, "rewind", None, None, None, json.dumps({"to": "researcher"}))])
    con.commit()
    con.close()
    if summary:
        (sdir / "summary.md").write_text("the answer", encoding="utf-8")
    return sdir


def _app(sessions=None):
    return SimpleNamespace(state=SimpleNamespace(sessions=sessions or {}))


class TestIndex:
    def test_record_then_finish(self):
        council_index.record({"sid": "csl-a", "cwd": "/nowhere",
                              "question": "q", "started_at": 5.0})
        assert council_index.get("csl-a")["status"] == "running"
        council_index.finish("csl-a", "completed", at=9.0)
        row = council_index.get("csl-a")
        assert (row["status"], row["finished_at"]) == ("completed", 9.0)

    def test_suspended_is_not_finished(self):
        council_index.record({"sid": "csl-b", "cwd": "/x", "started_at": 1.0})
        council_index.finish("csl-b", "suspended", at=2.0)
        assert council_index.get("csl-b")["finished_at"] is None
        # A resume re-registers: running again.
        council_index.record({"sid": "csl-b", "cwd": "/x"})
        assert council_index.get("csl-b")["status"] == "running"

    def test_missing_directory_is_reported(self, tmp_path):
        council_index.record({"sid": "csl-gone", "cwd": str(tmp_path)})
        (row,) = council_index.list_councils(query="csl-gone")
        assert row["missing"] is True

    def test_backfill_reads_existing_stores_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(council_index, "known_project_roots", lambda: [])
        _store(tmp_path, "csl-old", question="old question")
        _store(tmp_path, "csl-failed", summary=False, status="failed")
        (tmp_path / ".claude-hooks" / "consultants" / "not-a-council").mkdir()
        assert council_index.backfill([str(tmp_path)]) == 2
        assert council_index.backfill([str(tmp_path)]) == 0
        old = council_index.get("csl-old")
        assert old["question"] == "old question"
        assert old["status"] == "completed"
        assert council_index.get("csl-failed")["status"] == "failed"

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(council_index, "known_project_roots", lambda: [])
        _store(tmp_path, "csl-dry")
        assert council_index.backfill([str(tmp_path)], dry_run=True) == 1
        assert council_index.get("csl-dry") is None

    def test_find_stores_skips_heavy_dirs(self, tmp_path):
        (tmp_path / "a" / "b" / ".claude-hooks" / "consultants").mkdir(parents=True)
        (tmp_path / "node_modules" / "p" / ".claude-hooks" / "consultants"
         ).mkdir(parents=True)
        assert council_index.find_stores([str(tmp_path)]) == [
            str((tmp_path / "a" / "b").resolve())]

    def test_inflight_register_indexes(self, tmp_path):
        from consultants.server import inflight
        inflight.register({"sid": "csl-if", "cwd": str(tmp_path),
                           "question": "via inflight", "kind": "consult"})
        assert council_index.get("csl-if")["question"] == "via inflight"


class TestViews:
    def test_running_without_engine_is_lost(self):
        council_index.record({"sid": "csl-orphan", "cwd": "/x"})
        d = dashboard.council_list(_app())
        (row,) = [r for r in d["councils"] if r["sid"] == "csl-orphan"]
        assert row["status"] == "lost"
        assert d["running"] == 0

    def test_live_session_wins(self):
        council_index.record({"sid": "csl-live", "cwd": "/x"})
        s = SimpleNamespace(status="running", progress={"researcher": "in_progress"},
                            consultancy_status=None, run_control=None)
        d = dashboard.council_list(_app({"csl-live": s}))
        (row,) = [r for r in d["councils"] if r["sid"] == "csl-live"]
        assert row["status"] == "running"
        assert row["live"]["progress"] == {"researcher": "in_progress"}
        assert d["running"] == 1

    def test_detail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(council_index, "known_project_roots", lambda: [])
        _store(tmp_path, "csl-d")
        council_index.backfill([str(tmp_path)])
        d = dashboard.council_detail(_app(), "csl-d")
        lanes = {(ln["role"], ln["lane_idx"]): ln for ln in d["lanes"]}
        r0 = lanes[("researcher", 0)]
        # The compaction call belongs to its lane.
        assert (r0["llm_calls"], r0["prompt_tokens"], r0["commands"]) == (2, 150, 1)
        assert r0["stop_reason"] == "iteration_cap"
        kinds = [e["kind"] for e in d["timeline"]]
        assert "llm_call" not in kinds and "rewind" in kinds
        assert all("text" not in e["data"] for e in d["timeline"])
        assert d["totals"]["llm_calls"] == 3
        assert d["answer"] == "the answer"
        full = dashboard.council_detail(_app(), "csl-d", full=True)
        assert "llm_call" in [e["kind"] for e in full["timeline"]]

    def test_unknown_council(self):
        assert dashboard.council_detail(_app(), "csl-none") is None


class TestRoutes:
    def test_page_and_api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from consultants.server.app import create_app
        monkeypatch.setattr(council_index, "known_project_roots", lambda: [])
        _store(tmp_path, "csl-r")
        with TestClient(create_app()) as c:
            page = c.get("/dashboard")
            assert page.status_code == 200 and "<title>Council Dashboard" in page.text
            r = c.post("/v1/dashboard/index/backfill",
                       json={"scan": [str(tmp_path)]})
            assert r.json()["added"] == 1
            sids = [x["sid"] for x in c.get("/v1/dashboard/councils").json()["councils"]]
            assert "csl-r" in sids
            assert c.get("/v1/dashboard/councils/csl-r").json()["answer"] == "the answer"
            assert c.get("/v1/dashboard/councils/csl-zz").status_code == 404


class TestLanDashboard:
    """``[service] dashboard_host``: the LAN gets the dashboard, never the
    control API."""

    def _client(self, guard, host):
        from fastapi.testclient import TestClient

        from consultants.server.app import create_app
        app = create_app()
        app.state.lan_guard = guard
        return TestClient(app, client=(host, 50000))

    def test_a_lan_client_gets_the_dashboard_only(self):
        with self._client(True, "192.168.178.30") as c:
            assert c.get("/dashboard").status_code == 200
            assert c.get("/v1/dashboard/councils").status_code == 200
            assert c.get("/v1/health").status_code == 200
            for method, path in (("post", "/v1/consult"),
                                 ("get", "/v1/sessions"),
                                 ("post", "/v1/consult/csl-x/cancel"),
                                 ("post", "/v1/dashboard/index/backfill"),
                                 ("post", "/v1/consult/csl-x/inject")):
                r = (c.post(path, json={}) if method == "post"
                     else c.get(path))
                assert r.status_code == 403, path
                assert "loopback-only" in r.json()["detail"]

    def test_loopback_keeps_the_api(self):
        with self._client(True, "127.0.0.1") as c:
            assert c.get("/v1/sessions").status_code != 403

    def test_no_dashboard_host_changes_nothing(self):
        with self._client(False, "192.168.178.30") as c:
            assert c.get("/v1/sessions").status_code != 403

    def test_route_and_client_rules(self):
        from consultants.server.dashboard_routes import (
            is_local_client, lan_allowed)
        assert lan_allowed("GET", "/v1/consult/csl-1/events")
        assert not lan_allowed("POST", "/v1/dashboard/councils")
        assert not lan_allowed("GET", "/v1/consult/csl-1/result")
        assert not lan_allowed("GET", "/v1/dashboard/councils/a/b")
        assert is_local_client("::1") and is_local_client("testclient")
        assert not is_local_client("10.0.0.5")

    def test_sockets(self):
        import socket

        from consultants.server.__main__ import listen_sockets
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        socks = listen_sockets("127.0.0.1", "", port)
        try:
            assert [s.getsockname()[0] for s in socks] == ["127.0.0.1"]
        finally:
            for s in socks:
                s.close()
        socks = listen_sockets("127.0.0.1", "0.0.0.0", port)
        try:
            assert [s.getsockname()[0] for s in socks] == ["0.0.0.0"]
        finally:
            for s in socks:
                s.close()
        # A dashboard address that will not bind (taken): logged, the
        # engine still serves loopback.
        with socket.socket() as taken:
            taken.bind(("127.0.0.2", port))
            taken.listen()
            socks = listen_sockets("127.0.0.1", "127.0.0.2", port)
            try:
                assert [s.getsockname()[0] for s in socks] == ["127.0.0.1"]
            finally:
                for s in socks:
                    s.close()

    def test_config_round_trip(self, tmp_path, monkeypatch):
        import pytest as _pt

        from consultants import config as cc
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        cfg = cc.set_dashboard_host("0.0.0.0", scope="user")
        assert cfg.service.dashboard_host == "0.0.0.0"
        assert cc.load_config().service.dashboard_host == "0.0.0.0"
        assert cc.set_dashboard_host("off").service.dashboard_host == ""
        with _pt.raises(ValueError):
            cc.set_dashboard_host("lan.example")
