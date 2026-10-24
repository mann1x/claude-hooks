"""The per-run PolyKV manager (plan F3) against a fake engine.

Off and unsupported backends send the payload untouched; pooled lanes
share one root made once; a layer the engine refuses, or whose head is
not a prefix of the full render, shares less and never fails a call; a
pool the engine lost is rebuilt a bounded number of times; a restart
drops every pool; release closes every session and reports leaks.
"""

from __future__ import annotations

import threading
import time

import pytest

from claude_hooks.polykv.client import PolykvClient, Reply
from consultants.engine import polykv
from tests.test_polykv_client import FakeEngine, direct_fetch

SYSTEM = [{"role": "system", "content": "GROUNDING " + "g" * 2000},
          {"role": "system", "content": "You are the researcher."}]


class PoolEngine(FakeEngine):
    def __init__(self, *, features=("polykv_shared_root_v1", "stream_keepalive_v1"),
                 pools_enabled=True):
        super().__init__()
        self.features = list(features)
        self.pools_enabled = pools_enabled

    def handle(self, method, route, params, data):
        if route == "props" and self.refuse is None:
            self.requests.append((method, route, params, data))
            return Reply(200, {"features": self.features, "opencoti": {
                "polykv": {"pools_enabled": self.pools_enabled}}})
        return super().handle(method, route, params, data)

    def creates(self):
        return [r for r in self.requests if r[1] == "polykv/pools" and r[0] == "POST"]


class FakeChat:
    """Answers like a placed opencoti call; ``reply`` shapes the record."""

    def __init__(self):
        self.payloads = []
        self.reply = lambda placement: {"cache_n": 600, "n_pool_shared": 600,
                                        "pool_id": placement.get("pool_id")}
        self.lock = threading.Lock()

    def chat(self, payload):
        with self.lock:
            self.payloads.append(payload)
        p = payload.get("placement")
        out = {"choices": [{"message": {"content": "ok"}}],
               "usage": {"prompt_tokens": 700}, "timings": {}}
        if p:
            out["timings"]["polykv"] = {k: v for k, v in self.reply(p).items()
                                        if v is not None}
        return out

    chat_streamed = chat


def _mgr(engine, mode="pooled", **kw):
    def root_for(client, model):
        if getattr(client, "no_root", False):
            return None
        return polykv.Root(key="direct|e", kind="direct",
                           client=PolykvClient.direct("http://e",
                                                      fetch=direct_fetch(engine)))
    return polykv.KvRunManager("run-1", mode=mode, root_for=root_for,
                               show=lambda b, m: {}, **kw)


def _payload(item="lane 1: read retry.py", model="q"):
    return {"model": model, "messages": SYSTEM + [
        {"role": "user", "content": f"QUESTION\n{item}"}]}


# ------------------------------------------------------------ off is off

def test_off_and_no_root_send_the_payload_untouched():
    eng = PoolEngine()
    chat = FakeChat()
    for mgr, client in ((_mgr(eng, mode="off"), chat),
                        (_mgr(eng), type("C", (FakeChat,), {"no_root": True})())):
        p = _payload()
        polykv.PlacedChat(client, mgr, role="researcher").chat(p)
        assert client.payloads[-1] is p and "placement" not in p
    assert eng.requests == []


# ------------------------------------------------------------ pooled lanes

