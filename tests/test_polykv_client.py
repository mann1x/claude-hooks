"""The PolyKV control-plane client (plan F1), against a fake engine.

The cases opencoti_langgraph's ``tests/test_client.py`` covers, ported, plus
the surface it lacks: owners / shared / unowned pools, ``/kv``, session
close with ``found``, resize, ``/apply-template``, ``boot_id``, and the
xOllama ``/api/engine`` envelope. Every engine case runs on both
transports, so the envelope cannot drift from the direct path.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from claude_hooks.polykv.client import (
    Capacity,
    OpencotiError,
    PolykvClient,
    PoolSaturatedError,
    Reply,
    create_body,
    observe,
    pool_ref,
    wire_id,
)


class FakeEngine:
    """Enough of the engine's routes to drive the client."""

    def __init__(self):
        self.pools: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {"owner-1": {"window": 65536}}
        self.next_id = 0
        self.requests: list[tuple[str, str, dict, object]] = []
        self.boot = "boot-A"
        self.refuse: Reply | None = None

    def handle(self, method: str, route: str, params: dict, data) -> Reply:
        self.requests.append((method, route, params, data))
        if self.refuse is not None:
            return self.refuse
        parts = route.split("/")
        if route == "health":
            return Reply(200, {"status": "ok", "boot_id": self.boot})
        if route == "props":
            return Reply(200, {"features": ["kv_status_v1", "pool_unowned_v1"]})
        if route == "kv":
            return Reply(200, {"session_ctx_max": 131072, "allocations": [
                {"session_id": s, **v} for s, v in self.sessions.items()]})
        if route == "polykv/pools" and method == "POST":
            pid = self.next_id
            self.next_id += 1
            self.pools[pid] = {"pool_id": pid, "parent": None,
                               "pinned": bool(data.get("pin")),
                               "owner": data.get("session_id"), "body": data}
            return Reply(200, dict(self.pools[pid]))
        if route == "polykv/pools" and method == "GET":
            return Reply(200, {"pools": list(self.pools.values())})
        if parts[:2] == ["polykv", "pools"] and len(parts) >= 3:
            pid = int(parts[2])
            if pid not in self.pools:
                return Reply(404, {"error": {"message": "unknown pool"}})
            action = parts[3] if len(parts) > 3 else None
            if method == "DELETE" or action == "release":
                if any(p["parent"] == pid for p in self.pools.values()):
                    return Reply(409, {"error": {"message": "has children"}})
                del self.pools[pid]
                return Reply(200, {"released": pid})
            if action in ("pin", "unpin"):
                self.pools[pid]["pinned"] = action == "pin"
                return Reply(200, dict(self.pools[pid]))
            if action == "fork":
                cid = self.next_id
                self.next_id += 1
                self.pools[cid] = {"pool_id": cid, "parent": pid,
                                   "branch_pos": data.get("branch_pos"),
                                   "pinned": bool(data.get("pin")), "body": data}
                return Reply(200, dict(self.pools[cid]))
            if action == "capacity":
                exp = int(params.get("expected_tokens") or 0)
                return Reply(200, {"pool_id": pid, "can_admit": exp < 100000,
                                   "reason": "" if exp < 100000 else
                                   "context headroom exhausted",
                                   "slots_live": 4, "slots_free": 3,
                                   "folded": params.get("fold") == "1",
                                   "pressure": 0.4, "window_cells": 65536,
                                   "pressure_scope": "session",
                                   "owner": "owner-1"})
            if action == "admission":
                return Reply(200, {"pool_id": pid, "admission": data})
            return Reply(200, dict(self.pools[pid]))
        if parts[0] == "sessions" or route.startswith("kv/sessions"):
            sid = data.get("session_id") if parts[-1] in ("close", "resize") \
                and len(parts) == 2 else parts[-2]
            if route.endswith("close"):
                found = self.sessions.pop(sid, None) is not None
                if found:
                    for pid in [p for p, v in self.pools.items()
                                if v.get("owner") == sid]:
                        del self.pools[pid]
                return Reply(200, {"session_id": sid, "found": found,
                                   "released": found})
            if route.endswith("resize"):
                if sid not in self.sessions:
                    return Reply(404, {"error": {"message": "session_not_found"}})
                self.sessions[sid]["window"] = data["num_ctx"]
                return Reply(200, {"ok": True, "window_new": data["num_ctx"]})
        if route == "apply-template":
            text = "".join(f"<{m['role']}>{m['content']}" for m in data["messages"])
            return Reply(200, {"prompt": text})
        if route == "tokenize":
            return Reply(200, {"tokens": [ord(c) for c in data["content"]]})
        if route == "metrics":
            return Reply(200, None, "# HELP x\nopencoti_polykv_active_sessions 3\n"
                                    "other_metric 9\n")
        return Reply(404, {"error": {"message": f"no route {route}"}})


