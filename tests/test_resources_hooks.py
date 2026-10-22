"""Resource claims in the hooks and on the MCP servers."""
from __future__ import annotations

import os

import pytest

from claude_hooks.resources import api, hook
from claude_hooks.resources.model import parse_item
from claude_hooks.resources.store import Owner
from claude_hooks.resources.tools import ResourceTools, tool_catalog


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_HOST", "solidpc")
    monkeypatch.setenv("CLAUDE_HOOKS_RESOURCES_STATE_DIR", str(tmp_path / "state"))
    api.reset_store()
    s = api.store({})
    s._alive = lambda pid, started: True
    return s


def claim(st, session, *specs, reason="", pid=None):
    return st.request([parse_item(x) for x in specs],
                      Owner(session_id=session, alias=session, host="solidpc",
                            pid=pid, pid_started=1.0 if pid else None),
                      reason=reason)


def bash(cmd, session="me"):
    return {"tool_name": "Bash", "tool_input": {"command": cmd},
            "session_id": session}


class TestReferenced:
    @pytest.mark.parametrize("cmd,want", [
        ("ls -la", set()),
        ("ssh bs2 nvidia-smi", {"bs2"}),
        ("ssh -p 22 -o BatchMode=yes root@bs2 'ls'", {"bs2"}),
        ("scp a.gguf eleven2go:/models/", {"eleven2go"}),
        ("CUDA_VISIBLE_DEVICES=0,1 ./llama-bench", {"solidpc/gpu0",
                                                     "solidpc/gpu1"}),
    ])
    def test_names(self, cmd, want):
        assert hook.referenced(cmd, "solidpc") == want


class TestPreToolNote:
    def test_quiet_host_is_said_once_per_claim(self, st):
        c = claim(st, "other", "solidpc/gpu0:quiet", reason="perf bench")
        note = hook.pre_tool_note(event=bash("make -j32"), config={})
        assert note and "QUIET" in note and "perf bench" in note
        assert "claude-hooks-res wait solidpc/gpu0" in note
        assert hook.pre_tool_note(event=bash("make -j32"), config={}) is None
        assert c.granted

    def test_other_gpu_does_not_flag_every_local_command(self, st):
        claim(st, "other", "solidpc/gpu0:exclusive")
        assert hook.pre_tool_note(event=bash("pytest -q"), config={}) is None
        assert hook.pre_tool_note(
            event=bash("CUDA_VISIBLE_DEVICES=0 ./run"), config={})

    def test_remote_gpu_flags_ssh_to_that_host(self, st):
        claim(st, "other", "bs2/gpu1", reason="owner's run")
        note = hook.pre_tool_note(event=bash("ssh bs2 ./gate.sh"), config={})
        assert note and "owner's run" in note

    def test_own_claims_and_res_commands_are_silent(self, st):
        claim(st, "me", "solidpc:quiet")
        assert hook.pre_tool_note(event=bash("make"), config={}) is None
        claim(st, "other", "bs2:quiet")
        assert hook.pre_tool_note(
            event=bash("claude-hooks-res wait bs2"), config={}) is None

    def test_off_switch(self, st):
        claim(st, "other", "solidpc:quiet")
        cfg = {"hooks": {"resources": {"enabled": False}}}
        assert hook.pre_tool_note(event=bash("make"), config=cfg) is None

    def test_merged_into_the_pre_tool_use_answer(self, st):
        from claude_hooks.hooks import pre_tool_use
        claim(st, "other", "solidpc:quiet", reason="bench")
        out = pre_tool_use.handle(event=bash("make"), config={}, providers=[])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "bench" in ctx and out["hookSpecificOutput"].get(
            "permissionDecision") is None


class TestSessionHooks:
    def test_session_block_lists_mine_and_others(self, st):
        claim(st, "me", "solidpc/gpu0", reason="kld")
        claim(st, "other", "bs2/gpu0", reason="gate")
        claim(st, "other2", "solidpc/gpu0", reason="later")
        text = hook.session_block(event={"session_id": "me"}, config={})
        assert text.startswith("Resources:")
        assert "yours (held): solidpc/gpu0" in text
        assert "held: bs2/gpu0" in text and "queued #1: solidpc/gpu0" in text

    def test_session_block_empty_store_is_the_howto(self, st):
        assert hook.session_block(event={"session_id": "me"},
                                  config={}) == hook.HOWTO

    def test_session_end_keeps_run_jobs(self, st, monkeypatch):
        a = claim(st, "me", "solidpc/gpu0")                   # session claim
        b = claim(st, "me", "bs2/gpu0", pid=os.getpid())      # a run job
        monkeypatch.setattr(hook, "_is_claude_pid",
                            lambda pid: pid != os.getpid())
        assert hook.session_end(event={"session_id": "me"}, config={}) == 1
        assert st.get(a.claim.id) is None
        assert st.get(b.claim.id) is not None

    def test_touch_is_throttled(self, st):
        claim(st, "me", "solidpc/gpu0")
        assert hook.touch(event={}, config={}, now=1000.0) == 1
        assert hook.touch(event={}, config={}, now=1030.0) == 0
        assert hook.touch(event={}, config={}, now=1061.0) == 1

    def test_hooks_survive_an_unreachable_store(self, st, monkeypatch):
        def boom():
            raise OSError("down")
        monkeypatch.setattr(hook, "_store", boom)
        assert hook.pre_tool_note(event=bash("make"), config={}) is None
        assert hook.touch(event={}, config={}, now=5e9) == 0
        assert hook.session_end(event={"session_id": "x"}, config={}) == 0
        assert "unreachable" in hook.session_block(event={}, config={})

    def test_stop_skips_reindex_while_another_session_keeps_quiet(self, st):
        from claude_hooks.hooks import stop
        assert stop._host_kept_quiet({"session_id": "me"}, {}) is False
        claim(st, "other", "solidpc:quiet")
        assert stop._host_kept_quiet({"session_id": "me"}, {}) is True
        assert stop._host_kept_quiet({"session_id": "other"}, {}) is False