def test_lanes_share_one_root_made_once_with_their_own_sessions():
    eng = PoolEngine()
    orig = eng.handle

    def slow_create(method, route, params, data):
        if route == "polykv/pools" and method == "POST":
            time.sleep(0.05)               # let every lane reach the layer
        return orig(method, route, params, data)
    eng.handle = slow_create
    chat = FakeChat()
    mgr = _mgr(eng)
    placed = polykv.PlacedChat(chat, mgr, role="researcher")
    threads = [threading.Thread(target=placed.chat, args=(_payload(f"lane {i}"),))
               for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    places = [p["placement"] for p in chat.payloads]
    assert len({p["session_id"] for p in places}) == 4       # one per agent
    assert {p["pool_id"] for p in places} == {"0"}           # one root, pool 0
    assert len(eng.creates()) == 1
    body = eng.creates()[0][3]
    assert body["shared"] is True and body["ephemeral"] is True
    assert "QUESTION" not in body["prompt"] and "GROUNDING" in body["prompt"]
    assert all(p["keepalive"] and p["return_progress"] for p in places)
    st = mgr.stats["researcher"]
    assert (st.calls, st.placed, st.pooled, st.shared_tokens) == (4, 4, 4, 2400)
    assert not any(k.startswith("_") for p in places for k in p)


def test_a_tool_loop_keeps_its_session_across_turns():
    eng = PoolEngine()
    chat = FakeChat()
    placed = polykv.PlacedChat(chat, _mgr(eng), role="researcher")
    p = _payload()
    placed.chat(p)
    p2 = {**p, "messages": p["messages"] + [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "content": "file text"}]}
    placed.chat(p2)
    a, b = (x["placement"]["session_id"] for x in chat.payloads)
    assert a == b and len(eng.creates()) == 1


def test_concurrent_calls_of_one_identity_get_distinct_sessions():
    mgr = _mgr(PoolEngine())
    a = mgr.session_for("critic", "q", _payload()["messages"], "direct|e")
    b = mgr.session_for("critic", "q", _payload()["messages"], "direct|e")
    assert a != b and b == a + "~2"
    mgr._done(a)
    assert mgr.session_for("critic", "q", _payload()["messages"], "direct|e") == a


# ----------------------------------------------------- sharing less, safely

def test_a_short_head_is_not_pooled_but_keeps_affinity():
    eng = PoolEngine()
    chat = FakeChat()
    mgr = _mgr(eng, min_layer_chars=10 ** 6)
    polykv.PlacedChat(chat, mgr, role="planner").chat(_payload())
    place = chat.payloads[-1]["placement"]
    assert "pool_id" not in place and place["session_id"].startswith("run-1~planner~")
    assert eng.creates() == []


def test_a_head_that_is_not_a_prefix_is_never_pooled():
    class Reorders(PoolEngine):
        def handle(self, method, route, params, data):
            if route == "apply-template" and data["messages"][-1]["content"] != polykv.SENTINEL:
                self.requests.append((method, route, params, data))
                return Reply(200, {"prompt": "<tools first>" + "".join(
                    m["content"] for m in data["messages"])})
            return super().handle(method, route, params, data)
    eng = Reorders()
    chat = FakeChat()
    mgr = _mgr(eng)
    placed = polykv.PlacedChat(chat, mgr, role="researcher")
    placed.chat(_payload())
    placed.chat(_payload("lane 2"))
    assert eng.creates() == []
    assert all("pool_id" not in p["placement"] for p in chat.payloads)
    assert any(e["kind"] == "layer_refused" for e in mgr.events)


def test_a_refused_create_never_fails_the_call():
    eng = PoolEngine()
    chat = FakeChat()
    mgr = _mgr(eng)

    def refuse(method, route, params, data, _orig=eng.handle):
        if route == "polykv/pools" and method == "POST":
            return Reply(503, {"error": {"message": "no room in the KV cache"}})
        return _orig(method, route, params, data)
    eng.handle = refuse
    out = polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
    assert out["choices"][0]["message"]["content"] == "ok"
    assert "pool_id" not in chat.payloads[-1]["placement"]


def test_auto_without_shared_pools_is_affinity():
    eng = PoolEngine(features=(), pools_enabled=False)
    chat = FakeChat()
    mgr = _mgr(eng, mode="auto")
    polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
    assert set(chat.payloads[-1]["placement"]) == {"session_id"}
    assert mgr.roots["direct|e"].mode == "affinity"