def _split(url: str) -> tuple[str, dict]:
    u = urllib.parse.urlsplit(url)
    return u.path, dict(urllib.parse.parse_qsl(u.query))


def direct_fetch(engine: FakeEngine):
    def fetch(method, url, *, data=None, headers=None, timeout=None):
        path, params = _split(url)
        return engine.handle(method, path.lstrip("/"), params, data)
    return fetch


def xollama_fetch(engine: FakeEngine, model: str = "qwen3.6:27b"):
    def fetch(method, url, *, data=None, headers=None, timeout=None):
        path, params = _split(url)
        assert path == "/api/engine", path
        if params.pop("model") != model:
            return Reply(404, {"error": "model not loaded"})
        route = params.pop("endpoint")
        inner = engine.handle(method, route, params, data)
        env = {"model": model, "engine": "opencoti", "method": method,
               "endpoint": route, "status": inner.status}
        if inner.body is not None:
            env["body"] = inner.body
        else:
            env["text"] = inner.text
        return Reply(200, env, json.dumps(env), dict(inner.headers))
    return fetch


@pytest.fixture(params=["direct", "xollama"])
def engine_client(request):
    eng = FakeEngine()
    if request.param == "direct":
        c = PolykvClient.direct("http://e:8240/v1", fetch=direct_fetch(eng))
    else:
        c = PolykvClient.via_xollama("http://x:22434", "qwen3.6:27b",
                                     fetch=xollama_fetch(eng))
    return eng, c


# --------------------------------------------------------- upstream cases

def test_create_and_get_pool_zero(engine_client):
    eng, c = engine_client
    p = c.create_pool(prompt="system prompt here", pin=True)
    assert pool_ref(p) == "0"                      # pool 0 is a pool
    assert c.pool("0")["pool_id"] == 0
    assert c.pools()["pools"][0]["pool_id"] == 0
    assert eng.requests[0][3] == {"prompt": "system prompt here", "pin": True}


def test_a_pool_needs_exactly_one_source():
    with pytest.raises(ValueError):
        create_body()
    with pytest.raises(ValueError):
        create_body(prompt="a", from_session="s")


def test_fork_extend_and_cow_branch(engine_client):
    eng, c = engine_client
    root = pool_ref(c.create_pool(prompt="root"))
    child = c.fork_pool(root, prompt="root suffix")
    assert str(child["parent"]) == root
    c.fork_pool(root, branch_pos=2, prompt="ro alt")
    assert eng.requests[-1][3] == {"branch_pos": 2, "prompt": "ro alt"}
    with pytest.raises(ValueError):
        c.fork_pool(root, branch_pos=-1, prompt="x")


def test_release_is_leaves_first_and_pin_round_trips(engine_client):
    eng, c = engine_client
    root = pool_ref(c.create_pool(prompt="root"))
    leaf = pool_ref(c.fork_pool(root, prompt="root+"))
    c.pin_pool(root)
    assert eng.pools[0]["pinned"] is True
    c.pin_pool(root, pin=False)
    assert eng.pools[0]["pinned"] is False
    with pytest.raises(OpencotiError) as e:
        c.release_pool(root)
    assert e.value.status == 409                  # the engine's, not the envelope's
    c.release_pool(leaf, delete=True)
    assert eng.requests[-1][0] == "DELETE"
    c.release_pool(root)
    assert eng.pools == {}


def test_admission_validation():
    c = PolykvClient.direct("http://e", fetch=direct_fetch(FakeEngine()))
    with pytest.raises(ValueError):
        c.set_admission("0", mode="bogus")
    with pytest.raises(ValueError):
        c.set_admission("0")
    with pytest.raises(ValueError):
        c.set_admission("0", prefill_max_slots=-1)


def test_capacity_parses_window_view_and_fold(engine_client):
    eng, c = engine_client
    c.create_pool(prompt="root", session_id="owner-1")
    cap = c.capacity("0")
    assert isinstance(cap, Capacity) and cap.can_admit
    assert cap.pool_id == "0" and cap.slots_free == 3 and not cap.folded
    assert (cap.pressure, cap.window_cells, cap.pressure_scope, cap.owner) == \
        (0.4, 65536, "session", "owner-1")
    assert "fold" not in eng.requests[-1][2]      # a plain read never folds
    cap2 = c.capacity("0", expected_tokens=999999, fold=True)
    assert not cap2.can_admit and cap2.reason == "context headroom exhausted"
    assert cap2.folded
    assert Capacity.from_json({"pool_id": 0, "can_admit": True}).slots_live is None


