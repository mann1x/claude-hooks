"""A session change inside one Claude Code process (``/clear``, resume) must
never make the session a stranger to its own claims (backup_models #2169).

The MCP server keeps the ``CLAUDE_CODE_SESSION_ID`` it was spawned with;
``~/.claude/sessions/<pid>.json`` holds the live one. Claims follow the
Claude Code process, so ownership follows it too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_hooks._proc import process_start_time
from claude_hooks.mailbox import identity
from claude_hooks.resources import api, cli, hook
from claude_hooks.resources.model import parse_item
from claude_hooks.resources.store import Owner
from claude_hooks.resources.tools import ResourceTools

PID = os.getpid()
STARTED = process_start_time(PID)

linux_only = pytest.mark.skipif(not Path(f"/proc/{PID}/stat").exists(),
                                reason="needs /proc")


def _ticks() -> str:
    return Path(f"/proc/{PID}/stat").read_text().rsplit(")", 1)[1].split()[19]


def write_session_file(sid: str, **over) -> Path:
    rec = {"pid": PID, "sessionId": sid, "procStart": _ticks(),
           "startedAt": int(STARTED * 1000) + 1400}
    rec.update(over)
    rec = {k: v for k, v in rec.items() if v is not None}
    d = Path(os.environ[identity.SESSIONS_DIR_ENV])
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{PID}.json"
    p.write_text(json.dumps(rec))
    return p


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_HOST", "solidpc")
    monkeypatch.setenv("CLAUDE_HOOKS_RESOURCES_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOOKS_CLIENT_PID", str(PID))
    api.reset_store()
    s = api.store({})
    s._alive = lambda pid, started: True
    yield s
    api.reset_store()


def claim_as(st, session, *specs, pid=PID, started=STARTED):
    return st.request([parse_item(x) for x in specs],
                      Owner(session_id=session, alias="bm", host="solidpc",
                            pid=pid, pid_started=started), ttl_s=3600).claim


@linux_only
class TestLiveSessionId:
    CLIENT = (PID, STARTED)

    def test_the_file_names_the_live_session(self):
        write_session_file("live-b")
        assert identity.live_session_id(self.CLIENT) == "live-b"

    def test_another_process_file_under_a_reused_pid_is_ignored(self):
        write_session_file("stale", procStart=str(int(_ticks()) - 500))
        assert identity.live_session_id(self.CLIENT) is None

    def test_started_at_is_the_check_without_proc_start(self):
        write_session_file("live-b", procStart=None)
        assert identity.live_session_id(self.CLIENT) == "live-b"
        write_session_file("stale", procStart=None,
                           startedAt=int((STARTED - 3600) * 1000))
        assert identity.live_session_id(self.CLIENT) is None

    def test_a_file_for_another_pid_is_ignored(self):
        write_session_file("x", pid=PID + 1)
        assert identity.live_session_id(self.CLIENT) is None

    def test_no_file_no_client(self):
        assert identity.live_session_id(self.CLIENT) is None
        assert identity.live_session_id([]) is None

    def test_session_id_here_prefers_the_file_to_the_frozen_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spawned-a")
        assert api.session_id_here(self.CLIENT) == "spawned-a"
        write_session_file("live-b")
        assert api.session_id_here(self.CLIENT) == "live-b"


@linux_only
class TestMcpAfterASessionChange:
    """The mail's test: MCP spawned as A, the session is now B."""

    def test_extend_and_release_a_claim_made_as_b_from_the_cli(self, st, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spawned-a")
        write_session_file("live-b")
        c = claim_as(st, "live-b", "bs2/gpu1")
        mcp = ResourceTools(store_fn=lambda: st)        # real owner_here
        out = mcp.call("resource-extend", {"claim": c.id, "by": "12h"})
        assert out.startswith(f"EXTENDED {c.id}"), out
        out = mcp.call("resource-release", {"claim": c.id})
        assert out.startswith(f"RELEASED {c.id}"), out

    def test_a_claim_under_an_earlier_id_of_the_process_is_still_mine(self, st, monkeypatch):
        # No session file (an older Claude Code): the env id is stale, the
        # claim carries the new one, the process is the same.
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spawned-a")
        c = claim_as(st, "live-b", "bs2/gpu0")
        mcp = ResourceTools(store_fn=lambda: st)
        out = mcp.call("resource-extend", {"claim": c.id, "by": "1h"})
        assert out.startswith(f"EXTENDED {c.id}"), out
        assert st.get(c.id).claim.session_id == "spawned-a"   # re-stamped

    def test_another_process_claim_is_still_refused(self, st, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spawned-a")
        c = claim_as(st, "someone", "bs2/gpu0", pid=PID + 1, started=1.0)
        mcp = ResourceTools(store_fn=lambda: st)
        assert mcp.call("resource-extend", {"claim": c.id, "by": "1h"}
                        ).startswith(f"REFUSED {c.id}")
        assert mcp.call("resource-release", {"claim": c.id}
                        ).startswith(f"REFUSED {c.id}")

    def test_release_all_takes_the_process_claims_under_any_id(self, st, monkeypatch):
        write_session_file("live-b")
        mine = claim_as(st, "old-a", "bs2/gpu0")
        theirs = claim_as(st, "x", "bs2/gpu1", pid=PID + 1, started=1.0)
        out = ResourceTools(store_fn=lambda: st).call("resource-release",
                                                      {"all": True})
        assert f"RELEASED {mine.id}" in out
        assert st.get(mine.id) is None and st.get(theirs.id) is not None

    def test_acquire_reuses_the_process_claim_and_restamps_it(self, st):
        write_session_file("live-b")
        c = claim_as(st, "old-a", "bs2/gpu0")
        out = ResourceTools(store_fn=lambda: st).call(
            "resource-acquire", {"resource": "bs2/gpu0"})
        assert out.startswith(f"ALREADY HELD {c.id}"), out
        assert st.get(c.id).claim.session_id == "live-b"


@linux_only
class TestCli:
    def test_cli_extends_a_claim_of_an_earlier_session_id(self, st, capsys):
        write_session_file("live-b")
        c = claim_as(st, "old-a", "bs2/gpu1")
        assert cli.main(["extend", "--claim", c.id, "--by", "1h"]) == 0
        assert st.get(c.id).claim.session_id == "live-b"

    def test_a_claim_id_positional_is_read_as_the_claim(self, st, capsys):
        write_session_file("live-b")
        c = claim_as(st, "live-b", "bs2/gpu1")
        assert cli.main(["extend", c.id, "--by", "1h"]) == 0
        assert f"EXTENDED {c.id}" in capsys.readouterr().out
        assert cli.main(["release", c.id]) == 0
        assert st.get(c.id) is None


class TestHooks:
    def test_session_start_adopts_the_process_claims(self, st):
        c = claim_as(st, "old-a", "bs2/gpu0")
        other = claim_as(st, "x", "bs2/gpu1", pid=PID + 1, started=1.0)
        text = hook.session_block(event={"session_id": "new-b",
                                         "claude_client": [PID, STARTED]},
                                  config={})
        assert "yours (held): bs2/gpu0" in text
        assert st.get(c.id).claim.session_id == "new-b"
        assert st.get(other.id).claim.session_id == "x"

    @pytest.mark.parametrize("reason", sorted(hook.CONTINUING_REASONS))
    def test_session_end_on_clear_keeps_the_claims(self, st, reason, monkeypatch):
        monkeypatch.setattr(hook, "_is_claude_pid", lambda pid: True)
        c = claim_as(st, "old-a", "bs2/gpu0")
        assert hook.session_end(event={"session_id": "old-a",
                                       "reason": reason}, config={}) == 0
        assert st.get(c.id) is not None

    def test_session_end_on_exit_still_releases(self, st, monkeypatch):
        monkeypatch.setattr(hook, "_is_claude_pid", lambda pid: True)
        c = claim_as(st, "old-a", "bs2/gpu0")
        assert hook.session_end(event={"session_id": "old-a",
                                       "reason": "prompt_input_exit"},
                                config={}) == 1
        assert st.get(c.id) is None


@linux_only
def test_task_notes_are_stamped_with_the_live_session(tmp_path, monkeypatch):
    from claude_hooks.tasks.tools import tools_for_provider
    monkeypatch.setenv("CLAUDE_HOOKS_CLIENT_PID", str(PID))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spawned-a")
    tools = tools_for_provider(None, cwd=str(tmp_path))
    assert tools.svc.session_id == "spawned-a"
    write_session_file("live-b")
    assert tools.svc.session_id == "live-b"