@pytest.mark.parametrize("show,mode", [
    ({"xollama": {"council": {"enabled": True}}}, "off"),
    ({"xollama": {"session": {"client_pools": 0}}}, "affinity"),
    ({"xollama": {"session": {"client_pools": 6}}}, "pooled"),
])
def test_xollama_council_models_and_seatless_models(show, mode):
    eng = PoolEngine()

    def root_for(client, model):
        return polykv.Root(key="xollama|x|q", kind="xollama",
                           client=PolykvClient.direct("http://e",
                                                      fetch=direct_fetch(eng)))
    mgr = polykv.KvRunManager("r", mode="pooled", root_for=root_for,
                              show=lambda b, m: show)
    mgr.roots["xollama|x|q"] = root_for(None, "q")
    mgr.roots["xollama|x|q"].client.transport.base = "http://x"
    mgr.roots["xollama|x|q"].client.transport.model = "q"
    mgr._client_roots[(id(FakeChat), "q")] = "xollama|x|q"
    root = mgr.roots["xollama|x|q"]
    mgr._settle_mode(root, "q")
    assert root.mode == mode


def test_an_unloaded_xollama_engine_is_probed_again():
    eng = PoolEngine()
    eng.refuse = Reply(503, {"error": {"message": "model not loaded"}})
    t = {"now": 100.0}
    mgr = polykv.KvRunManager("r", mode="pooled", root_for=lambda c, m: None,
                              show=lambda b, m: {"xollama": {"session": {
                                  "client_pools": 6}}},
                              clock=lambda: t["now"])
    root = polykv.Root(key="xollama|x|q", kind="xollama",
                       client=PolykvClient.direct("http://e", fetch=direct_fetch(eng)))
    root.client.transport.base, root.client.transport.model = "http://x", "q"
    mgr._settle_mode(root, "q")
    assert root.mode == "affinity" and not root.settled
    eng.refuse = None
    mgr._settle_mode(root, "q")            # inside REPROBE_S: not asked
    assert root.mode == "affinity"
    t["now"] += polykv.REPROBE_S + 1
    mgr._settle_mode(root, "q")
    assert root.mode == "pooled" and root.settled


# -------------------------------------------------- losses and restarts

def test_a_lost_pool_is_rebuilt_then_given_up():
    eng = PoolEngine()
    chat = FakeChat()
    chat.reply = lambda p: {"pool_id": p.get("pool_id"), "pool_unknown": True,
                            "warnings": ["pool unknown to this engine"]}
    mgr = _mgr(eng)
    placed = polykv.PlacedChat(chat, mgr, role="researcher")
    for _ in range(polykv.MAX_REBUILDS + 2):
        placed.chat(_payload())
    assert len(eng.creates()) == polykv.MAX_REBUILDS
    assert "pool_id" not in chat.payloads[-1]["placement"]


def test_a_restart_drops_every_pool():
    eng = PoolEngine()
    chat = FakeChat()
    mgr = _mgr(eng)
    placed = polykv.PlacedChat(chat, mgr, role="researcher")
    placed.chat(_payload())
    root = mgr.roots["direct|e"]
    assert root.boot_id == "boot-A" and root.pools
    chat.reply = lambda p: {"pool_id": p.get("pool_id"), "boot_id": "boot-B",
                            "cache_n": 600}
    placed.chat(_payload())
    assert root.generation == 1 and root.pools == {} and root.boot_id == "boot-B"
    chat.reply = lambda p: {"pool_id": p.get("pool_id"), "cache_n": 600}
    placed.chat(_payload())
    assert len(eng.creates()) == 2


# --------------------------------------------------------------- release

def test_release_closes_every_session_and_reports_leaks():
    eng = PoolEngine()
    chat = FakeChat()
    mgr = _mgr(eng)
    placed = polykv.PlacedChat(chat, mgr, role="researcher")
    placed.chat(_payload("a"))
    placed.chat(_payload("b"))
    leaked = next(iter(mgr.sessions))
    eng.sessions[leaked] = {"window": 1}       # the engine kept one booked
    eng.handle_close = None
    orig = eng.handle

    def keep(method, route, params, data):
        if route.endswith("/close") and leaked in route:
            return Reply(200, {"session_id": leaked, "found": False})
        return orig(method, route, params, data)
    eng.handle = keep
    rep = mgr.release_all()
    assert rep["closed"] == 2
    assert rep["leaked_allocations"] == [leaked]
    assert rep["stats"]["researcher"]["placed"] == 2
    assert mgr.release_all()["already"] is True
    assert mgr.place(chat, _payload(), role="researcher") is None