def test_429_maps_to_saturation_with_window_fields(engine_client):
    eng, c = engine_client
    eng.refuse = Reply(429, {"error": {
        "message": "session allocation full (worker of owner-1)",
        "largest_admissible": 4096, "pressure": {"refused_60s": 2}}},
        headers={"retry-after": "2"})
    with pytest.raises(PoolSaturatedError) as e:
        c.create_pool(prompt="x")
    err = e.value
    assert err.worker_full and err.retry_after == 2.0
    assert err.largest_admissible == 4096 and err.pressure == {"refused_60s": 2}


def test_tokenize_says_which_tokenizer(engine_client):
    eng, c = engine_client
    assert c.tokenize("ab") == [97, 98]
    assert eng.requests[-1][3]["add_special"] is False
    c.tokenize("ab", add_special=True)
    assert eng.requests[-1][3]["add_special"] is True


def test_from_session_snapshot_and_ephemeral_passthrough(engine_client):
    eng, c = engine_client
    c.create_pool(from_session="owner-1", ephemeral=False, pin=True)
    assert eng.requests[-1][3] == {"from_session": "owner-1",
                                   "ephemeral": False, "pin": True}
    c.fork_pool("0", from_session="owner-1", branch_pos=10, ephemeral=True)
    assert eng.requests[-1][3] == {"branch_pos": 10, "from_session": "owner-1",
                                   "ephemeral": True}


# -------------------------------------------------------------- additions

def test_owner_shared_and_unowned_pools():
    assert create_body(prompt="p", session_id="o") == {"prompt": "p", "session_id": "o"}
    assert create_body(prompt="p", shared=True, ephemeral=True) == {
        "prompt": "p", "ephemeral": True, "shared": True}
    assert create_body(from_session="s", unowned=True) == {
        "from_session": "s", "unowned": True}
    with pytest.raises(ValueError):
        create_body(prompt="p", shared=True, session_id="o")
    with pytest.raises(ValueError):
        create_body(prompt="p", unowned=True, session_id="o")


def test_close_reads_found_and_takes_owned_pools(engine_client):
    eng, c = engine_client
    c.create_pool(prompt="root", session_id="owner-1")
    got = c.close_session("owner-1")
    assert got.found and eng.pools == {}
    assert eng.requests[-1][1] == "sessions/owner-1/close"
    again = c.close_session("owner-1")
    assert not again.found                        # nothing was released


def test_close_uses_the_body_form_for_an_unroutable_id(engine_client):
    eng, c = engine_client
    eng.sessions["run/a b"] = {"window": 1}
    got = c.close_session("run/a b", keep_running=True)
    assert got.found
    method, route, _, body = eng.requests[-1]
    assert route == "sessions/close"
    assert body == {"session_id": "run/a b", "keep_running": True}


def test_resize_and_kv(engine_client):
    eng, c = engine_client
    assert c.resize_session("owner-1", 98304)["window_new"] == 98304
    assert eng.requests[-1][1] == "kv/sessions/owner-1/resize"
    assert c.kv()["allocations"][0]["window"] == 98304
    c.kv(live=True)
    assert eng.requests[-1][2] == {"live": "1"}
    with pytest.raises(OpencotiError) as e:
        c.resize_session("gone", 1)
    assert e.value.status == 404


def test_apply_template_continue_pool_and_boot_id(engine_client):
    eng, c = engine_client
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    assert c.apply_template(msgs) == "<system>S<user>Q"
    c.apply_template(msgs, pool_id="0", continue_pool=True)
    assert eng.requests[-1][3]["pool_id"] == 0
    assert eng.requests[-1][3]["continue_pool"] is True
    with pytest.raises(ValueError):
        c.apply_template(msgs, continue_pool=True)
    assert c.boot_id() == "boot-A"
    assert "kv_status_v1" in c.features()


def test_metrics_keeps_the_polykv_gauges(engine_client):
    _, c = engine_client
    assert c.metrics() == {"opencoti_polykv_active_sessions": 3.0}


def test_xollama_refusal_outside_the_envelope_is_its_own():
    eng = FakeEngine()
    c = PolykvClient.via_xollama("http://x", "other:model",
                                 fetch=xollama_fetch(eng, model="qwen3.6:27b"))
    with pytest.raises(OpencotiError) as e:
        c.kv()
    assert e.value.status == 404 and eng.requests == []


def test_direct_root_drops_v1():
    seen = []

    def fetch(method, url, **kw):
        seen.append(url)
        return Reply(200, {"boot_id": "b"})
    PolykvClient.direct("http://e:8240/v1/", fetch=fetch).boot_id()
    assert seen == ["http://e:8240/health"]


