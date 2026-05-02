"""Handler tests for UserPromptSubmit, SessionStart, SessionEnd.

Stop handler tests split between test_stop_guard.py (meta-context
escape and patterns), test_pre_tool_use_handler.py (integration with
stop_guard via handler), and the Stop store/dedup/claudemem-reindex
wiring is also covered here under TestStopStoreHandler.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_hooks.hooks import session_end, session_start, stop, user_prompt_submit


# ===================================================================== #
# UserPromptSubmit
# ===================================================================== #
class TestUserPromptSubmitHandler:
    def test_disabled_returns_none(self, base_config, fake_provider):
        cfg = base_config(hooks={"user_prompt_submit": {"enabled": False}})
        r = user_prompt_submit.handle(
            event={"prompt": "long enough prompt to exceed min_chars"},
            config=cfg,
            providers=[fake_provider()],
        )
        assert r is None

    def test_short_prompt_returns_none(self, base_config, fake_provider):
        """With the always-on ## Now block, even short prompts get a
        timestamp injection. Disable now_block in this test so we
        keep verifying the original 'recall is skipped' behaviour."""
        cfg = base_config(
            hooks={"user_prompt_submit": {"min_prompt_chars": 50}},
            system={"now_block": {"enabled": False}},
        )
        r = user_prompt_submit.handle(
            event={"prompt": "tiny"},
            config=cfg,
            providers=[fake_provider()],
        )
        assert r is None

    def test_short_prompt_emits_now_block_only(self, base_config, fake_provider):
        """Mirror of the above — confirms the now-block fires when
        recall is skipped (default config has now_block enabled)."""
        cfg = base_config(hooks={"user_prompt_submit": {"min_prompt_chars": 50}})
        r = user_prompt_submit.handle(
            event={"prompt": "tiny"},
            config=cfg,
            providers=[fake_provider()],
        )
        assert r is not None
        ac = r["hookSpecificOutput"]["additionalContext"]
        assert "## Now" in ac
        assert "## Recalled memory" not in ac

    def test_no_providers_returns_none(self, base_config):
        cfg = base_config(system={"now_block": {"enabled": False}})
        r = user_prompt_submit.handle(
            event={"prompt": "long enough prompt to exceed min_chars"},
            config=cfg,
            providers=[],
        )
        assert r is None

    def test_no_providers_still_emits_now_block(self, base_config):
        """Even with zero providers (so recall returns nothing), the
        now-block must surface so the model gets a fresh timestamp."""
        r = user_prompt_submit.handle(
            event={"prompt": "long enough prompt to exceed min_chars"},
            config=base_config(),
            providers=[],
        )
        assert r is not None
        ac = r["hookSpecificOutput"]["additionalContext"]
        assert "## Now" in ac

    def test_happy_path_emits_additional_context(self, base_config, fake_provider):
        from claude_hooks.providers.base import Memory
        p = fake_provider(
            name="qdrant",
            recall_returns=[Memory(text="hit one"), Memory(text="hit two")],
        )
        r = user_prompt_submit.handle(
            event={"prompt": "long enough prompt to exceed min_chars"},
            config=base_config(),
            providers=[p],
        )
        assert r is not None
        h = r["hookSpecificOutput"]
        assert h["hookEventName"] == "UserPromptSubmit"
        assert "hit one" in h["additionalContext"]
        assert "hit two" in h["additionalContext"]

    def test_passes_cwd_and_max_chars_through(self, base_config, fake_provider):
        cfg = base_config(hooks={"user_prompt_submit": {"max_total_chars": 123}})
        with patch("claude_hooks.recall.run_recall", return_value="ctx") as m:
            user_prompt_submit.handle(
                event={"prompt": "long enough prompt to exceed min_chars",
                       "cwd": "/proj"},
                config=cfg,
                providers=[fake_provider()],
            )
        kwargs = m.call_args.kwargs
        assert kwargs["cwd"] == "/proj"
        assert kwargs["max_total_chars"] == 123


# ===================================================================== #
# SessionStart
# ===================================================================== #
class TestSessionStartHandler:
    def test_disabled_returns_none(self, base_config, fake_provider):
        cfg = base_config(hooks={"session_start": {"enabled": False}})
        r = session_start.handle(
            event={"source": "startup"},
            config=cfg,
            providers=[fake_provider()],
        )
        assert r is None

    def test_no_providers_fires_reindex_and_returns_none(
        self, base_config, fake_provider,
    ):
        # With claudemem_reindex enabled and no providers, the handler
        # still fires the stale-check and then returns None.
        cfg = base_config(hooks={"claudemem_reindex": {"enabled": True,
                                                        "check_on_session_start": True}})
        with patch(
            "claude_hooks.claudemem_reindex.reindex_if_stale_async",
        ) as m_reindex:
            r = session_start.handle(
                event={"source": "startup", "cwd": "/proj"},
                config=cfg,
                providers=[],
            )
        assert r is None
        m_reindex.assert_called_once()

    def test_status_line_uses_verb_from_source(self, base_config, fake_provider):
        p = fake_provider(name="qdrant")
        for src, expected_verb in [
            ("startup", "Started"),
            ("resume", "Resumed"),
            ("compact", "Compacted"),
        ]:
            # Compact triggers recall; stub run_recall to return None so
            # we just get the status line back.
            with patch("claude_hooks.recall.run_recall", return_value=None):
                r = session_start.handle(
                    event={"source": src},
                    config=base_config(),
                    providers=[p],
                )
            ctx = r["hookSpecificOutput"]["additionalContext"]
            assert expected_verb in ctx

    def test_compact_triggers_recall_with_compact_query(
        self, base_config, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        cfg = base_config(hooks={"session_start": {
            "compact_recall": True,
            "compact_recall_query": "summary please",
        }})
        with patch(
            "claude_hooks.recall.run_recall",
            return_value="## Recalled memory\n- prior context",
        ) as m:
            r = session_start.handle(
                event={"source": "compact"},
                config=cfg,
                providers=[p],
            )
        assert m.call_args.args[0] == "summary please"
        assert "prior context" in r["hookSpecificOutput"]["additionalContext"]

    def test_reindex_disabled_does_not_fire(self, base_config, fake_provider):
        cfg = base_config(hooks={"claudemem_reindex": {"enabled": False}})
        with patch(
            "claude_hooks.claudemem_reindex.reindex_if_stale_async",
        ) as m_reindex:
            session_start.handle(
                event={"source": "startup"},
                config=cfg,
                providers=[fake_provider()],
            )
        m_reindex.assert_not_called()


# ===================================================================== #
# SessionEnd
# ===================================================================== #
class TestSessionEndHandler:
    def test_disabled_returns_none(self, base_config):
        cfg = base_config(hooks={"session_end": {"enabled": False}})
        assert session_end.handle(event={}, config=cfg, providers=[]) is None

    def test_episodic_off_returns_none(self, base_config):
        cfg = base_config(episodic={"mode": "off"})
        assert session_end.handle(event={}, config=cfg, providers=[]) is None

    def test_episodic_client_no_url_returns_none(self, base_config):
        cfg = base_config(episodic={"mode": "client", "server_url": ""})
        assert session_end.handle(event={}, config=cfg, providers=[]) is None

    def test_episodic_client_pushes_transcript(
        self, base_config, tmp_path,
    ):
        # Write a transcript big enough to pass the 100-byte floor.
        tp = tmp_path / "transcript.jsonl"
        tp.write_text("x" * 500)
        cfg = base_config(episodic={
            "mode": "client",
            "server_url": "http://episodic.local",
            "timeout": 5.0,
        })
        captured = {}

        def _capture(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["has_project_header"] = bool(req.get_header("X-project"))
            from io import BytesIO
            class _R:
                def __init__(self): self._b = BytesIO(b'{"project":"test"}')
                def read(self, *a, **kw): return self._b.read(*a, **kw)
                def __enter__(self): return self
                def __exit__(self, *a): self._b.close()
            return _R()

        with patch(
            "claude_hooks.hooks.session_end.urllib.request.urlopen",
            side_effect=_capture,
        ):
            session_end.handle(
                event={
                    "transcript_path": str(tp),
                    "cwd": "/proj",
                    "session_id": "abc123",
                },
                config=cfg,
                providers=[],
            )
        assert captured["url"] == "http://episodic.local/ingest"
        assert captured["method"] == "POST"
        assert captured["has_project_header"]

    def test_episodic_client_push_failure_is_caught(
        self, base_config, tmp_path,
    ):
        tp = tmp_path / "transcript.jsonl"
        tp.write_text("x" * 500)
        cfg = base_config(episodic={
            "mode": "client",
            "server_url": "http://episodic.local",
        })
        import urllib.error
        with patch(
            "claude_hooks.hooks.session_end.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            # Must not raise.
            r = session_end.handle(
                event={"transcript_path": str(tp), "cwd": "/p",
                       "session_id": "s"},
                config=cfg,
                providers=[],
            )
        assert r is None

    def test_episodic_client_skips_missing_transcript(self, base_config):
        cfg = base_config(episodic={
            "mode": "client",
            "server_url": "http://episodic.local",
        })
        r = session_end.handle(
            event={"transcript_path": "/nonexistent/transcript.jsonl"},
            config=cfg,
            providers=[],
        )
        assert r is None

    def test_episodic_client_skips_tiny_transcript(
        self, base_config, tmp_path,
    ):
        tp = tmp_path / "tiny.jsonl"
        tp.write_text("hi")  # under the 100-byte floor
        cfg = base_config(episodic={
            "mode": "client",
            "server_url": "http://episodic.local",
        })
        with patch(
            "claude_hooks.hooks.session_end.urllib.request.urlopen",
        ) as m:
            session_end.handle(
                event={"transcript_path": str(tp), "cwd": "/p",
                       "session_id": "s"},
                config=cfg,
                providers=[],
            )
        m.assert_not_called()

    def test_episodic_server_triggers_local_sync(self, base_config):
        cfg = base_config(episodic={"mode": "server", "binary": "episodic-memory"})
        with patch("subprocess.Popen") as m:
            session_end.handle(event={}, config=cfg, providers=[])
        m.assert_called_once()
        args = m.call_args.args[0]
        assert args[0] == "episodic-memory"
        assert "sync" in args

    def test_episodic_server_binary_missing_is_caught(self, base_config):
        cfg = base_config(episodic={"mode": "server", "binary": "does-not-exist"})
        with patch("subprocess.Popen", side_effect=FileNotFoundError("no bin")):
            # Must not raise.
            r = session_end.handle(event={}, config=cfg, providers=[])
        assert r is None


# ===================================================================== #
# Stop — the store/dedup/reindex half complementing stop_guard tests
# ===================================================================== #
class TestStopStoreHandler:
    def test_disabled_returns_none(self, base_config, transcript_file, fake_provider):
        cfg = base_config(hooks={"stop": {"enabled": False}})
        path = transcript_file(
            user="u",
            assistant_text="a",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        r = stop.handle(
            event={"transcript_path": path, "cwd": "/p"},
            config=cfg,
            providers=[fake_provider()],
        )
        assert r is None

    def test_store_threshold_off(self, base_config, transcript_file, fake_provider):
        cfg = base_config(hooks={"stop": {"store_threshold": "off"}})
        p = fake_provider()
        path = transcript_file(
            user="u",
            assistant_text="a",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        stop.handle(
            event={"transcript_path": path, "cwd": "/p"},
            config=cfg,
            providers=[p],
        )
        assert p.stored == []

    def test_noteworthy_edit_triggers_store(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="please edit",
            assistant_text="done",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
        )
        stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s1"},
            config=base_config(),
            providers=[p],
        )
        assert len(p.stored) == 1
        content, meta = p.stored[0]
        assert "edit" in content.lower() or "x.py" in content
        assert meta["type"] == "session_turn"

    def test_non_noteworthy_turn_skips_store(
        self, base_config, transcript_file, fake_provider,
    ):
        # No tool_use blocks at all → not noteworthy.
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="tell me a joke",
            assistant_text="why did the chicken cross the road",
        )
        stop.handle(
            event={"transcript_path": path, "cwd": "/p"},
            config=base_config(),
            providers=[p],
        )
        assert p.stored == []

    def test_meta_prompt_filtered_from_summary(
        self, base_config, transcript_file, fake_provider,
    ):
        # Promptstop filters messages containing known meta-markers.
        p = fake_provider(name="qdrant")
        path = transcript_file(
            user="You are an expert developer experience engineer. "
                 "extract reusable operational lessons from the events.",
            assistant_text="here are the lessons",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        stop.handle(
            event={"transcript_path": path, "cwd": "/p", "session_id": "s"},
            config=base_config(),
            providers=[p],
        )
        # Store ran (edit happened), but the meta-prompt is not echoed
        # in the stored content.
        assert len(p.stored) == 1
        content, _ = p.stored[0]
        assert "extract reusable operational lessons" not in content

    def test_claudemem_reindex_called_on_edit_turn(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        cfg = base_config(hooks={"claudemem_reindex": {"enabled": True,
                                                        "check_on_stop": True}})
        path = transcript_file(
            user="u",
            assistant_text="a",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        with patch(
            "claude_hooks.claudemem_reindex.reindex_if_dirty_async",
        ) as m:
            stop.handle(
                event={"transcript_path": path, "cwd": "/p"},
                config=cfg,
                providers=[p],
            )
        m.assert_called_once()
        assert m.call_args.kwargs.get("turn_modified") is True

    def test_claudemem_reindex_not_called_without_edit(
        self, base_config, transcript_file, fake_provider,
    ):
        p = fake_provider(name="qdrant")
        cfg = base_config(hooks={"claudemem_reindex": {"enabled": True,
                                                        "check_on_stop": True}})
        path = transcript_file(user="u", assistant_text="a")
        with patch(
            "claude_hooks.claudemem_reindex.reindex_if_dirty_async",
        ) as m:
            stop.handle(
                event={"transcript_path": path, "cwd": "/p"},
                config=cfg,
                providers=[p],
            )
        m.assert_not_called()

    def test_stop_guard_blocks_stop(
        self, base_config, transcript_file, fake_provider,
    ):
        # Assistant message contains a trigger phrase with no meta-markers
        # and no quotes, so stop_guard should fire.
        p = fake_provider(name="qdrant")
        cfg = base_config(hooks={"stop_guard": {"enabled": True}})
        path = transcript_file(
            user="u",
            assistant_text="The failing test is a pre-existing issue, not my concern.",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        r = stop.handle(
            event={"transcript_path": path, "cwd": "/p", "stop_hook_active": False},
            config=cfg,
            providers=[p],
        )
        assert r is not None
        assert r["decision"] == "block"
        assert "PRE-EXISTING" in r["reason"].upper()
        # Store did NOT run because the hook returned early.
        assert p.stored == []

    def test_stop_guard_respects_hook_active_flag(
        self, base_config, transcript_file, fake_provider,
    ):
        # When stop_hook_active=True, the guard is bypassed to prevent loops.
        p = fake_provider(name="qdrant")
        cfg = base_config(hooks={"stop_guard": {"enabled": True}})
        path = transcript_file(
            user="u",
            assistant_text="pre-existing issue here",
            assistant_tools=[{"name": "Edit", "input": {"file_path": "x"}}],
        )
        r = stop.handle(
            event={"transcript_path": path, "cwd": "/p", "stop_hook_active": True},
            config=cfg,
            providers=[p],
        )
        # Guard bypassed — store should have run normally.
        assert r is None or "decision" not in r
        assert len(p.stored) == 1
