"""Whether timing matters to a claim (owner, 2026-11-27).

opencoti held off copying a model over the network because a shared GPU
claim for an episodic index rebuild did not say it was not a measurement.
A claim now states ``perf``: True (a measurement — keep load off), False
(use beside it freely), None (not stated). A quiet claim is always True.
"""
from __future__ import annotations

import sqlite3

import pytest

from claude_hooks.resources import api, hook, model, schema
from claude_hooks.resources.model import Claim, SpecError, parse_item
from claude_hooks.resources.store import Owner, ResourceStore
from claude_hooks.resources.tools import ResourceTools, tool_catalog


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_HOST", "solidpc")
    monkeypatch.setenv("CLAUDE_HOOKS_RESOURCES_STATE_DIR", str(tmp_path / "state"))
    api.reset_store()
    s = api.store({})
    s._alive = lambda pid, started: True
    return s


def claim(st, session, *specs, perf=None, reason="r"):
    return st.request([parse_item(x) for x in specs],
                      Owner(session_id=session, alias=session, host="solidpc"),
                      reason=reason, perf=perf).claim


def bash(cmd, session="me"):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session}


class TestModel:
    def test_quiet_is_always_a_measurement(self):
        c = Claim(id="c", seq=1, items=(parse_item("solidpc:quiet"),), perf=None)
        assert model.perf_of(c) is True and model.perf_tag(c) == "perf-sensitive"

    def test_stated_and_unstated(self):
        mk = lambda perf: Claim(id="c", seq=1, items=(parse_item("solidpc/gpu0:shared"),),
                                perf=perf)
        assert [model.perf_tag(mk(p)) for p in (True, False, None)] == [
            "perf-sensitive", "not perf-sensitive", "perf not stated"]

    def test_a_quiet_claim_cannot_be_declared_insensitive(self, st):
        with pytest.raises(SpecError):
            claim(st, "me", "solidpc:quiet", perf=False)


class TestStore:
    def test_round_trip(self, st):
        for perf in (True, False, None):
            c = claim(st, f"s{perf}", f"solidpc/gpu{int(bool(perf))}:shared", perf=perf)
            got = next(x for x in st.snapshot() if x.id == c.id)
            assert got.perf is perf

    def test_reuse_takes_the_latest_statement(self, st):
        c = claim(st, "me", "solidpc/gpu0:shared")
        again = st.request([parse_item("solidpc/gpu0:shared")],
                           Owner(session_id="me", alias="me", host="solidpc"),
                           perf=False)
        assert again.reused and again.claim.id == c.id
        assert next(x for x in st.snapshot() if x.id == c.id).perf is False

    def test_a_table_from_before_the_column_is_read_and_migrated(self, tmp_path):
        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE resource_claims (seq INTEGER PRIMARY KEY "
                    "AUTOINCREMENT, " + schema._CLAIMS_COLS.format(INT="INTEGER",
                                                                   REAL="REAL") + ")")
        con.execute("INSERT INTO resource_claims (id, items, state, session_id, alias, "
                    "host, created_at, granted_at, renewed_at) VALUES ('old1', "
                    "'[[\"solidpc/gpu0\", \"shared\", 1]]', 'granted', 'x', 'x', "
                    "'solidpc', '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z', "
                    "'2099-01-01T00:00:00Z')")
        con.commit()
        con.close()
        rs = ResourceStore("sqlite", str(db), host="solidpc",
                           alive=lambda pid, s: True)
        # The hot path reads it without the column (and never writes).
        assert [c.perf for c in rs.snapshot() if c.id == "old1"] == [None]
        cols = {r[1] for r in sqlite3.connect(db).execute(
            "PRAGMA table_info(resource_claims)")}
        assert "perf" not in cols
        # The first locked write adds it.
        rs.request([parse_item("solidpc/gpu1:shared")],
                   Owner(session_id="y", alias="y", host="solidpc"), perf=True)
        cols = {r[1] for r in sqlite3.connect(db).execute(
            "PRAGMA table_info(resource_claims)")}
        assert "perf" in cols

    def test_migrations_add_only_what_is_missing(self):
        assert schema.migrations("sqlite", set(schema.CLAIM_COLUMNS)) == []
        assert [m.split()[5] for m in schema.migrations(
            "sqlite", set(schema.LEGACY_CLAIM_COLUMNS))] == ["ttl_s", "perf"]


