"""The per-run PolyKV manager (plan F3) against a fake engine.

Off and unsupported backends send the payload untouched; pooled lanes
share one root made once; a layer the engine refuses, or whose head is
not a prefix of the full render, shares less and never fails a call; a
pool the engine lost is rebuilt a bounded number of times; a restart
drops every pool; release closes every session and reports leaks.
"""

from __future__ import annotations

import json
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
    assert root.reason == ""               # the not-loaded reason is gone


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

    def test_an_attached_layer_is_released_too(self):
        # Unpinned and left to the engine's sweep, three attached layers
        # outlived two councils on eleven2go and the next run's layer was
        # refused: "pool seq-id reservoir exhausted".
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        assert eng.pools[0]["pinned"] is True       # pinned while in use
        eng.pools[0]["attached_ever"] = True
        report = mgr.release_all()
        assert report["released_pools"] == ["0"] and eng.pools == {}
        assert report["unpinned_pools"] == [] and report["leaked_pools"] == []

    def test_a_layer_someone_still_holds_is_unpinned(self):
        eng = SizedEngine()
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        orig = eng.handle

        def held(method, route, params, data):
            if route.endswith("/release"):
                return Reply(409, {"error": {"message": "pool is attached"}})
            return orig(method, route, params, data)
        eng.handle = held
        report = mgr.release_all()
        assert report["unpinned_pools"] == ["0"] and eng.pools[0]["pinned"] is False
        assert report["released_pools"] == []

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
            if route.endswith("/release"):
                return Reply(409, {"error": {"message": "pool is attached"}})
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
            out = orig(method, route, params, data)
            if route.endswith("/close") and (out.body or {}).get("found"):
                eng.free += 10000
            return out
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

    def _full(self, item="plan"):
        # An unpooled call asking the engine's whole window again.
        p = _payload(item)
        p["options"] = {"num_ctx": 32768, "num_predict": 4000}
        return p

    def test_the_callers_own_cells_are_dropped_for_a_full_window(self):
        # csl-2026-10-24-1700-78ca: the planner's third call asked 32,768 of
        # a 32,768-cell engine while its own session held 1,151 — refused
        # for 47 min, nothing else of the run to close.
        eng = self._eng(free=40000)
        mgr = _mgr(eng, min_layer_chars=10 ** 6)       # no layer: unpooled
        chat = FakeChat()
        placed = polykv.PlacedChat(chat, mgr, role="planner")
        placed.chat(self._full())
        sid = chat.payloads[0]["placement"]["session_id"]
        eng.sessions[sid] = {"window": 1151}
        eng.free = 31617
        placed.chat(self._full())
        closes = [r for r in eng.requests if r[1].endswith("/close")]
        assert [r[1] for r in closes] == [f"sessions/{sid}/close"]
        assert closes[0][3].get("keep_running") is True
        assert any(e["kind"] == "dropped_own" and e["session"] == sid
                   for e in mgr.events)
        assert len(chat.payloads) == 2
        assert chat.payloads[1]["placement"]["session_id"] == sid

    def test_a_first_call_with_nothing_to_drop_is_sent_once(self):
        eng = self._eng(free=1000)
        mgr = _mgr(eng, min_layer_chars=10 ** 6)
        chat = FakeChat()
        polykv.PlacedChat(chat, mgr, role="planner").chat(self._full())
        assert len(chat.payloads) == 1
        assert not any(e["kind"] == "dropped_own" for e in mgr.events)
        assert any(e["kind"] == "no_room" for e in mgr.events)

    def test_the_payload_window_is_judged_when_the_placement_states_none(self):
        eng = self._eng(free=0)
        mgr = _mgr(eng, min_layer_chars=10 ** 6)
        p = self._full()
        pl = mgr.place(FakeChat(), p, role="planner")
        assert "num_ctx" not in pl
        assert mgr.make_room(pl) is None              # nothing it can see
        assert mgr.make_room(pl, payload=p, wait_s=0) is False

    def test_room_refusals_include_xollamas_no_room_503(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        no_room = ChatFault(
            'ollama chat HTTP 503: {"error":"the engine has had no room for '
            'this request for 2m0s; it is refusing new work rather than '
            'queueing it"}', fault_class="transport", status=503)
        assert polykv._admission_refused(no_room)
        assert not polykv._admission_refused(ChatFault(
            "ollama chat HTTP 503: model not loaded", fault_class="transport",
            status=503))


# ------------------------------------------------------------ assured

class OwnerEngine(SizedEngine):
    """Owned pools, a ``/kv`` with room, and resize."""

    def __init__(self, *, admissible=60000, ctx_max=65536, **kw):
        kw.setdefault("features", ("polykv_shared_root_v1", "polykv_subpools_v1"))
        super().__init__(**kw)
        self.admissible = admissible
        self.ctx_max = ctx_max
        self.resized = []

    def handle(self, method, route, params, data):
        if route == "kv":
            self.requests.append((method, route, params, data))
            return Reply(200, {"session_ctx_max": self.ctx_max,
                               "largest_admissible": self.admissible,
                               "cells_free": self.admissible, "allocations": []})
        if route.endswith("/resize"):
            self.requests.append((method, route, params, data))
            self.resized.append(data["num_ctx"])
            return Reply(200, {"num_ctx": data["num_ctx"]})
        return super().handle(method, route, params, data)


class OwnerChat(FakeChat):
    def __init__(self):
        super().__init__()
        base = self.reply
        self.reply = lambda p: {**base(p), "context_window": p.get("num_ctx")}

    def owners(self):
        return [p for p in self.payloads
                if (p.get("placement") or {}).get("session_id", "").count("~owner~")]

    def workers(self):
        return [p for p in self.payloads if p not in self.owners()]


def _assured(eng, **kw):
    return _mgr(eng, mode="assured", **kw)


class TestAssured:

    def _run(self, eng=None, lanes=3, out=4000):
        eng = eng or OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        for i in range(lanes):
            p = _payload(f"lane {i}")
            p["options"] = {"num_predict": out}
            placed.chat(p)
        return eng, chat, mgr

    def test_one_owner_books_the_window_and_owns_the_layer(self):
        eng, chat, mgr = self._run()
        (opener,) = chat.owners()
        pl = opener["placement"]
        assert pl["session_id"] == "run-1~owner~0"
        assert opener["options"] == {"num_predict": 1}
        assert pl["num_ctx_min"] <= pl["num_ctx"] <= 60000
        assert pl["num_ctx"] % polykv.WINDOW_ALIGN == 0
        (create,) = eng.creates()
        assert create[3]["session_id"] == "run-1~owner~0"
        assert "shared" not in create[3] and create[3]["pin"] is True
        root = mgr.roots["direct|e"]
        assert (root.mode, root.owner, root.owner_window) == (
            "assured", "run-1~owner~0", pl["num_ctx"])

    def test_workers_state_no_window(self):
        _, chat, mgr = self._run()
        ws = chat.workers()
        assert len(ws) == 3
        assert all(w["placement"]["pool_id"] == "0" for w in ws)
        assert not any("num_ctx" in w["placement"] for w in ws)
        assert mgr.stats["researcher"].pooled == 3

    def test_settle_closes_workers_then_the_owner(self):
        eng, chat, mgr = self._run()
        eng.sessions["run-1~owner~0"] = {"window": 1}
        report = mgr.release_all()
        closes = [r[1] for r in eng.requests if r[1].endswith("/close")]
        assert closes[-1] == "sessions/run-1~owner~0/close"
        assert len(closes) == 4                    # 3 workers, then the owner
        assert report["owners_closed"] == ["run-1~owner~0"]
        assert report["released_pools"] == [] and report["unpinned_pools"] == []

    def test_no_room_for_one_lane_pools_shared(self):
        eng, chat, mgr = self._run(OwnerEngine(admissible=1024))
        assert chat.owners() == []
        assert any(e["kind"] == "owner_refused" for e in mgr.events)
        (create,) = eng.creates()
        assert create[3]["shared"] is True
        assert all("num_ctx" in w["placement"] for w in chat.workers())

    def test_an_engine_without_owned_pools_runs_pooled(self):
        eng = OwnerEngine(features=("polykv_shared_root_v1",))
        _, chat, mgr = self._run(eng)
        root = mgr.roots["direct|e"]
        assert root.mode == "pooled" and "owned pools" in root.reason
        assert chat.owners() == []

    def test_an_owner_refused_opener_shares_less(self):
        eng = OwnerEngine()
        chat = OwnerChat()
        orig = chat.chat

        def refuse_owner(payload):
            if "~owner~" in (payload.get("placement") or {}).get("session_id", ""):
                raise RuntimeError("HTTP 429: no room")
            return orig(payload)
        chat.chat = refuse_owner
        mgr = _assured(eng)
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        assert eng.creates()[0][3]["shared"] is True
        assert mgr.roots["direct|e"].owner is None


class TestOwnerFull:
    """A worker refused as its owner is full is a queue, never a failure."""

    def _full_once(self, chat):
        from claude_hooks.get_advice.chat_client import ChatFault
        orig = chat.chat
        state = {"refused": 0}

        def chat_fn(payload):
            pl = payload.get("placement") or {}
            if pl.get("pool_id") is not None and not state["refused"]:
                state["refused"] += 1
                raise ChatFault("ollama chat HTTP 429: session allocation full "
                                "(worker of run-1~owner~0)",
                                fault_class="transport", status=429)
            return orig(payload)
        chat.chat = chat_fn
        return state

    def test_the_owner_grows_and_the_worker_is_resent(self):
        eng = OwnerEngine()
        chat = OwnerChat()
        self._full_once(chat)
        mgr = _assured(eng)
        p = _payload()
        p["options"] = {"num_predict": 4000}
        out = polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        assert out["choices"][0]["message"]["content"] == "ok"
        root = mgr.roots["direct|e"]
        assert eng.resized == [root.owner_window]
        assert any(e["kind"] == "owner_grown" for e in mgr.events)
        assert not [r for r in eng.requests if r[1].endswith("/close")]   # nothing reclaimed

    def test_no_room_to_grow_waits_for_a_sibling(self):
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        p = _payload("lane 0")
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)   # opens the owner
        eng.admissible = 0
        busy = mgr.place(chat, _payload("lane 1"), role="researcher")
        pl2 = mgr.place(chat, _payload("lane 2"), role="researcher")
        got = []
        t = threading.Thread(target=lambda: got.append(mgr.grow_owner(pl2)))
        t.start()
        time.sleep(0.1)
        assert t.is_alive()
        # a lane that started after the wait began ends: not the sibling
        late = mgr.place(chat, _payload("lane 3"), role="researcher")
        mgr.observe(late, None, role="researcher")
        time.sleep(0.1)
        assert t.is_alive() and not got
        mgr.observe(busy, None, role="researcher")
        t.join(2)
        assert got == [True] and eng.resized == []

    def test_nothing_to_wait_for_raises_the_refusal(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        p = _payload("lane 0")
        p["options"] = {"num_predict": 4000}
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        placed.chat(p)
        eng.admissible = 0
        self._full_once(chat)
        with pytest.raises(ChatFault):
            placed.chat({**p, "messages": _payload("lane 1")["messages"]})


class TestOwnerLost:

    def test_a_lost_owned_layer_opens_a_new_owner_and_closes_both(self):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        placed = polykv.PlacedChat(chat, mgr, role="researcher")
        p = _payload("lane 0")
        p["options"] = {"num_predict": 4000}
        placed.chat(p)
        orig = chat.chat
        state = {"n": 0}

        def gone(payload):
            pl = payload.get("placement") or {}
            if pl.get("pool_id") == "0" and not state["n"]:
                state["n"] += 1
                raise ChatFault("ollama chat HTTP 400: request (16664 tokens) "
                                "exceeds the available context size (14592 tokens)",
                                fault_class="context_overflow", status=400)
            return orig(payload)
        chat.chat = gone
        placed.chat({**p, "messages": _payload("lane 1")["messages"]})
        owners = [o["placement"]["session_id"] for o in chat.owners()]
        assert owners == ["run-1~owner~0", "run-1~owner~0~1"]
        assert eng.creates()[1][3]["session_id"] == "run-1~owner~0~1"
        for o in owners:
            eng.sessions[o] = {"window": 1}
        assert mgr.release_all()["owners_closed"] == owners

    def test_a_restart_forgets_the_owner(self):
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        root = mgr.roots["direct|e"]
        mgr._new_generation(root, "boot-B")
        assert root.owner is None and root.pools == {} and root.sizes == {}

    def test_the_owner_rides_a_snapshot(self):
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = _assured(eng)
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        back = _assured(eng)
        back.restore(mgr.snapshot())
        back._root(FakeChat(), "q")
        r = back.roots["direct|e"]
        assert (r.owner, r.owner_window) == (mgr.roots["direct|e"].owner,
                                             mgr.roots["direct|e"].owner_window)


def test_the_keepalive_touches_an_idle_owner(monkeypatch):
    monkeypatch.setattr(polykv, "OWNER_KEEPALIVE_S", 0.05)
    eng = OwnerEngine()
    chat = OwnerChat()
    mgr = _assured(eng)
    p = _payload()
    p["options"] = {"num_predict": 4000}
    polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any(
            e["kind"] == "owner_keepalive" for e in mgr.events):
        time.sleep(0.02)
    touches = [x for x in chat.owners() if "num_ctx" not in x["placement"]]
    assert touches and touches[0]["options"] == {"num_predict": 1}
    monkeypatch.setattr(polykv, "OWNER_KEEPALIVE_S", 30)
    time.sleep(0.1)                                # the loop is in a long wait
    mgr.release_all()
    mgr._keepalive.join(1)
    assert not mgr._keepalive.is_alive()          # stopped with the run


def test_no_admissible_room_is_no_owner():
    eng = OwnerEngine(admissible=0)
    chat = OwnerChat()
    mgr = _assured(eng)
    p = _payload()
    p["options"] = {"num_predict": 4000}
    polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
    assert chat.owners() == []
    assert [e for e in mgr.events if e["kind"] == "owner_refused"][0]["admissible"] == 0


# ------------------------------------------------------------ the hold

class TestHold:
    """A completed run whose consultancy is open keeps its owner and layers
    for the follow-up; accept or the deadline releases them."""

    def _held(self, tmp_path, mode="assured"):
        eng = OwnerEngine()
        chat = OwnerChat()
        mgr = polykv.register(_mgr(eng, mode=mode))
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        if mgr.roots["direct|e"].owner:
            eng.sessions[mgr.roots["direct|e"].owner] = {"window": 1}
        out = polykv.settle_run("run-1", cwd=str(tmp_path), status="completed",
                                hold_root="root-1")
        return eng, chat, mgr, out

    def test_hold_closes_lanes_and_keeps_owner_and_layer(self, tmp_path):
        eng, chat, mgr, out = self._held(tmp_path)
        assert out["held"] and out["hold_root"] == "root-1"
        closes = [r[1] for r in eng.requests if r[1].endswith("/close")]
        assert len(closes) == 1 and "~owner~" not in closes[0]   # the lane only
        root = mgr.roots["direct|e"]
        assert root.owner and root.pools and list(eng.pools) == [0]
        reg = json.loads(polykv.held_registry_path().read_text())
        assert list(reg) == ["root-1"]
        polykv.release_held("root-1")

    def test_a_follow_up_adopts_the_hold(self, tmp_path):
        eng, chat, mgr, _ = self._held(tmp_path)
        child = polykv.start_run("child-1", mode="assured", cwd=str(tmp_path),
                                 adopt_root="root-1")
        assert child is mgr and polykv.manager_for("child-1") is mgr
        assert json.loads(polykv.held_registry_path().read_text()) == {}
        p = _payload("lane 1")
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(chat, child, role="researcher").chat(p)
        assert len(eng.creates()) == 1 and len(chat.owners()) == 1   # reused
        assert chat.payloads[-1]["placement"]["pool_id"] == "0"
        assert any(e["kind"] == "adopted" and e["by"] == "child-1"
                   for e in mgr.events)
        polykv.drop("child-1")
        mgr.release_all()

    def test_accept_releases_and_writes_the_report(self, tmp_path):
        eng, chat, mgr, _ = self._held(tmp_path)
        out = polykv.release_held("root-1", reason="accepted")
        assert out["owners_closed"] == [mgr.roots["direct|e"].owner]
        assert eng.pools == {}                            # went with the owner
        saved = json.loads(polykv.snapshot_path(str(tmp_path), "run-1").read_text())
        assert saved["released"] == "accepted"
        assert polykv.release_held("root-1") is None      # once
        assert polykv.adopt("root-1") is None

    def test_the_hold_expires(self, tmp_path, monkeypatch):
        monkeypatch.setattr(polykv, "HOLD_S", 0.05)
        eng, chat, mgr, _ = self._held(tmp_path)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not mgr.released:
            time.sleep(0.02)
        assert mgr.released and eng.pools == {}
        saved = json.loads(polykv.snapshot_path(str(tmp_path), "run-1").read_text())
        assert saved["released"] == "expired"

    def test_a_follow_up_in_another_mode_starts_clean(self, tmp_path):
        eng, chat, mgr, _ = self._held(tmp_path)
        child = polykv.start_run("child-2", mode="affinity", cwd=str(tmp_path),
                                 adopt_root="root-1", show=lambda b, m: {})
        assert child is not mgr and mgr.released
        polykv.drop("child-2")

    def test_a_run_without_an_open_consultancy_is_released(self, tmp_path):
        eng = OwnerEngine()
        mgr = polykv.register(_mgr(eng, mode="assured"))
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(OwnerChat(), mgr, role="researcher").chat(p)
        out = polykv.settle_run("run-1", cwd=str(tmp_path), status="completed")
        assert mgr.released and "held" not in out
        out = None
        mgr2 = polykv.register(_mgr(eng, mode="assured"))
        polykv.PlacedChat(OwnerChat(), mgr2, role="researcher").chat(p)
        polykv.settle_run("run-1", cwd=str(tmp_path), status="failed",
                          hold_root="root-1")
        assert mgr2.released and polykv.adopt("root-1") is None

    def test_a_stale_hold_is_released_at_engine_start(self, tmp_path, monkeypatch):
        eng, chat, mgr, _ = self._held(tmp_path, mode="pooled")
        pid = next(iter(mgr.roots["direct|e"].pools.values()))
        # The process that held it is gone: its timer and memory with it.
        with polykv._HELD_LOCK:
            entry = polykv._HELD.pop("root-1")
        entry["timer"].cancel()
        monkeypatch.setattr(polykv, "client_for_key", lambda key: PolykvClient.direct(
            "http://e", fetch=direct_fetch(eng)) if key == "direct|e" else None)
        assert polykv.sweep_held() == ["root-1"]
        assert int(pid) not in eng.pools                  # never attached: released
        assert json.loads(polykv.held_registry_path().read_text()) == {}
        assert polykv.sweep_held() == []


def test_client_for_key_reads_both_kinds():
    d = polykv.client_for_key("direct|http://h:8240")
    x = polykv.client_for_key("xollama|http://h:22434|m:q")
    assert d.transport.kind == "direct" and x.transport.model == "m:q"
    assert polykv.client_for_key("cloud|x") is None


def test_run_with_state_holds_a_run_whose_consultancy_is_open(tmp_path, monkeypatch):
    from consultants.server import app as app_mod
    seen = {}
    monkeypatch.setattr(polykv, "settle_run",
                        lambda sid, **kw: seen.update(kw))
    monkeypatch.setattr(app_mod, "_settle_inflight", lambda st: None)
    root = app_mod.SessionState(sid="root-9", question="q", cwd=str(tmp_path),
                                effort="high", topology="council")
    st = app_mod.SessionState(sid="child-9", question="q", cwd=str(tmp_path),
                              effort="high", topology="council")
    st.root_sid = "root-9"
    st._polykv = object()
    monkeypatch.setattr(app_mod, "_persist_consultancy", lambda r: None)
    app = type("A", (), {"state": type("S", (), {"sessions": {"root-9": root}})()})()

    def run(state, runner_input):
        state.status = "completed"
    app_mod._run_with_state(app, run, st, {})
    assert seen["hold_root"] == "root-9"
    root.consultancy_status = app_mod.CONSULTANCY_ACCEPTED
    st._polykv = object()
    app_mod._run_with_state(app, run, st, {})
    assert seen["hold_root"] is None


class TestHoldEdges:

    def _hold(self, tmp_path, run_id, root="root-1"):
        eng = OwnerEngine()
        mgr = polykv.register(polykv.KvRunManager(
            run_id, mode="assured", show=lambda b, m: {},
            root_for=lambda c, m: polykv.Root(
                key="direct|e", kind="direct",
                client=PolykvClient.direct("http://e", fetch=direct_fetch(eng)))))
        p = _payload()
        p["options"] = {"num_predict": 4000}
        polykv.PlacedChat(OwnerChat(), mgr, role="researcher").chat(p)
        polykv.settle_run(run_id, cwd=str(tmp_path), status="completed",
                          hold_root=root)
        return eng, mgr

    def test_an_adopted_holds_timer_never_releases_the_next_hold(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(polykv, "HOLD_S", 0.1)
        _, mgr = self._hold(tmp_path, "run-a")
        child = polykv.start_run("run-b", mode="assured", cwd=str(tmp_path),
                                 adopt_root="root-1")
        monkeypatch.setattr(polykv, "HOLD_S", 30)
        polykv.settle_run("run-b", cwd=str(tmp_path), status="completed",
                          hold_root="root-1")
        time.sleep(0.4)                        # past the first deadline
        assert not child.released and polykv.adopt("root-1") is child
        child.release_all()

    def test_a_hold_closes_its_lanes_once(self, tmp_path):
        eng, mgr = self._hold(tmp_path, "run-c")
        polykv.release_held("root-1")
        lanes = [r[1] for r in eng.requests
                 if r[1].endswith("/close") and "~owner~" not in r[1]]
        assert len(lanes) == len(set(lanes)) == 1

    def test_a_second_hold_of_one_consultancy_releases_the_first(self, tmp_path):
        _, first = self._hold(tmp_path, "run-d")
        _, second = self._hold(tmp_path, "run-e")
        assert first.released and not second.released
        polykv.release_held("root-1")


def test_the_follow_up_runner_adopts(tmp_path):
    import inspect
    from consultants.server import runner
    src = inspect.getsource(runner.make_follow_up_runner)
    assert "_polykv_manager(cfg, state, cwd, runner_input, adopt=True)" in src

    class Cfg:
        polykv = type("P", (), {"enabled": True, "mode": "assured"})()
    held = polykv.KvRunManager("run-p", mode="assured", show=lambda b, m: {})
    polykv._hold("root-p", held, tmp_path / "polykv.json")
    st = type("St", (), {"sid": "child-p", "root_sid": "root-p", "_polykv": None})()
    assert runner._polykv_manager(Cfg, st, str(tmp_path), {}, adopt=True) is held
    st2 = type("St", (), {"sid": "root-q", "root_sid": "root-q", "_polykv": None})()
    fresh = runner._polykv_manager(Cfg, st2, str(tmp_path), {}, adopt=True)
    assert fresh is not held                      # a root never adopts itself
    polykv.drop("child-p"), polykv.drop("root-q")


class TestColdBudget:
    """A cold model's budget asked a 96k output of a 32k engine; the
    make_room loop then re-closed the same sessions 150 times."""

    class Ceiling(SizedEngine):
        def handle(self, method, route, params, data):
            if route == "props" and self.refuse is None:
                self.requests.append((method, route, params, data))
                return Reply(200, {"features": self.features, "opencoti": {
                    "polykv": {"pools_enabled": True},
                    "kv": {"session_ctx_max": 32768}}})
            if route == "kv":
                self.requests.append((method, route, params, data))
                return Reply(200, {"cells_free": 1000, "allocations": []})
            if route.endswith("/close"):
                self.requests.append((method, route, params, data))
                return Reply(200, {"found": True, "released": True})
            return super().handle(method, route, params, data)

    def test_a_window_above_the_engine_ceiling_is_clamped(self):
        eng = self.Ceiling()
        chat = FakeChat()
        p = _payload()
        p["options"] = {"num_predict": 96000}
        mgr = _mgr(eng)
        polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        root = mgr.roots["direct|e"]
        assert root.ctx_max == 32768
        place = chat.payloads[-1]["placement"]
        # The layer it attaches sits in the same cells: asking the whole
        # ceiling beside it can never be admitted (csl-2026-10-24-1803-00d6).
        layer = root.chain_cells[str(place["pool_id"])]
        assert layer > 0
        assert place["num_ctx"] + layer <= 32768
        assert place["num_ctx"] == polykv._align_down(32768 - layer)

    def test_widen_never_asks_past_the_ceiling_less_the_layer(self):
        eng = self.Ceiling()
        mgr = _mgr(eng)
        p = _payload()
        p["options"] = {"num_predict": 4000}
        pl = mgr.place(FakeChat(), p, role="researcher")
        layer = mgr.roots["direct|e"].chain_cells[str(pl["pool_id"])]
        err = Exception("request (40000 tokens, 1000 of them shareable from "
                        "pool 0) exceeds the available context size "
                        f"({pl['num_ctx']} private tokens)")
        mgr.widen(pl, err, p)
        assert pl["num_ctx"] + layer <= 32768

    def test_make_room_reclaims_once_and_forgets_what_it_closed(self):
        eng = self.Ceiling()
        mgr = _mgr(eng)
        a = mgr.place(FakeChat(), _payload("lane 0"), role="researcher")
        mgr.observe(a, None, role="researcher")
        p = _payload("lane 1")
        p["options"] = {"num_predict": 4000}
        b = mgr.place(FakeChat(), p, role="researcher")
        assert mgr.make_room(b, wait_s=0) is False
        closes = [r[1] for r in eng.requests if r[1].endswith("/close")]
        assert closes == [f"sessions/{a['session_id']}/close"]
        assert a["session_id"] not in mgr.sessions
        assert b["session_id"] in mgr.sessions        # the caller is kept


def test_a_cold_budget_still_opens_an_owner():
    class CeilingOwner(OwnerEngine):
        def handle(self, method, route, params, data):
            if route == "props" and self.refuse is None:
                self.requests.append((method, route, params, data))
                return Reply(200, {"features": self.features, "opencoti": {
                    "polykv": {"pools_enabled": True},
                    "kv": {"session_ctx_max": 65536}}})
            return super().handle(method, route, params, data)
    eng = CeilingOwner(admissible=65536)
    chat = OwnerChat()
    mgr = _assured(eng)
    p = _payload()
    p["options"] = {"num_predict": 96000}           # a cold model's 96k cap
    polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
    assert len(chat.owners()) == 1
    assert chat.owners()[0]["placement"]["num_ctx_min"] <= 65536


class TestPeakCells:
    """Plan F acceptance: a live run reports its peak cells — the pool
    size from /props less the fewest free cells any /kv read saw."""

    class Counting(TestColdBudget.Ceiling):
        def __init__(self, frees, pool_cells=200000):
            super().__init__()
            self.frees, self.pool_cells = list(frees), pool_cells

        def handle(self, method, route, params, data):
            if route == "props" and self.refuse is None:
                kv = {"session_ctx_max": 32768}
                if self.pool_cells:
                    kv["kv_pool_cells"] = self.pool_cells
                return Reply(200, {"features": self.features, "opencoti": {
                    "polykv": {"pools_enabled": True}, "kv": kv}})
            if route == "kv":
                free = self.frees.pop(0) if len(self.frees) > 1 else self.frees[0]
                return Reply(200, {"cells_free": free, "allocations": []})
            return super().handle(method, route, params, data)

    def test_the_settle_report_names_the_peak(self):
        eng = self.Counting([150000, 120000, 180000])
        mgr = _mgr(eng)
        chat = FakeChat()
        for _ in range(2):
            p = _payload()
            p["options"] = {"num_ctx": 8192}     # a window: make_room reads /kv
            polykv.PlacedChat(chat, mgr, role="researcher").chat(p)
        root = mgr.roots["direct|e"]
        assert root.pool_cells == 200000 and root.min_free == 120000
        rep = mgr.release_all()["roots"]["direct|e"]
        assert rep["peak_cells"] == 80000
        assert rep["pool_cells"] == 200000 and rep["min_cells_free"] == 120000

    def test_no_pool_size_is_no_peak_not_a_guess(self):
        eng = self.Counting([150000], pool_cells=0)
        mgr = _mgr(eng)
        polykv.PlacedChat(FakeChat(), mgr, role="researcher").chat(_payload())
        assert mgr.release_all()["roots"]["direct|e"]["peak_cells"] is None


class TestRootDetection:
    """csl-2026-10-25-0703-a11c: solidpc's Ollama is an xOllama, and the
    cloud models behind it were given xOllama roots (affinity) — every
    session close and leak audit at settle answered 404."""

    def _root(self, monkeypatch, *, cloud):
        from claude_hooks import ollama_slots, provider_caps
        from claude_hooks.get_advice.chat_client import ChatClient
        monkeypatch.setattr(provider_caps, "probe_xollama",
                            lambda base, headers=None: {"client_placement_v1"})
        seen = []
        monkeypatch.setattr(ollama_slots, "is_cloud",
                            lambda base, model: seen.append((base, model)) or cloud)
        root = polykv.default_root_for(ChatClient("http://x:11434"), "m:tag")
        assert seen == [("http://x:11434", "m:tag")]
        return root

    def test_a_cloud_model_behind_xollama_has_no_root(self, monkeypatch):
        assert self._root(monkeypatch, cloud=True) is None

    def test_a_local_model_behind_xollama_keeps_its_root(self, monkeypatch):
        root = self._root(monkeypatch, cloud=False)
        assert root is not None and root.kind == "xollama"
        assert root.key == "xollama|http://x:11434|m:tag"


class TestLaneOutputCap:
    """ch-21: each placed call asks its prompt past the layer plus its
    whole output allowance, so on a small engine two researcher lanes
    beside the layer could not both be admitted. Under pooled / assured
    a lane's allowance is its share of the pool beside the layer."""

    HEAD = [{"role": "system", "content": "GROUNDING " + "g" * 16000},
            {"role": "system", "content": "You are the researcher."}]

    class Small(PoolEngine):
        """A pool of ``pool_cells``; a layer is one cell per four chars."""

        def __init__(self, pool_cells):
            super().__init__()
            self.pool_cells = pool_cells

        def handle(self, method, route, params, data):
            if route == "props" and self.refuse is None:
                self.requests.append((method, route, params, data))
                return Reply(200, {"features": self.features, "opencoti": {
                    "polykv": {"pools_enabled": self.pools_enabled},
                    "kv": {"session_ctx_max": 32768,
                           "kv_pool_cells": self.pool_cells}}})
            out = super().handle(method, route, params, data)
            if route == "polykv/pools" and method == "POST" and out.status == 200:
                pid = out.body["pool_id"]
                self.pools[pid]["own_len"] = len(data.get("prompt") or "") // 4
                out = Reply(200, dict(self.pools[pid]))
            return out

    class Admitting(FakeChat):
        """Admits a call while the layer plus every live booking fits the
        pool; bookings are kept (both lanes are mid-run)."""

        def __init__(self, engine):
            super().__init__()
            self.engine, self.booked, self.refused = engine, {}, []

        def context_length(self, model):
            return 32768

        def chat(self, payload):
            from claude_hooks.get_advice.chat_client import ChatFault
            p = payload.get("placement") or {}
            layer = sum(int(v.get("own_len") or 0) for v in self.engine.pools.values())
            ask = int(p.get("num_ctx") or 32768)
            others = sum(v for k, v in self.booked.items() if k != p.get("session_id"))
            if layer + others + ask > self.engine.pool_cells:
                self.refused.append(p.get("session_id"))
                raise ChatFault("ollama chat HTTP 429: admission rejected",
                                fault_class="transport", status=429)
            self.booked[p.get("session_id")] = ask
            return super().chat(payload)

        chat_streamed = chat

    def _lane(self, n):
        return self.HEAD + [{"role": "user", "content": f"QUESTION\nlane {n}: read x.py"}]

    def _run_lane(self, placed, n):
        from consultants.engine import council
        msgs, plan = council._plan_and_compact(placed, "q", self._lane(n),
                                               role="researcher")
        placed.chat({"model": "q", "messages": msgs,
                     "options": {"num_predict": plan.output_tokens}})
        return msgs, plan

    def _setup(self, pool_cells, monkeypatch, *, cap=True):
        monkeypatch.setattr(polykv, "WAIT_FOR_ROOM_S", 0.0)
        eng = self.Small(pool_cells)
        chat = self.Admitting(eng)
        mgr = _mgr(eng)
        if not cap:
            monkeypatch.setattr(mgr, "output_cap", lambda *a, **k: None)
        return eng, chat, polykv.PlacedChat(chat, mgr, role="researcher"), mgr

    def test_two_lanes_fit_with_the_cap(self, monkeypatch):
        eng, chat, placed, mgr = self._setup(32768, monkeypatch)
        _, plan = self._run_lane(placed, 1)
        self._run_lane(placed, 2)
        assert chat.refused == [] and len(chat.booked) == 2
        lanes = polykv._lanes_of("researcher")
        assert lanes == 2
        assert plan.role_cap == mgr._out_caps[("direct|e", "q", "researcher")]
        assert plan.role_cap < 32768 * 0.75 // lanes

    def test_without_the_cap_the_second_lane_is_refused(self, monkeypatch):
        from claude_hooks.get_advice.chat_client import ChatFault
        eng, chat, placed, mgr = self._setup(32768, monkeypatch, cap=False)
        self._run_lane(placed, 1)
        with pytest.raises(ChatFault):
            self._run_lane(placed, 2)
        assert len(chat.refused) >= 1

    def test_a_large_pool_changes_nothing(self, monkeypatch):
        from consultants.engine import council
        eng, chat, placed, mgr = self._setup(589824, monkeypatch)
        a = council._plan_and_compact(placed, "q", self._lane(1), role="researcher")
        b = council._plan_and_compact(self.Admitting(eng), "q", self._lane(1),
                                      role="researcher")
        assert a[0] == b[0]
        assert a[1].output_tokens == b[1].output_tokens == 24576

    def test_the_cap_is_stated_once_and_stays(self, monkeypatch):
        from consultants.engine import budget, council
        eng, chat, placed, mgr = self._setup(32768, monkeypatch)
        msgs, plan = self._run_lane(placed, 1)
        cap = plan.role_cap
        stated = [m["content"] for m in msgs
                  if budget.OUTPUT_BUDGET_HEADING in str(m.get("content"))]
        assert len(stated) == 1
        assert f"{cap:,}" in stated[0] or str(cap) in stated[0]
        # The layer now exists and is sized: the figure must not move.
        assert mgr.roots["direct|e"].sizes
        fresh, plan2 = council._plan_and_compact(placed, "q", self._lane(3),
                                                 role="researcher")
        assert plan2.role_cap == cap
        longer = msgs + [{"role": "assistant", "content": "a" * 3000},
                         {"role": "user", "content": "b" * 3000}]
        again, plan3 = council._plan_and_compact(placed, "q", longer,
                                                 role="researcher")
        assert plan3.role_cap == cap
        assert [m for m in again if m.get("role") == "system"] == \
            [m for m in msgs if m.get("role") == "system"]

    def test_a_single_role_gets_the_whole_share(self):
        assert polykv._lanes_of("planner") == 1
        assert polykv._lanes_of("researcher:cross_exam") == polykv._lanes_of("researcher")

    def test_off_and_affinity_have_no_cap(self):
        eng = self.Small(32768)
        assert _mgr(eng, mode="off").output_cap(FakeChat(), "q", self._lane(1),
                                                role="researcher") is None
        eng.pools_enabled = False
        assert _mgr(eng).output_cap(FakeChat(), "q", self._lane(1),
                                    role="researcher") is None
