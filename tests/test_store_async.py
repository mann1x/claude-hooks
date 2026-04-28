"""Tests for the detached store helper (Tier 1.3 latency reduction)."""
from __future__ import annotations

import json
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from claude_hooks import store_async


# ===================================================================== #
# spawn() — payload serialisation + Popen wiring
# ===================================================================== #
class TestSpawn:
    def test_spawn_returns_false_on_unserialisable_payload(self):
        # An object that json.dumps cannot encode.
        class _NotJson:
            pass
        payload = {"summary": "x", "metadata": {"obj": _NotJson()}}
        assert store_async.spawn(payload) is False

    def test_spawn_invokes_popen_with_module_runner(self):
        fake_proc = MagicMock()
        fake_proc.stdin = MagicMock()
        fake_proc.pid = 12345
        with patch.object(
            store_async.subprocess, "Popen", return_value=fake_proc,
        ) as popen:
            ok = store_async.spawn({
                "config": {}, "summary": "s", "metadata": {},
                "provider_names": ["qdrant"],
            })
        assert ok is True
        popen.assert_called_once()
        args, kwargs = popen.call_args
        cmdline = args[0]
        assert cmdline[0] == sys.executable
        assert cmdline[1] == "-m"
        assert cmdline[2] == "claude_hooks.store_async"
        # Detached: new session + DEVNULL stdio.
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] == store_async.subprocess.DEVNULL
        assert kwargs["stderr"] == store_async.subprocess.DEVNULL

    def test_spawn_writes_payload_to_stdin(self):
        fake_proc = MagicMock()
        fake_proc.stdin = MagicMock()
        fake_proc.pid = 99
        with patch.object(
            store_async.subprocess, "Popen", return_value=fake_proc,
        ):
            store_async.spawn({
                "config": {"providers": {}}, "summary": "ABC",
                "metadata": {"k": "v"}, "provider_names": ["qdrant"],
            })
        # The exact bytes written are the JSON-encoded payload.
        written = fake_proc.stdin.write.call_args[0][0]
        decoded = json.loads(written.decode("utf-8"))
        assert decoded["summary"] == "ABC"
        assert decoded["provider_names"] == ["qdrant"]
        fake_proc.stdin.close.assert_called_once()

    def test_spawn_returns_false_on_oserror(self):
        with patch.object(
            store_async.subprocess, "Popen", side_effect=OSError("nope"),
        ):
            ok = store_async.spawn({
                "config": {}, "summary": "s", "metadata": {},
                "provider_names": ["qdrant"],
            })
        assert ok is False

    def test_spawn_kills_proc_on_stdin_failure(self):
        fake_proc = MagicMock()
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        fake_proc.pid = 1
        with patch.object(
            store_async.subprocess, "Popen", return_value=fake_proc,
        ):
            ok = store_async.spawn({
                "config": {}, "summary": "s", "metadata": {},
                "provider_names": ["qdrant"],
            })
        assert ok is False
        fake_proc.kill.assert_called_once()