class TestPerfHolder:
    def test_only_measurements_hold_background_work_back(self, st):
        claim(st, "other", "solidpc/gpu0:shared", perf=False)
        claim(st, "other2", "solidpc/gpu1:shared")              # not stated
        assert api.perf_holder("solidpc", session_id="me") is None
        c = claim(st, "bench", "solidpc/gpu1:shared", perf=True)
        assert api.perf_holder("solidpc", session_id="me").id == c.id
        assert api.perf_holder("solidpc", session_id="bench") is None
        assert api.perf_holder("bs2", session_id="me") is None

    def test_quiet_holds_it_back(self, st):
        claim(st, "other", "solidpc:quiet")
        assert api.perf_holder("solidpc", session_id="me") is not None

    def test_the_stop_hook_skips_its_reindex_for_a_measurement(self, st):
        from claude_hooks.hooks import stop
        claim(st, "other", "solidpc/gpu0:shared", perf=False)
        assert stop._host_kept_quiet({"session_id": "me"}, {}) is False
        claim(st, "bench", "solidpc/gpu1:exclusive", perf=True)
        assert stop._host_kept_quiet({"session_id": "me"}, {}) is True


class TestPreToolNote:
    def test_a_measurement_on_a_gpu_flags_work_on_its_host(self, st):
        claim(st, "bench", "solidpc/gpu0:shared", perf=True, reason="decode bench")
        note = hook.pre_tool_note(event=bash("make -j32"), config={})
        assert note and "measures on it: keep load off the host" in note
        assert "[perf-sensitive]" in note

    def test_an_insensitive_claim_says_light_work_is_fine(self, st):
        claim(st, "rebuild", "solidpc/gpu0:exclusive", perf=False, reason="index")
        note = hook.pre_tool_note(event=bash("CUDA_VISIBLE_DEVICES=0 ./copy"), config={})
        assert note and "not a measurement: light work beside it is fine" in note
        assert "None of them is a measurement" in note

    def test_an_insensitive_shared_gpu_claim_does_not_flag_local_commands(self, st):
        claim(st, "rebuild", "solidpc/gpu0:shared", perf=False)
        assert hook.pre_tool_note(event=bash("rsync -a src/ dst/"), config={}) is None

    def test_the_session_block_shows_the_tag_and_asks_for_it(self, st):
        claim(st, "other", "solidpc/gpu0:shared", perf=False, reason="rebuild")
        text = hook.session_block(event={"session_id": "me"}, config={})
        assert "[not perf-sensitive]" in text and "--no-perf" in text


class TestTools:
    def test_acquire_takes_perf(self, st):
        tools = ResourceTools(store_fn=lambda: st, owner_fn=lambda: Owner(
            session_id="me", alias="me", host="solidpc"))
        out = tools.call("resource-acquire", {"resources": ["solidpc/gpu0:shared"],
                                              "reason": "rebuild", "perf": False})
        assert "GRANTED" in out and "[not perf-sensitive]" in out
        assert "Timing not stated" not in out
        out = tools.call("resource-acquire", {"resources": ["solidpc/gpu1:shared"],
                                              "reason": "x"})
        assert "Timing not stated" in out
        assert "not perf-sensitive" in tools.call("resource-status", {})
        assert tools.call("resource-acquire", {"resources": ["bs2"], "reason": "x",
                                               "perf": "maybe"}).startswith("Not done")

    def test_schema_offers_it(self):
        acquire = next(t for t in tool_catalog() if t["name"] == "resource-acquire")
        assert acquire["inputSchema"]["properties"]["perf"]["type"] == "boolean"


class TestCli:
    def test_flags(self):
        from claude_hooks.resources.cli import build_parser
        p = build_parser()
        assert p.parse_args(["acquire", "solidpc/gpu0", "--no-perf"]).perf is False
        assert p.parse_args(["wait", "solidpc/gpu0", "--perf"]).perf is True
        assert p.parse_args(["run", "solidpc/gpu0", "--perf"]).perf is True
        assert p.parse_args(["acquire", "solidpc/gpu0"]).perf is None
        with pytest.raises(SystemExit):
            p.parse_args(["acquire", "solidpc/gpu0", "--perf", "--no-perf"])
        assert p.parse_args(["perf-check", "solidpc"]).host == "solidpc"
