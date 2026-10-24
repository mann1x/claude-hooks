"""Resource manager: the decision rules, the store on SQLite (real
threads and processes), the CLI, and — with a DSN — the Postgres waiter."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from claude_hooks.resources.store import parse_iso

from claude_hooks.resources import model
from claude_hooks.resources.model import (
    EXCLUSIVE, QUIET, SHARED, Claim, Item, SpecError, decide, items_conflict,
    parse_item)
from claude_hooks.resources.store import Owner, ResourceStore

REPO = Path(__file__).resolve().parents[1]


def C(seq, *specs, state="waiting", session="s"):
    return Claim(id=f"c{seq}", seq=seq, items=tuple(parse_item(s) for s in specs),
                 state=state, session_id=session)


# ─── rules ──────────────────────────────────────────────────────────────

class TestConflicts:
    @pytest.mark.parametrize("a,b,conflict", [
        ("solidpc/gpu0:s", "solidpc/gpu0:s", False),
        ("solidpc/gpu0:s", "solidpc/gpu0:x", True),
        ("solidpc/gpu0:x", "solidpc/gpu0:x", True),
        ("solidpc/gpu0:x", "solidpc/gpu1:x", False),
        ("solidpc:x", "solidpc/gpu0:s", True),
        ("solidpc:s", "solidpc/gpu0:x", False),
        ("solidpc/gpu0:x", "solidpc:s", False),
        ("solidpc/gpu0:q", "solidpc/gpu1:s", True),
        ("solidpc/gpu0:q", "solidpc:s", True),
        ("solidpc:q", "solidpc/gpu0:s", True),
        ("solidpc:q", "bs2/gpu0:x", False),
        ("solidpc/gpu0:x", "solidpcx/gpu0:x", False),
    ])
    def test_matrix(self, a, b, conflict):
        assert items_conflict(parse_item(a), parse_item(b)) is conflict
        assert items_conflict(parse_item(b), parse_item(a)) is conflict


class TestSpecs:
    def test_defaults_and_aliases(self):
        assert parse_item("SolidPC/GPU0") == Item("solidpc/gpu0", EXCLUSIVE, 1)
        assert parse_item("ollama-cloud:shared:2") == Item(
            "ollama-cloud", SHARED, 2)
        assert parse_item("bs2:q").mode == QUIET

    @pytest.mark.parametrize("bad", ["", "a b", "x:loud", "x:s:0", "x:s:1:2",
                                     "/", "a//b:z"])
    def test_rejects(self, bad):
        with pytest.raises(SpecError):
            parse_item(bad)


class TestDecide:
    def test_shared_coexist_within_capacity(self):
        cap = {"ollama-cloud": 3}
        w = [C(i, "ollama-cloud:s") for i in range(1, 5)]
        got = decide([], w, cap)
        assert [c.seq for c in got] == [1, 2, 3]

    def test_exclusive_waiter_is_not_starved_by_later_shared(self):
        held = [C(1, "gpu0:s", state="granted")]
        w = [C(2, "gpu0:x"), C(3, "gpu0:s")]
        assert decide(held, w, {}) == []

    def test_unrelated_later_claim_passes_a_blocked_one(self):
        held = [C(1, "gpu0:x", state="granted")]
        w = [C(2, "gpu0:x"), C(3, "gpu1:x")]
        assert [c.seq for c in decide(held, w, {})] == [3]

    def test_multi_item_claim_is_all_or_nothing(self):
        held = [C(1, "solidpc/gpu1:x", state="granted")]
        w = [C(2, "solidpc/gpu0:x", "solidpc/gpu1:x")]
        assert decide(held, w, {}) == []
        assert [c.seq for c in decide([], w, {})] == [2]

    def test_quiet_waits_for_the_whole_host(self):
        held = [C(1, "solidpc/gpu1:s", state="granted")]
        assert decide(held, [C(2, "solidpc/gpu0:q")], {}) == []
        assert [c.seq for c in decide(held, [C(2, "bs2/gpu0:q")], {})] == [2]

    def test_two_claims_of_one_session_conflict(self):
        held = [C(1, "gpu0:x", state="granted", session="same")]
        assert decide(held, [C(2, "gpu0:x", session="same")], {}) == []

    def test_covers(self):
        held = C(1, "gpu0:x", "solidpc:s", state="granted")
        assert model.covers(held, [parse_item("gpu0:s")])
        assert not model.covers(held, [parse_item("gpu0:q")])
        assert not model.covers(held, [parse_item("gpu1:x")])
        quiet = C(2, "solidpc/gpu0:q", state="granted")
        assert model.covers(quiet, [parse_item("solidpc/gpu1:x"),
                                    parse_item("solidpc:x")])
        assert not model.covers(quiet, [parse_item("bs2/gpu0:s")])


# ─── store (SQLite) ─────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "res.db")


def mk(db, *, host="solidpc", alive=None, clock=None):
    return ResourceStore("sqlite", db, host=host,
                         alive=alive or (lambda pid, st: True),
                         clock=clock or time.time)


def owner(session, host="solidpc", pid=None):
    return Owner(session_id=session, alias=session, host=host, pid=pid,
                 pid_started=1.0 if pid else None)


def items(*specs):
    return [parse_item(s) for s in specs]


class TestStore:
    def test_grant_queue_release_hands_over(self, db):
        st = mk(db)
        a = st.request(items("solidpc/gpu0"), owner("A"), reason="kld")
        assert a.granted
        b = st.request(items("solidpc/gpu0"), owner("B"), reason="gate")
        assert not b.granted and b.position == 1
        assert "held by A@solidpc#A" in b.block.describe()
        assert "kld" in b.block.describe()
        done, refused = st.release([a.claim.id], session_id="A")
        assert [c.id for c in done] == [a.claim.id] and not refused
        assert st.get(b.claim.id).granted

    def test_release_of_another_sessions_claim_needs_force(self, db):
        st = mk(db)
        a = st.request(items("x"), owner("A"))
        done, refused = st.release([a.claim.id], session_id="B")
        assert not done and [c.id for c in refused] == [a.claim.id]
        done, _ = st.release([a.claim.id], session_id="B", force=True)
        assert done
        assert st.events(5)[0]["event"] == "force_released"

    def test_holder_gone_releases_and_grants_next(self, db):
        alive = {"pid": True}
        st = mk(db, alive=lambda pid, started: alive["pid"])
        a = st.request(items("gpu0"), owner("A", pid=4242))
        b = st.request(items("gpu0"), owner("B"))
        assert a.granted and not b.granted
        alive["pid"] = False
        assert st.get(a.claim.id) is None
        assert st.get(b.claim.id).granted
        assert any(e["event"] == "holder_gone" for e in st.events(10))

    def test_remote_claim_lives_by_its_lease(self, db):
        clock = Clock()
        here = mk(db, host="pandorum", clock=clock)
        there = mk(db, host="solidpc", clock=clock,
                   alive=lambda pid, st: True)
        a = there.request(items("bs2/gpu0"), owner("A", "solidpc", pid=7),
                          lease_s=600)
        clock.t += 500
        assert here.get(a.claim.id) is not None
        there.renew()                       # solidpc renews its own claims
        clock.t += 500
        assert here.get(a.claim.id) is not None
        clock.t += 601
        assert here.get(a.claim.id) is None
        assert here.events(3)[0]["event"] == "lease_lapsed"

    def test_ttl_expires(self, db):
        clock = Clock()
        st = mk(db, clock=clock)
        a = st.request(items("eleven2go"), owner("A", pid=1), ttl_s=60)
        clock.t += 59
        assert st.get(a.claim.id) is not None
        clock.t += 2
        assert st.get(a.claim.id) is None

    def test_a_queued_claims_ttl_starts_at_its_grant(self, db):
        # 2026-10-24: queued 2nd behind a 2 h job with --ttl 2h, the claim
        # would have expired in the queue before it was ever held.
        clock = Clock()
        st = mk(db, clock=clock)
        a = st.request(items("eleven2go"), owner("A", pid=1))
        b = st.request(items("eleven2go"), owner("B", pid=2), ttl_s=60)
        assert not b.granted and b.claim.expires_at == ""
        clock.t += 3600
        assert st.get(b.claim.id) is not None      # still queued
        st.release([a.claim.id], force=True)
        held = st.get(b.claim.id)
        assert held.granted
        # The expiry status shows is the grant's, not the request's.
        assert parse_iso(held.claim.expires_at) == pytest.approx(clock.t + 60)
        clock.t += 59
        assert st.get(b.claim.id) is not None
        clock.t += 2
        assert st.get(b.claim.id) is None
        assert st.events(1)[0]["event"] == "expired"

    def test_a_grant_from_older_code_is_still_bounded_by_its_ttl(self, db):
        clock = Clock()
        st = mk(db, clock=clock)
        a = st.request(items("eleven2go"), owner("A", pid=1), ttl_s=60)
        st._run(lambda cur: cur.execute(st._q(
            "UPDATE resource_claims SET expires_at = '' WHERE id = ?"),
            (a.claim.id,)))
        clock.t += 59
        assert st.get(a.claim.id) is not None
        clock.t += 2
        assert st.get(a.claim.id) is None

    def test_a_table_from_before_ttl_s_is_migrated_in_place(self, db):
        st = mk(db)
        st._run(lambda cur: None)
        if st.dialect != "sqlite":
            pytest.skip("the ALTER is exercised on sqlite")
        conn = st._connect()
        conn.execute("ALTER TABLE resource_claims DROP COLUMN ttl_s")
        conn.commit()
        conn.close()
        old = mk(db)
        assert old.snapshot() == []                 # read before migrating
        a = old.request(items("eleven2go"), owner("A", pid=1), ttl_s=60)
        assert a.granted and a.claim.ttl_s == 60

    def test_reuse_returns_the_covering_claim(self, db):
        st = mk(db)
        a = st.request(items("gpu0:x", "solidpc:s"), owner("A"))
        again = st.request(items("gpu0:s"), owner("A"))
        assert again.reused and again.claim.id == a.claim.id
        other = st.request(items("gpu0:x"), owner("A"), reuse=False)
        assert not other.granted and other.self_blocked

    def test_a_job_following_another_process_is_never_reused(self, db):
        # 2026-10-22: a queued follow-up bench (--follow-pid its own
        # script) inherited the session id of the bench it queued behind,
        # was answered ALREADY HELD with the bench's claim, and ran at once.
        alive = {200: True, 300: True}
        st = mk(db, alive=lambda pid, started: alive.get(pid, True))
        st.define("ollama-cloud", capacity=2)
        bench = st.request(items("ollama-cloud:s:2"), owner("A", pid=200))
        follow = st.request(items("ollama-cloud:s:2"), owner("A", pid=300))
        assert bench.granted
        assert not follow.reused and not follow.granted
        assert not follow.self_blocked        # ends when pid 200 does
        alive[200] = False
        assert st.get(follow.claim.id).granted

    def test_self_blocked_only_behind_the_same_process(self, db):
        st = mk(db)
        st.request(items("gpu0:x"), owner("A", pid=200))
        detached = st.request(items("gpu0:x"), owner("A", pid=300))
        assert not detached.granted and detached.block.by is not None
        assert not detached.self_blocked      # pid 200 ends on its own
        same = st.request(items("gpu0:x"), owner("A", pid=200), reuse=False)
        assert same.self_blocked

    def test_a_claim_on_a_dead_process_is_refused_not_crashed(self, db):
        st = mk(db, alive=lambda pid, started: False)
        with pytest.raises(SpecError):
            st.request(items("gpu0"), owner("A", pid=99))
        assert st.status()[0] == []

    def test_capacity_from_definition(self, db):
        st = mk(db)
        st.define("ollama-cloud", capacity=2)
        outs = [st.request(items("ollama-cloud:s"), owner(f"S{i}"))
                for i in range(3)]
        assert [o.granted for o in outs] == [True, True, False]
        assert "capacity" in outs[2].block.describe()
        st.define("ollama-cloud", capacity=3)          # raised: let it in
        assert st.get(outs[2].claim.id).granted

    def test_session_release_by_resource(self, db):
        st = mk(db)
        st.request(items("gpu0"), owner("A"))
        st.request(items("gpu1"), owner("A"))
        done, _ = st.release(session_id="A", resources=["gpu1"])
        assert [c.label() for c in done] == ["gpu1:exclusive"]
        outs, _, _ = st.status()
        assert [o.claim.label() for o in outs] == ["gpu0:exclusive"]

    def test_waiter_wakes_when_released(self, db):
        st = mk(db)
        a = st.request(items("gpu0"), owner("A"))
        b = st.request(items("gpu0"), owner("B"))
        got = {}

        def waiter():
            w = mk(db)                     # its own connection, as a CLI is
            t0 = time.monotonic()
            got["out"] = w.wait(b.claim.id, timeout_s=20)
            got["s"] = time.monotonic() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.3)
        st.release([a.claim.id], session_id="A")
        t.join(25)
        assert got["out"] is not None and got["out"].granted
        assert got["s"] < 3

    def test_waiter_times_out_and_reports(self, db):
        st = mk(db)
        st.request(items("gpu0"), owner("A"))
        b = st.request(items("gpu0"), owner("B"))
        out = mk(db).wait(b.claim.id, timeout_s=0.6)
        assert out is not None and not out.granted

    def test_concurrent_deciders_grant_one(self, db):
        mk(db).status()                    # schema first
        results = []
        barrier = threading.Barrier(8)

        def grab(i):
            s = mk(db)
            barrier.wait()
            results.append(s.request(items("gpu0"), owner(f"S{i}")).granted)

        ts = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        assert sorted(results) == [False] * 7 + [True]


# ─── CLI ────────────────────────────────────────────────────────────────

def cli(*argv, env=None, timeout=60):
    e = dict(os.environ)
    e.update(env or {})
    e["CLAUDE_HOOKS_CLIENT_PID"] = "0"
    return subprocess.run([sys.executable, "-m", "claude_hooks.resources.cli",
                           *argv], cwd=REPO, env=e, capture_output=True,
                          text=True, timeout=timeout)


class TestCli:
    def test_acquire_busy_then_wait_gets_it_after_release(self, tmp_path):
        env = {"CLAUDE_HOOKS_RESOURCES_DB": str(tmp_path / "r.db")}
        a = cli("--session", "A", "acquire", "solidpc/gpu0", "-r", "kld",
                env=env)
        assert a.returncode == 0 and "GRANTED" in a.stdout
        cid = a.stdout.split()[1].rstrip(":")
        b = cli("--session", "B", "acquire", "solidpc/gpu0", env=env)
        assert b.returncode == 1 and "held by claude-hooks@" in b.stderr
        assert "#A" in b.stderr and "kld" in b.stderr
        e = dict(os.environ, **env, CLAUDE_HOOKS_CLIENT_PID="0")
        w = subprocess.Popen([sys.executable, "-m", "claude_hooks.resources.cli",
                              "--session", "B", "wait", "solidpc/gpu0",
                              "--timeout", "30s"], cwd=REPO, env=e,
                             stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        r = cli("--session", "A", "release", "--claim", cid, env=env)
        assert r.returncode == 0
        out, _ = w.communicate(timeout=30)
        assert w.returncode == 0 and "GRANTED" in out and "QUEUED" in out

    def test_run_holds_for_the_command_only(self, tmp_path):
        env = {"CLAUDE_HOOKS_RESOURCES_DB": str(tmp_path / "r.db")}
        r = cli("--session", "A", "run", "bs2/gpu1", "-r", "t", "--",
                sys.executable, "-c",
                "import subprocess,sys,os;"
                "r=subprocess.run([sys.executable,'-m','claude_hooks.resources.cli',"
                "'check','bs2/gpu1'],capture_output=True,text=True);"
                "sys.exit(7 if 'BUSY' in r.stdout else 0)", env=env)
        assert r.returncode == 7, r.stdout + r.stderr
        assert cli("check", "bs2/gpu1", env=env).returncode == 0

    def test_quiet_check(self, tmp_path):
        env = {"CLAUDE_HOOKS_RESOURCES_DB": str(tmp_path / "r.db"),
               "CLAUDE_HOOKS_HOST": "solidpc"}
        assert cli("quiet-check", env=env).returncode == 0
        assert cli("--session", "A", "acquire", "solidpc/gpu0:quiet",
                   env=env).returncode == 0
        q = cli("--session", "B", "quiet-check", env=env)
        assert q.returncode == 1 and "QUIET" in q.stdout

    def test_follow_pid_ties_the_claim_to_a_detached_job(self, tmp_path):
        env = {"CLAUDE_HOOKS_RESOURCES_DB": str(tmp_path / "r.db")}
        job = subprocess.Popen([sys.executable, "-c",
                                "import time; time.sleep(60)"])
        try:
            a = cli("--session", "A", "acquire", "bs2/gpu0", "--follow-pid",
                    str(job.pid), env=env)
            assert a.returncode == 0, a.stderr
            assert cli("check", "bs2/gpu0", env=env).returncode == 1
        finally:
            job.kill()
            job.wait()
        assert cli("check", "bs2/gpu0", env=env).returncode == 0
        assert cli("acquire", "x", "--follow-pid", "999999999",
                   env=env).returncode == 2

    def test_bad_spec_is_usage_error(self, tmp_path):
        env = {"CLAUDE_HOOKS_RESOURCES_DB": str(tmp_path / "r.db")}
        assert cli("acquire", "Bad Name", env=env).returncode == 2


# ─── Postgres (live, opt-in) ────────────────────────────────────────────

PG = os.environ.get("CLAUDE_HOOKS_RESOURCES_TEST_DSN", "")


@pytest.mark.skipif(not PG, reason="set CLAUDE_HOOKS_RESOURCES_TEST_DSN")
class TestPostgresLive:
    """Runs in a throw-away schema, never the shared tables."""

    @pytest.fixture
    def pgdsn(self):
        import psycopg
        schema_name = "res_test_" + uuid.uuid4().hex[:8]
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema_name}")
        sep = "&" if "?" in PG else "?"
        yield f"{PG}{sep}options=-csearch_path%3D{schema_name}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {schema_name} CASCADE")

    def test_a_table_from_before_ttl_s_is_migrated_in_place(self, pgdsn):
        import psycopg
        st = ResourceStore("postgres", pgdsn, host="solidpc",
                           alive=lambda p, s: True)
        st._run(lambda cur: None)
        with psycopg.connect(pgdsn, autocommit=True) as c:
            c.execute("ALTER TABLE resource_claims DROP COLUMN ttl_s")
        old = ResourceStore("postgres", pgdsn, host="solidpc",
                            alive=lambda p, s: True)
        assert old.snapshot() == []
        a = old.request(items("gpu0"), owner("A"))
        b = old.request(items("gpu0"), owner("B"), ttl_s=60)
        assert b.claim.ttl_s == 60 and b.claim.expires_at == ""
        old.release([a.claim.id], force=True)
        assert old.get(b.claim.id).claim.expires_at

    def test_listen_wakes_waiter_fast(self, pgdsn):
        st = ResourceStore("postgres", pgdsn, host="solidpc",
                           alive=lambda p, s: True)
        a = st.request(items("gpu0"), owner("A"))
        b = st.request(items("gpu0"), owner("B"))
        got = {}

        def waiter():
            w = ResourceStore("postgres", pgdsn, host="solidpc",
                              alive=lambda p, s: True)
            t0 = time.monotonic()
            got["out"] = w.wait(b.claim.id, timeout_s=30)
            got["s"] = time.monotonic() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(1.0)
        st.release([a.claim.id], session_id="A")
        t.join(35)
        assert got["out"].granted
        # Released after ~1 s; the poll alone would take 15 s.
        assert got["s"] < 5

    def test_concurrent_deciders_grant_one(self, pgdsn):
        ResourceStore("postgres", pgdsn, host="h").status()
        results = []
        barrier = threading.Barrier(6)

        def grab(i):
            s = ResourceStore("postgres", pgdsn, host="h",
                              alive=lambda p, st: True)
            barrier.wait()
            results.append(s.request(items("gpu0"), owner(f"S{i}")).granted)

        ts = [threading.Thread(target=grab, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        assert sorted(results) == [False] * 5 + [True]