# ------------------------------------------------------------ observation

def test_wire_id_keeps_every_route_usable():
    assert wire_id("sid/researcher lane 2") == "sid~researcher~lane~2"
    assert wire_id(".hidden") == "~hidden"
    assert len(wire_id("x" * 400)) == 256
    assert wire_id("a~b.c-d_e") == "a~b.c-d_e"


def test_observe_reads_the_grant_and_the_share():
    obs = observe({"X-Context-Window": "65536", "X-OpenCoti-Boot-Id": "b1",
                   "X-Sessions-Remaining": "3"},
                  {"opencoti": {"pool_id": 0, "n_pool_shared": 5917,
                                "pool_match": 5917, "pool_len": 5917},
                   "timings": {"cache_n": 5917, "prompt_n": 33}}, pool_id="0")
    assert (obs.context_window, obs.boot_id, obs.sessions_remaining) == (65536, "b1", 3)
    assert obs.pool_id == "0" and obs.n_pool_shared == 5917
    assert (obs.cache_n, obs.prompt_n) == (5917, 33)
    assert not obs.degraded and obs.warnings == []


def test_observe_warns_on_a_silent_miss(caplog):
    obs = observe({}, {"opencoti": {"pool_id": 0, "n_pool_shared": 0,
                                    "pool_match": 4, "pool_len": 6000}},
                  pool_id="0")
    assert obs.degraded and "n_pool_shared 0" in obs.warnings[0]
    unk = observe({}, {"opencoti": {"pool_id": 7, "pool_unknown": True,
                                    "pool_match": -1}}, pool_id=7)
    assert not unk.degraded and "unknown" in unk.warnings[0]
    partial = observe({}, {"opencoti": {"pool_id": 1, "n_pool_shared": 40,
                                        "pool_match": 40, "pool_len": 90}},
                      pool_id=1)
    assert "matches 40 of its 90" in partial.warnings[0]
    quiet = observe({}, {"choices": []})
    assert quiet.pool_id is None and quiet.warnings == []
    missing = observe({}, {"choices": []}, pool_id="3")
    assert "no opencoti block" in missing.warnings[0]


def test_without_a_block_the_cached_count_is_the_share():
    """xOllama's native /api/chat carries no opencoti block, only
    prompt_eval_cached_count (eleven2go, 2026-10-24: 2840 on both workers
    of a 2840-token tree)."""
    shared = observe({}, {"timings": {"cache_n": 2840}}, pool_id="3")
    assert not shared.degraded and shared.warnings == []
    missed = observe({}, {"timings": {"cache_n": 0}}, pool_id="3")
    assert missed.degraded and "nothing cached" in missed.warnings[0]
    blind = observe({}, {}, pool_id="3")
    assert not blind.degraded and "no opencoti block" in blind.warnings[0]


class TestRenderChat:
    """Layers are cut from what the inference path renders: xOllama trims
    each turn's content, so the engine's apply-template diverged at the
    first turn ending in a newline (1,050 of 16,557 tokens shared)."""

    MSGS = [{"role": "system", "content": "S\n"}, {"role": "user", "content": "U"}]

    def test_direct_is_the_engines_apply_template(self):
        eng = FakeEngine()
        c = PolykvClient.direct("http://e:8240", fetch=direct_fetch(eng))
        orig = eng.handle
        eng.handle = lambda m, r, p, d: (Reply(200, {"prompt": "ENGINE"})
                                         if r == "apply-template" else orig(m, r, p, d))
        assert c.render_chat(self.MSGS) == "ENGINE"

    def test_xollama_is_its_own_render(self):
        seen = []

        def fetch(method, url, *, data=None, headers=None, timeout=None):
            seen.append((method, url, data))
            return Reply(200, {"_debug_info": {"rendered_template": "XOLLAMA"}})
        c = PolykvClient.via_xollama("http://x:22434", "m", fetch=fetch)
        assert c.render_chat(self.MSGS, tools=[{"t": 1}]) == "XOLLAMA"
        method, url, body = seen[0]
        assert (method, url) == ("POST", "http://x:22434/api/chat")
        assert body["_debug_render_only"] is True and body["model"] == "m"
        assert body["messages"] == self.MSGS and body["tools"] == [{"t": 1}]

    def test_xollama_without_a_render_is_an_error(self):
        c = PolykvClient.via_xollama(
            "http://x:22434", "m",
            fetch=lambda *a, **k: Reply(200, {"message": {"content": "hi"}}))
        with pytest.raises(OpencotiError):
            c.render_chat(self.MSGS)
