"""ollama_slots: one account-wide cloud scope, per-server local scopes,
cross-process file-lock slots, FIFO inside a process, fail-open."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from claude_hooks import ollama_slots as os_  # noqa: E402


@pytest.fixture
def slots(monkeypatch, tmp_path):
    monkeypatch.delenv(os_.ENV_DISABLE, raising=False)
    monkeypatch.delenv(os_.ENV_CLOUD, raising=False)
    monkeypatch.setattr(os_, "slot_dir", lambda: tmp_path / "slots")
    monkeypatch.setattr(os_, "_plan_cache_path", lambda: tmp_path / "plan.json")
    monkeypatch.setattr(os_, "_remote_tags", lambda base: None)
    monkeypatch.setattr(os_, "_fetch_plan", lambda: "pro")
    os_._queues.clear()
    return tmp_path


def test_cloud_is_one_scope_across_relays_and_local_is_per_server(slots):
    cfg = {}
    a = os_.scope_for("http://192.168.178.161:11434", "glm-5.3:cloud", cfg)
    b = os_.scope_for("http://192.168.178.2:11433", "gemma4:31b-cloud", cfg)
    assert a == b == ("cloud", 2)  # pro 3 − 1 reserved for the hooks
    assert os_.scope_for("http://127.0.0.1:11434", "qwen3:8b", cfg) == (
        "local-127.0.0.1_11434", 2)


def test_the_server_decides_cloud_over_the_name(slots, monkeypatch):
    monkeypatch.setattr(os_, "_remote_tags",
                        lambda base: {"odd-name:latest": True, "x:cloud": False})
    assert os_.is_cloud("http://h", "odd-name:latest")
    assert not os_.is_cloud("http://h", "x:cloud")
    assert os_.is_cloud("http://h", "unlisted:cloud")  # falls back to the name


def test_limits_follow_plan_config_and_env(slots, monkeypatch):
    monkeypatch.setattr(os_, "_fetch_plan", lambda: "max")
    assert os_.cloud_limit({}) == 9
    (slots / "plan.json").unlink()
    monkeypatch.setattr(os_, "_fetch_plan", lambda: "free")
    assert os_.cloud_limit({}) == 1  # never 0 from the plan
    assert os_.cloud_limit({"ollama_slots": {"cloud_limit": 1}}) == 1
    monkeypatch.setenv(os_.ENV_CLOUD, "0")
    assert os_.scope_for("http://h", "m:cloud", {}) is None
    monkeypatch.setenv(os_.ENV_DISABLE, "1")
    monkeypatch.delenv(os_.ENV_CLOUD)
    assert os_.scope_for("http://h", "m:cloud", {}) is None


def test_the_plan_cache_keeps_only_the_plan(slots):
    assert os_.plan() == "pro"
    assert set(json.loads((slots / "plan.json").read_text())) == {"plan", "at"}


def test_a_full_scope_makes_the_next_call_wait(slots):
    cfg = {"ollama_slots": {"cloud_limit": 1}}
    order = []
    with os_.slot("http://h", "m:cloud", cfg=cfg) as sc:
        assert sc == "cloud"

        def second():
            with os_.slot("http://h", "m:cloud", cfg=cfg):
                order.append("second")
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.3)
        order.append("first-done")
    t.join(5)
    assert order == ["first-done", "second"]


def test_waiters_are_served_in_arrival_order(slots):
    cfg = {"ollama_slots": {"cloud_limit": 1}}
    got = []
    gate = threading.Event()

    def worker(i):
        with os_.slot("http://h", "m:cloud", cfg=cfg):
            got.append(i)
            gate.wait(0.05)
    with os_.slot("http://h", "m:cloud", cfg=cfg):
        ts = []
        for i in range(4):
            t = threading.Thread(target=worker, args=(i,))
            t.start()
            ts.append(t)
            time.sleep(0.1)  # arrival order 0, 1, 2, 3
    for t in ts:
        t.join(5)
    assert got == [0, 1, 2, 3]


def test_a_slot_held_by_another_process_counts(slots):
    d = slots / "slots" / "cloud"
    d.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys,time;sys.path.insert(0,%r);"
        "from claude_hooks import ollama_slots as o;"
        "f=open(%r,'a+b');assert o._try_lock(f);print('held',flush=True);"
        "time.sleep(30)") % (str(_REPO), str(d / "slot-0.lock"))],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        cfg = {"ollama_slots": {"cloud_limit": 1, "max_wait_s": 0.5}}
        t0 = time.monotonic()
        with os_.slot("http://h", "m:cloud", cfg=cfg) as sc:
            # Waited the whole budget, then failed open.
            assert sc is None and time.monotonic() - t0 >= 0.5
        holder.kill()
        holder.wait(5)
        # The OS released the dead holder's lock.
        with os_.slot("http://h", "m:cloud", cfg=cfg) as sc:
            assert sc == "cloud"
    finally:
        holder.kill()


def test_cancel_while_queued_raises_and_frees_the_queue(slots):
    cfg = {"ollama_slots": {"cloud_limit": 1}}
    with os_.slot("http://h", "m:cloud", cfg=cfg):
        cancelled = threading.Event()
        err = []

        def waiter():
            try:
                with os_.slot("http://h", "m:cloud", cfg=cfg,
                              cancel_check=cancelled.is_set):
                    pass
            except os_.Cancelled as e:
                err.append(e)
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)
        cancelled.set()
        t.join(5)
    assert err
    with os_.slot("http://h", "m:cloud", cfg=cfg) as sc:  # queue not wedged
        assert sc == "cloud"


def test_a_broken_limiter_fails_open(slots, monkeypatch):
    def boom(*a):
        raise PermissionError("read-only home")
    monkeypatch.setattr(os_, "_try_any", boom)
    with os_.slot("http://h", "m:cloud", cfg={}) as sc:
        assert sc is None


def test_the_stall_clock_restarts_on_admission():
    from consultants.engine.stall import StallController
    now = [100.0]
    c = StallController(started_ts=0.0, time_source=lambda: now[0])
    c.mark_token()
    c.restart_clock()
    p = c.progress()
    assert p.started_ts == 100.0 and p.last_token_ts is None


def test_stall_chat_passes_on_admitted_only_to_clients_that_take_it():
    from consultants.engine.stall_chat import _accepts

    def old(payload, *, on_token=None, cancel_check=None):
        pass

    def new(payload, *, on_token=None, cancel_check=None, on_admitted=None):
        pass
    assert not _accepts(old, "on_admitted") and _accepts(new, "on_admitted")


def test_a_waiter_cancelled_mid_queue_does_not_wedge_the_ones_behind(slots):
    cfg = {"ollama_slots": {"cloud_limit": 1}}
    stop_b = threading.Event()
    got = []

    def waiter(name, cancel=None):
        try:
            with os_.slot("http://h", "m:cloud", cfg=cfg, cancel_check=cancel):
                got.append(name)
        except os_.Cancelled:
            got.append(name + "-cancelled")
    with os_.slot("http://h", "m:cloud", cfg=cfg):
        a = threading.Thread(target=waiter, args=("a",))
        b = threading.Thread(target=waiter, args=("b", stop_b.is_set))
        c = threading.Thread(target=waiter, args=("c",))
        for t in (a, b, c):
            t.start()
            time.sleep(0.1)
        stop_b.set()  # b is queued behind a, not at the head
        time.sleep(0.8)
    for t in (a, b, c):
        t.join(5)
    assert got == ["b-cancelled", "a", "c"]