def test_snapshot_round_trip_and_registry():
    eng = PoolEngine()
    chat = FakeChat()
    mgr = _mgr(eng)
    polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
    snap = mgr.snapshot()
    other = _mgr(eng)
    other._root(chat, "q")
    other.restore(snap)
    assert other.roots["direct|e"].pools == mgr.roots["direct|e"].pools
    assert other.sessions == mgr.sessions
    polykv.register(mgr)
    assert polykv.manager_for("run-1") is mgr
    assert polykv.drop("run-1") is mgr and polykv.manager_for("run-1") is None


def test_placement_failure_sends_unplaced(monkeypatch):
    chat = FakeChat()
    mgr = _mgr(PoolEngine())
    monkeypatch.setattr(mgr, "place", lambda *a, **k: 1 / 0)
    p = _payload()
    polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
    assert chat.payloads[-1] is p


# ---------------------------------------------------------- run lifecycle

def test_off_config_builds_no_manager_and_wraps_nothing(tmp_path):
    from consultants import config as cc
    from consultants.server import runner
    cfg = cc.ConsultantsConfig()
    state = type("S", (), {"sid": "s-off"})()
    assert runner._polykv_manager(cfg, state, str(tmp_path), {}) is None
    raw = FakeChat()
    assert polykv.placed(raw, None, "planner") is raw
    cfg.polykv.enabled, cfg.polykv.mode = True, "off"
    assert runner._polykv_manager(cfg, state, str(tmp_path), {}) is None


def test_on_config_registers_and_a_follow_up_unwraps_the_parent(tmp_path):
    from consultants import config as cc
    from consultants.server import runner
    cfg = cc.ConsultantsConfig()
    cfg.polykv.enabled = True
    state = type("S", (), {"sid": "s-on"})()
    mgr = runner._polykv_manager(cfg, state, str(tmp_path), {})
    assert state._polykv is mgr and polykv.manager_for("s-on") is mgr
    raw = FakeChat()
    wrapped = polykv.placed(raw, mgr, "critic")
    again = polykv.placed(wrapped, mgr, "critic")
    assert again._placed_inner is raw                 # never wrapped twice
    polykv.drop("s-on")


def test_settle_releases_a_finished_run_and_writes_the_report(tmp_path):
    eng = PoolEngine()
    chat = FakeChat()
    mgr = polykv.register(_mgr(eng))
    polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
    out = polykv.settle_run("run-1", cwd=str(tmp_path), status="completed")
    assert out["closed"] == 1 and out["status"] == "completed"
    saved = polykv.snapshot_path(str(tmp_path), "run-1")
    assert '"closed": 1' in saved.read_text()
    assert polykv.manager_for("run-1") is None
    assert polykv.settle_run("run-1", cwd=str(tmp_path), status="completed") is None


def test_a_suspended_run_keeps_its_sessions_and_resumes_them(tmp_path):
    eng = PoolEngine()
    chat = FakeChat()
    mgr = polykv.register(_mgr(eng))
    polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
    closes_before = [r for r in eng.requests if r[1].endswith("close")]
    snap = polykv.settle_run("run-1", cwd=str(tmp_path), status="suspended")
    assert snap["suspended"] and snap["sessions"]
    assert [r for r in eng.requests if r[1].endswith("close")] == closes_before
    pools = dict(mgr.roots["direct|e"].pools)

    def root_for(client, model):
        return polykv.Root(key="direct|e", kind="direct",
                           client=PolykvClient.direct("http://e",
                                                      fetch=direct_fetch(eng)))
    back = polykv.start_run("run-1", mode="pooled", cwd=str(tmp_path),
                            resume=True, root_for=root_for, show=lambda b, m: {})
    assert back.sessions == mgr.sessions
    polykv.PlacedChat(chat, back, role="researcher").chat(_payload())
    assert back.roots["direct|e"].pools == pools        # same boot: kept
    assert len(eng.creates()) == 1                       # no rebuild
    polykv.drop("run-1")