class TestMarkerRoute:
    def test_resources_part_parses(self):
        from claude_hooks.hook_parts import parse_marker
        assert "resources" in parse_marker("keep: memory, resources")

    def test_pre_tool_use_note_through_the_marker_route(self, st):
        from claude_hooks import hook_parts
        claim(st, "other", "solidpc:quiet", reason="bench")
        out = hook_parts.run("PreToolUse", event=bash("make"), config={},
                             providers=[], keep=frozenset({"resources"}))
        assert "bench" in out["hookSpecificOutput"]["additionalContext"]
        none = hook_parts.run("PreToolUse", event=bash("make"), config={},
                              providers=[], keep=frozenset({"guards"}))
        assert none is None

    def test_session_end_through_the_marker_route(self, st, monkeypatch):
        from claude_hooks import hook_parts
        a = claim(st, "me", "solidpc/gpu0")
        hook_parts.run("SessionEnd", event={"session_id": "me"}, config={},
                       providers=[], keep=frozenset({"resources"}))
        assert st.get(a.claim.id) is None


class TestTools:
    def tools(self, st, session="me"):
        return ResourceTools(store_fn=lambda: st, owner_fn=lambda: Owner(
            session_id=session, alias=session, host="solidpc"))

    def test_catalog_names(self):
        assert {t["name"] for t in tool_catalog()} == {
            "resource-status", "resource-acquire", "resource-release",
            "resource-check", "resource-define", "resource-events"}

    def test_acquire_busy_then_queue_then_release(self, st):
        mine, theirs = self.tools(st), self.tools(st, "other")
        ok = mine.call("resource-acquire", {"resources": ["solidpc/gpu0"],
                                            "reason": "kld"})
        assert ok.startswith("GRANTED")
        busy = theirs.call("resource-acquire", {"resources": ["solidpc/gpu0"],
                                                "reason": "x"})
        assert busy.startswith("BUSY") and "kld" in busy
        assert "nobody waiting" not in mine.call("resource-status", {})
        q = theirs.call("resource-acquire", {"resources": ["solidpc/gpu0"],
                                             "reason": "x", "queue": True})
        assert q.startswith("QUEUED") and "claude-hooks-res wait --claim" in q
        rel = mine.call("resource-release", {"all": True})
        assert rel.startswith("RELEASED")
        assert "HELD   solidpc/gpu0" in mine.call("resource-status", {})

    def test_release_refuses_others(self, st):
        mine, theirs = self.tools(st), self.tools(st, "other")
        cid = mine.call("resource-acquire", {"resources": ["x"],
                                             "reason": "r"}).split()[1][:-1]
        assert theirs.call("resource-release",
                           {"claims": [cid]}).startswith("REFUSED")

    def test_bad_spec_and_outage_read_as_such(self, st):
        t = self.tools(st)
        assert t.call("resource-acquire", {"resources": ["Bad Name"],
                                           "reason": ""}).startswith("Not done")

        def boom():
            raise OSError("db down")
        dead = ResourceTools(store_fn=boom, owner_fn=lambda: Owner())
        assert "unreachable" in dead.call("resource-status", {})

    def test_both_servers_offer_and_dispatch_them(self):
        import inspect
        from claude_hooks.pgvector_mcp import server as pgs
        from claude_hooks.sqlite_vec_mcp import server as svs
        for mod in (pgs, svs):
            src = inspect.getsource(mod)
            assert "resource_catalog()" in src
            assert 'name.startswith("resource-")' in src


def test_daemon_tick_renews(st):
    import threading
    c = claim(st, "me", "solidpc/gpu0")
    t = hook.ResourceMaintenanceThread(lambda: {}, stop_event=threading.Event())
    before = st.get(c.claim.id).claim.renewed_at
    import time
    time.sleep(0.01)
    t.tick()
    assert st.get(c.claim.id).claim.renewed_at > before