# ===================================================================== #
# main() — stdin parsing + dispatcher rebuild + dedup/store fan-out
# ===================================================================== #
class TestMain:
    def test_main_no_stdin_returns_zero(self):
        with patch.object(sys, "stdin") as stdin:
            stdin.buffer.read.return_value = b""
            assert store_async.main() == 0

    def test_main_invalid_json_returns_one(self):
        with patch.object(sys, "stdin") as stdin:
            stdin.buffer.read.return_value = b"not json {{{"
            assert store_async.main() == 1

    def test_main_empty_summary_returns_zero_no_providers_built(self):
        payload = json.dumps({
            "config": {}, "summary": "",
            "metadata": {}, "provider_names": ["qdrant"],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch("claude_hooks.dispatcher.build_providers") as bp:
            stdin.buffer.read.return_value = payload
            assert store_async.main() == 0
            bp.assert_not_called()

    def test_main_no_provider_names_skips_build(self):
        payload = json.dumps({
            "config": {}, "summary": "a summary",
            "metadata": {}, "provider_names": [],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch("claude_hooks.dispatcher.build_providers") as bp:
            stdin.buffer.read.return_value = payload
            store_async.main()
            bp.assert_not_called()

    def test_main_filters_providers_to_requested_names(self):
        # build_providers returns two providers; payload requests only one.
        prov_a = MagicMock(name="provA"); prov_a.name = "qdrant"
        prov_b = MagicMock(name="provB"); prov_b.name = "memory_kg"
        payload = json.dumps({
            "config": {"providers": {"qdrant": {}, "memory_kg": {}}},
            "summary": "noteworthy summary text",
            "metadata": {"type": "fix"},
            "provider_names": ["qdrant"],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch(
                 "claude_hooks.dispatcher.build_providers",
                 return_value=[prov_a, prov_b],
             ):
            stdin.buffer.read.return_value = payload
            store_async.main()
        prov_a.store.assert_called_once()
        prov_b.store.assert_not_called()

    def test_main_calls_provider_store_with_summary_and_metadata(self):
        prov = MagicMock(); prov.name = "qdrant"
        payload = json.dumps({
            "config": {"providers": {"qdrant": {}}},
            "summary": "the turn summary",
            "metadata": {"type": "fix", "session_id": "s1"},
            "provider_names": ["qdrant"],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch(
                 "claude_hooks.dispatcher.build_providers",
                 return_value=[prov],
             ):
            stdin.buffer.read.return_value = payload
            store_async.main()
        args, kwargs = prov.store.call_args
        assert args[0] == "the turn summary"
        assert kwargs["metadata"]["type"] == "fix"

    def test_main_swallows_provider_store_error(self):
        prov = MagicMock(); prov.name = "qdrant"
        prov.store.side_effect = RuntimeError("MCP unavailable")
        payload = json.dumps({
            "config": {"providers": {"qdrant": {}}},
            "summary": "summary",
            "metadata": {},
            "provider_names": ["qdrant"],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch(
                 "claude_hooks.dispatcher.build_providers",
                 return_value=[prov],
             ):
            stdin.buffer.read.return_value = payload
            # Must not raise — failure logged + suppressed.
            assert store_async.main() == 0

    def test_main_dedup_skip_does_not_call_store(self):
        prov = MagicMock(); prov.name = "qdrant"
        # 100+ char summary triggers dedup; threshold > 0 enables it.
        big_summary = "x" * 200
        payload = json.dumps({
            "config": {
                "providers": {"qdrant": {"dedup_threshold": 0.5}},
            },
            "summary": big_summary,
            "metadata": {},
            "provider_names": ["qdrant"],
        }).encode()
        with patch.object(sys, "stdin") as stdin, \
             patch(
                 "claude_hooks.dispatcher.build_providers",
                 return_value=[prov],
             ), patch(
                 "claude_hooks.dedup.should_store", return_value=False,
             ):
            stdin.buffer.read.return_value = payload
            store_async.main()
        prov.store.assert_not_called()


# ===================================================================== #
# Stop hook integration — verify detach is gated by config flag
# ===================================================================== #
class TestStopHandlerDetachIntegration:
    def test_detach_disabled_runs_inline(
        self, base_config, transcript_file, fake_provider,
    ):
        from claude_hooks.hooks import stop
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please", assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        # Default: detach_store=False — store happens inline.
        stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
            config=base_config(),
            providers=[p],
        )
        assert len(p.stored) == 1

    def test_detach_enabled_spawns_and_skips_inline_store(
        self, base_config, transcript_file, fake_provider,
    ):
        from claude_hooks.hooks import stop
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please", assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        cfg = base_config(hooks={"stop": {"detach_store": True}})
        with patch(
            "claude_hooks.store_async.spawn", return_value=True,
        ) as spawn:
            r = stop.handle(
                event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
                config=cfg,
                providers=[p],
            )
        spawn.assert_called_once()
        # Inline store path was skipped — fake provider has no entries.
        assert p.stored == []
        # systemMessage indicates async storage.
        assert r is not None
        assert "async" in r["systemMessage"]
        assert "qdrant" in r["systemMessage"]

    def test_detach_falls_back_to_inline_on_spawn_failure(
        self, base_config, transcript_file, fake_provider,
    ):
        from claude_hooks.hooks import stop
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please", assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        cfg = base_config(hooks={"stop": {"detach_store": True}})
        with patch("claude_hooks.store_async.spawn", return_value=False):
            r = stop.handle(
                event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
                config=cfg,
                providers=[p],
            )
        # Spawn failed → inline path runs and the fake provider got stored to.
        assert len(p.stored) == 1
        assert r is not None
        assert "stored to qdrant" in r["systemMessage"]

    def test_detach_skipped_when_no_auto_providers(
        self, base_config, transcript_file, fake_provider,
    ):
        from claude_hooks.hooks import stop
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please", assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        cfg = base_config(
            hooks={"stop": {"detach_store": True}},
            providers={"qdrant": {"store_mode": "off"}},
        )
        with patch("claude_hooks.store_async.spawn") as spawn:
            stop.handle(
                event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
                config=cfg,
                providers=[p],
            )
        # No auto providers → spawn never called.
        spawn.assert_not_called()