def test_run_with_state_settles_before_the_inflight_record(tmp_path, monkeypatch):
    from consultants.server import app as app_mod
    order = []
    monkeypatch.setattr(polykv, "settle_run",
                        lambda sid, **kw: order.append(("polykv", kw["status"])))
    monkeypatch.setattr(app_mod, "_settle_inflight",
                        lambda st: order.append(("inflight", st.status)))
    st = app_mod.SessionState(sid="s-x", question="q", cwd=str(tmp_path),
                              effort="high", topology="council")
    st._polykv = object()

    def run(state, runner_input):
        state.status = "failed"
    app_mod._run_with_state(type("A", (), {"state": None})(), run, st, {})
    assert order[:2] == [("polykv", "failed"), ("inflight", "failed")]
    assert st._polykv is None


class TestConfig:

    def test_default_off_and_round_trip(self, tmp_path):
        from consultants import config as cc
        assert cc.ConsultantsConfig().polykv.enabled is False
        cfg = cc.set_polykv(enabled=True, mode="affinity", scope="project",
                            cwd=tmp_path)
        assert (cfg.polykv.enabled, cfg.polykv.mode) == (True, "affinity")
        text = (tmp_path / ".claude-hooks" / "consultants.toml").read_text()
        assert "[polykv]" in text and 'mode = "affinity"' in text
        back = cc.load_config(cwd=tmp_path)
        assert (back.polykv.enabled, back.polykv.mode) == (True, "affinity")
        with pytest.raises(ValueError):
            cc.set_polykv(mode="sometimes", scope="project", cwd=tmp_path)

    def test_default_writes_no_section(self, tmp_path):
        from consultants import config as cc
        cc.set_polykv(enabled=False, scope="project", cwd=tmp_path)
        text = (tmp_path / ".claude-hooks" / "consultants.toml").read_text()
        assert "[polykv]" not in text

    def test_cli(self):
        from consultants import cli
        args = cli.build_parser().parse_args(
            ["config", "set-polykv", "on", "--mode", "pooled"])
        assert args.fn is cli.cmd_config_set_polykv
        assert (args.state, args.mode) == ("on", "pooled")


# ------------------------------------------- live findings (eleven2go, F3)

class SizedEngine(PoolEngine):
    """Pools answer ``own_len`` as one cell per two prompt characters."""

    def handle(self, method, route, params, data):
        out = super().handle(method, route, params, data)
        if route == "polykv/pools" and method == "POST" and out.status == 200:
            pid = out.body["pool_id"]
            self.pools[pid]["own_len"] = len(data.get("prompt") or "") // 2
            self.pools[pid]["attached_ever"] = False
            out = Reply(200, dict(self.pools[pid]))
        return out


class TestNeverAttachedPools:
    """An ephemeral pool is swept only once attached: a layer every lane
    was refused on stayed on the engine (pool 0, 16,603 cells)."""

    def test_release_frees_a_layer_nobody_attached(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        assert list(eng.pools) == [0]
        report = mgr.release_all()
        assert report["released_pools"] == ["0"]
        assert eng.pools == {} and report["leaked_pools"] == []

    def test_an_attached_layer_is_unpinned_and_left_to_the_sweep(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        assert eng.pools[0]["pinned"] is True       # pinned while in use
        eng.pools[0]["attached_ever"] = True
        report = mgr.release_all()
        assert report["released_pools"] == [] and list(eng.pools) == [0]
        assert report["unpinned_pools"] == ["0"] and eng.pools[0]["pinned"] is False
        assert report["leaked_pools"] == []

    def test_a_layer_that_could_not_be_released_is_reported(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        orig = eng.handle

        def no_release(method, route, params, data):
            if route.endswith("/release"):
                return Reply(500, {"error": {"message": "busy"}})
            return orig(method, route, params, data)
        eng.handle = no_release
        eng.pools[0]["pinned"] = False   # unpinned and never attached: never swept
        report = mgr.release_all()
        assert report["leaked_pools"] == [0]
        assert any("pool 0" in e for e in report["close_errors"])

    def test_created_pools_survive_a_snapshot(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        back = _mgr(eng)
        back.restore(mgr.snapshot())
        back._root(FakeChat(), "q")
        assert back.created == [("direct|e", "0")]
        assert back.roots["direct|e"].sizes == mgr.roots["direct|e"].sizes
        assert back.release_all()["released_pools"] == ["0"]


class TestPrivateWindow:
    """With no window the engine charged the whole prompt again on top of
    the pool; a stated ``num_ctx`` counts private tokens only."""

    def _placed(self, payload):
        eng = SizedEngine()
        chat = FakeChat()
        mgr = _mgr(eng)
        polykv.PlacedChat(chat, mgr, role="researcher").chat(payload)
        return chat.payloads[-1]["placement"], mgr

    def test_window_is_the_private_part_plus_the_output(self):
        p = _payload()
        p["options"] = {"num_predict": 4000}
        pl, mgr = self._placed(p)
        cells, chars = mgr.roots["direct|e"].sizes["0"]
        assert cells == chars // 2
        # the private part is the user turn, far below the layer
        assert 4000 + polykv.PRIVATE_FLOOR <= pl["num_ctx"] < 4000 + 1024
        assert pl["num_ctx"] % polykv.WINDOW_ALIGN == 0

    def test_a_longer_tail_asks_a_larger_window(self):
        short = _payload()
        short["options"] = {"num_predict": 1000}
        long = {**short, "messages": short["messages"] + [
            {"role": "assistant", "content": "x" * 4000},
            {"role": "user", "content": "y" * 4000}]}
        a, _ = self._placed(short)
        b, _ = self._placed(long)
        # 8000 chars at 2 chars/cell, with the margin
        assert b["num_ctx"] - a["num_ctx"] >= 4000 * polykv.PRIVATE_MARGIN - 256

    def test_max_tokens_counts_as_the_output(self):
        p = _payload()
        p["max_tokens"] = 2000
        pl, _ = self._placed(p)
        assert pl["num_ctx"] >= 2000 + polykv.PRIVATE_FLOOR

    def test_no_stated_output_states_no_window(self):
        pl, _ = self._placed(_payload())
        assert "pool_id" in pl and "num_ctx" not in pl

    def test_no_layer_size_states_no_window(self):
        eng = PoolEngine()                 # create answers no own_len
        chat = FakeChat()
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, _mgr(eng), role="researcher").chat(p)
        pl = chat.payloads[-1]["placement"]
        assert "pool_id" in pl and "num_ctx" not in pl


def test_the_layer_is_cut_from_the_inference_render():
    eng = SizedEngine()
    mgr = _mgr(eng)
    root = mgr._root(FakeChat(), "q")
    seen = []

    def render(messages, tools=None, think=None):
        seen.append(len(messages))
        return "".join(f"<{m['role']}>{m['content'].strip()}</>" for m in messages)
    root.client.render_chat = render
    polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
    prompt = eng.creates()[0][3]["prompt"]
    assert prompt == "".join(f"<system>{m['content'].strip()}</>" for m in SYSTEM) + "<user>"
    assert seen == [3, 3]                  # head + sentinel, then the full chat


class TestReclaimIdle:
    """A finished lane's session kept its window booked and every later
    lane was refused (5,669 of 32,768 free next to a 16,603-cell layer)."""

    class Refusing(FakeChat):
        def __init__(self, refusals=1):
            super().__init__()
            self.refusals = refusals

        def chat(self, payload):
            if self.refusals and len(self.payloads) >= 1:
                self.refusals -= 1
                self.payloads.append(payload)
                from claude_hooks.get_advice.chat_client import ChatFault
                raise ChatFault("ollama chat HTTP 429: admission rejected",
                                fault_class="transport", status=429)
            return FakeChat.chat(self, payload)

    def _closes(self, eng):
        return [r[1] for r in eng.requests if r[1].endswith("/close")]

    def test_an_idle_lane_is_closed_and_the_call_asked_again(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        chat = self.Refusing()
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        placed.chat(_payload("lane 0"))
        first = chat.payloads[0]["placement"]["session_id"]
        eng.sessions[first] = {"window": 10496}   # the engine keeps it booked
        out = placed.chat(_payload("lane 1"))
        assert out["choices"][0]["message"]["content"] == "ok"
        assert self._closes(eng) == [f"sessions/{first}/close"]
        assert any(e["kind"] == "reclaimed" for e in mgr.events)
        assert mgr.stats["researcher"].pooled == 2

    def test_nothing_idle_raises_the_refusal(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng = SizedEngine()
        mgr = _mgr(eng)
        chat = self.Refusing()
        chat.payloads.append({})            # refuse the very first call
        with pytest.raises(ChatFault):
            polykv.PlacedChat(chat, mgr, role="researcher").chat(_payload())
        assert self._closes(eng) == []

    def test_a_busy_lane_is_never_closed(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        p0 = mgr.place(FakeChat(), _payload("lane 0"), role="researcher")
        p1 = mgr.place(FakeChat(), _payload("lane 1"), role="researcher")
        assert mgr.reclaim_idle(p1) == 0          # lane 0 is mid-call
        assert self._closes(eng) == []
        mgr.observe(p0, None, role="researcher")  # lane 0 done
        eng.sessions[p0["session_id"]] = {"window": 10496}
        assert mgr.reclaim_idle(p1) == 1
        assert self._closes(eng) == [f"sessions/{p0['session_id']}/close"]

    def test_a_content_refusal_is_not_retried(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng = SizedEngine()
        mgr = _mgr(eng)

        class Bad(FakeChat):
            def chat(self, payload):
                if self.payloads:
                    raise ChatFault("HTTP 400: exceeds", fault_class="context_overflow",
                                    status=400)
                return FakeChat.chat(self, payload)
        placed = polykv.PlacedChat(Bad(), mgr, role="researcher")
        placed.chat(_payload("lane 0"))
        with pytest.raises(ChatFault):
            placed.chat(_payload("lane 1"))
        assert self._closes(eng) == []


class TestLayerLost:
    """The engine swept an unpinned layer between two lanes; the next
    attach to the missing id was charged in full (400, no share named)."""

    class Swept(FakeChat):
        def __init__(self, text):
            super().__init__()
            self.text = text
            self.refused = 0

        def chat(self, payload):
            p = payload.get("placement") or {}
            if self.payloads and p.get("pool_id") == "0" and not self.refused:
                self.refused += 1
                self.payloads.append(payload)
                from claude_hooks.get_advice.chat_client import ChatFault
                raise ChatFault(f"ollama chat HTTP 400: {self.text}",
                                fault_class="context_overflow", status=400)
            return FakeChat.chat(self, payload)

    def test_the_layer_is_built_again_and_the_call_resent(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        chat = self.Swept("request (16664 tokens) exceeds the available "
                          "context size (14592 tokens)")
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        placed.chat(_payload("lane 0"))
        out = placed.chat(_payload("lane 1"))
        assert out["choices"][0]["message"]["content"] == "ok"
        assert len(eng.creates()) == 2                 # rebuilt once
        assert chat.payloads[-1]["placement"]["pool_id"] == "1"
        assert any(e["kind"] == "pool_dropped" for e in mgr.events)
        assert not mgr._busy                           # both sessions freed

    def test_a_refusal_naming_the_share_is_the_callers(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng = SizedEngine()
        mgr = _mgr(eng)
        chat = self.Swept("request (16557 tokens, 1050 of them shareable "
                          "from pool 0) exceeds the available context size "
                          "(4096 private tokens)")
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        placed.chat(_payload("lane 0"))
        with pytest.raises(ChatFault):
            placed.chat(_payload("lane 1"))
        assert len(eng.creates()) == 1


class TestAudit:
    """The engine's pool list keeps pools it dropped; the audit asks each
    candidate by id."""

    def _run(self, eng):
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        return mgr

    def test_a_listed_pool_the_engine_no_longer_has_is_not_a_leak(self):
        eng = SizedEngine()
        mgr = self._run(eng)
        orig = eng.handle

        def stale(method, route, params, data):
            if route == "polykv/pools" and method == "GET":
                return Reply(200, {"pools": [{"pool_id": 0, "pinned": True}]})
            if route == "polykv/pools/0" and method == "GET" and 0 not in eng.pools:
                return Reply(400, {"error": {"message": "Invalid pool ID"}})
            return orig(method, route, params, data)
        eng.handle = stale
        report = mgr.release_all()
        assert report["released_pools"] == ["0"] and report["leaked_pools"] == []

    def test_a_layer_left_pinned_is_a_leak(self):
        eng = SizedEngine()
        mgr = self._run(eng)
        eng.pools[0]["attached_ever"] = True
        orig = eng.handle

        def no_unpin(method, route, params, data):
            if route.endswith("/unpin"):
                return Reply(500, {"error": {"message": "busy"}})
            return orig(method, route, params, data)
        eng.handle = no_unpin
        report = mgr.release_all()
        assert report["leaked_pools"] == [0]
        assert any("pool 0" in e for e in report["close_errors"])


class TestMakeRoom:
    """Lanes of one run queue on each other for room instead of each
    spending the client's Retry-After loop (60 s live) and failing."""

    def _eng(self, free):
        eng = SizedEngine()
        orig = eng.handle
        eng.free = free

        def handle(method, route, params, data):
            if route == "kv":
                eng.requests.append((method, route, params, data))
                return Reply(200, {"cells_free": eng.free, "allocations": []})
            if route.endswith("/close"):
                eng.free += 10000
            return orig(method, route, params, data)
        eng.handle = handle
        return eng

    def _p(self, item, out=4000):
        p = _payload(item)
        p["options"] = {"num_predict": out}
        return p

    def test_enough_room_sends_without_closing(self):
        eng = self._eng(free=20000)
        mgr = _mgr(eng)
        placed = polykv.PlacedChat(FakeChat(), mgr, role="researcher")
        placed.chat(self._p("lane 0"))
        placed.chat(self._p("lane 1"))
        assert not [r for r in eng.requests if r[1].endswith("/close")]

    def test_an_idle_sibling_is_closed_before_the_send(self):
        eng = self._eng(free=1000)
        mgr = _mgr(eng)
        chat = FakeChat()
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        placed.chat(self._p("lane 0"))
        first = chat.payloads[0]["placement"]["session_id"]
        eng.sessions[first] = {"window": 10000}
        placed.chat(self._p("lane 1"))
        assert [r[1] for r in eng.requests if r[1].endswith("/close")] == \
            [f"sessions/{first}/close"]

    def test_a_lane_waits_for_a_busy_sibling(self):
        eng = self._eng(free=1000)
        mgr = _mgr(eng)
        p0 = mgr.place(FakeChat(), self._p("lane 0"), role="researcher")
        eng.sessions[p0["session_id"]] = {"window": 10000}
        p1 = mgr.place(FakeChat(), self._p("lane 1"), role="researcher")
        got = []
        t = threading.Thread(target=lambda: got.append(mgr.make_room(p1, wait_s=5)))
        t.start()
        time.sleep(0.1)
        assert t.is_alive() and not got              # waiting on lane 0
        mgr.observe(p0, None, role="researcher")     # lane 0 ends
        t.join(2)
        assert got == [True]
        assert any(e["kind"] == "room" and e["waited"] for e in mgr.events)

    def test_no_sibling_means_no_wait(self):
        eng = self._eng(free=1000)
        mgr = _mgr(eng)
        p = mgr.place(FakeChat(), self._p("lane 0"), role="researcher")
        t0 = time.monotonic()
        assert mgr.make_room(p, wait_s=5) is False
        assert time.monotonic() - t0 < 1
        assert any(e["kind"] == "no_room" for e in mgr.events)

    def test_the_wait_is_bounded(self):
        eng = self._eng(free=1000)
        mgr = _mgr(eng)
        p0 = mgr.place(FakeChat(), self._p("lane 0"), role="researcher")
        p1 = mgr.place(FakeChat(), self._p("lane 1"), role="researcher")
        t0 = time.monotonic()
        assert mgr.make_room(p1, wait_s=0.3) is False   # lane 0 never ends
        assert time.monotonic() - t0 < 2
        assert p0

    def test_nothing_to_judge_is_none(self):
        eng = self._eng(free=0)
        mgr = _mgr(eng)
        p = mgr.place(FakeChat(), _payload(), role="researcher")   # no output stated
        assert mgr.make_room(p) is None
