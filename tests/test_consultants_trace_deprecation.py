"""Phase 6 tests: the v1.0 tracer is decommissioned. The Tracer
class, TracedChat / traced_tool / traced_node remain importable as
no-op shims so the runner / graph builder don't need source
changes, but no JSONL is ever written and CONSULTANTS_TRACE prints
a one-shot deprecation warning.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

from consultants.engine import trace as trace_mod


@pytest.fixture
def fresh_trace_module(monkeypatch):
    """Reload the module so the per-process deprecation latch is
    cleared between tests."""
    importlib.reload(trace_mod)
    yield trace_mod


class TestTracerNoOp:
    def test_for_session_returns_disabled_tracer_even_with_env_set(
            self, fresh_trace_module, monkeypatch):
        monkeypatch.setenv("CONSULTANTS_TRACE", "1")
        t = fresh_trace_module.Tracer.for_session("csl-x")
        assert t.enabled is False

    def test_record_methods_are_noops(self, fresh_trace_module):
        t = fresh_trace_module.Tracer.for_session("csl-x")
        # Should not raise, should not write anywhere.
        t.record_llm(role="planner", model="m", duration_ms=10)
        t.record_tool(role="researcher", tool="read_file",
                      duration_ms=10)

    def test_span_pushes_role_for_current_role(self, fresh_trace_module):
        t = fresh_trace_module.Tracer.for_session("csl-x")
        assert t.current_role() is None
        with t.span("researcher"):
            assert t.current_role() == "researcher"
        assert t.current_role() is None

    def test_does_not_create_legacy_directory(
            self, fresh_trace_module, monkeypatch, tmp_path, isolated_home):
        # Even with CONSULTANTS_TRACE on and a fake home,
        # for_session must not create ~/.claude/consultants-traces.
        # ``isolated_home`` redirects Path.home() to tmp_path cross-platform
        # (bug-635: HOME-only no-op'd on Windows, so this asserted against the
        # wrong dir and could touch the real ~/.claude).
        monkeypatch.setenv("CONSULTANTS_TRACE", "1")
        fresh_trace_module.Tracer.for_session("csl-x")
        legacy = tmp_path / ".claude" / "consultants-traces"
        assert not legacy.exists()


class TestDeprecationLogging:
    def test_legacy_env_logs_once_at_for_session(
            self, fresh_trace_module, monkeypatch, caplog):
        monkeypatch.setenv("CONSULTANTS_TRACE", "1")
        with caplog.at_level(logging.WARNING,
                             logger="consultants.engine.trace"):
            fresh_trace_module.Tracer.for_session("csl-1")
            fresh_trace_module.Tracer.for_session("csl-2")
            fresh_trace_module.Tracer.for_session("csl-3")
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        # One log line, despite three for_session calls.
        deprecations = [w for w in warnings
                        if "deprecated" in w.getMessage().lower()]
        assert len(deprecations) == 1

    def test_no_log_when_env_unset(
            self, fresh_trace_module, monkeypatch, caplog):
        monkeypatch.delenv("CONSULTANTS_TRACE", raising=False)
        with caplog.at_level(logging.WARNING,
                             logger="consultants.engine.trace"):
            fresh_trace_module.Tracer.for_session("csl-1")
        deprecations = [r for r in caplog.records
                        if "deprecated" in r.getMessage().lower()]
        assert deprecations == []


class TestPassThroughWrappers:
    def test_traced_chat_forwards_chat(self, fresh_trace_module):
        class FakeClient:
            def __init__(self):
                self.calls = 0
            def chat(self, payload):
                self.calls += 1
                return {"choices": [{"message": {"content": "x"}}]}

        c = FakeClient()
        wrapped = fresh_trace_module.TracedChat(c, role="planner")
        out = wrapped.chat({"model": "m"})
        assert out["choices"][0]["message"]["content"] == "x"
        assert c.calls == 1
        # _client survives so runner.py's getattr(c, "_client", c)
        # extraction still finds the raw client.
        assert wrapped._client is c

    def test_traced_chat_forwards_arbitrary_attributes(
            self, fresh_trace_module):
        class FakeClient:
            tag = "warm"
            def chat(self, payload):
                return {}
        wrapped = fresh_trace_module.TracedChat(FakeClient(), role="r")
        assert wrapped.tag == "warm"

    def test_traced_tool_returns_executor_unchanged(
            self, fresh_trace_module):
        def real(name, args, cwd):
            return f"ran {name}"
        out = fresh_trace_module.traced_tool(real)
        assert out is real
        assert out("read_file", "{}", "/tmp") == "ran read_file"

    def test_traced_node_returns_fn_unchanged(self, fresh_trace_module):
        def fn(state):
            return {"plan": "x"}
        out = fresh_trace_module.traced_node(fn, role="planner")
        assert out is fn
        assert out({}) == {"plan": "x"}


class TestCLITraceFlagDeprecation:
    """The --trace / --no-trace CLI flags are still accepted but
    emit a one-shot deprecation warning. The body sent to the engine
    no longer carries a 'trace' key — the engine ignores it anyway."""

    def test_cmd_consult_warns_and_does_not_send_trace_field(
            self, monkeypatch, capsys):
        from consultants import cli
        # Reset the per-process deprecation latch.
        monkeypatch.setattr(cli, "_TRACE_DEPRECATION_LOGGED", False)

        captured: dict = {}

        def fake_http(method, url, *, body=None, timeout=600.0):
            captured["body"] = dict(body or {})
            return {"sid": "csl-x", "status": "running",
                    "status_url": "/v1/consult/csl-x"}

        monkeypatch.setattr(cli, "_http", fake_http)

        class _Args:
            message = "smoke"
            cwd = "/tmp/x"
            effort = None
            trace = True

        rc = cli.cmd_consult(_Args(), base="http://x")
        out = capsys.readouterr()
        assert rc == 0
        assert "trace" not in captured["body"]
        assert "deprecated" in out.err.lower()

    def test_one_shot_warning_only(self, monkeypatch, capsys):
        from consultants import cli
        monkeypatch.setattr(cli, "_TRACE_DEPRECATION_LOGGED", False)
        monkeypatch.setattr(
            cli, "_http",
            lambda *a, **kw: {"sid": "x", "status": "running",
                              "status_url": "/v1/consult/x"},
        )

        class _Args:
            message = "smoke"
            cwd = "/tmp/x"
            effort = None
            trace = True

        cli.cmd_consult(_Args(), base="http://x")
        cli.cmd_consult(_Args(), base="http://x")
        cli.cmd_consult(_Args(), base="http://x")
        out = capsys.readouterr()
        # One warning across three calls.
        assert out.err.lower().count("deprecated") == 1


class TestTraceSummaryScript:
    """The waterfall script now reads transcript.db, not JSONL."""

    def test_summarizes_db_with_meta_and_events(self, tmp_path):
        # Build a minimal transcript.db via the recorder, then run
        # the script in-process (import + summarize) on the result.
        from consultants.engine.recorder import (
            MessageRecorder, RecorderMeta,
        )
        meta = RecorderMeta(
            sid="csl-trace-test", cwd=str(tmp_path),
            question="why?", effort="medium", topology="council",
            models={"planner": "m", "synthesizer": "m"},
        )
        db_path = tmp_path / "transcript.db"
        rec = MessageRecorder(db_path, meta=meta)
        try:
            rec.record_node(role="planner", kind="node_enter")
            rec.record_llm(
                role="planner", round=1, model="m",
                request={"messages": []},
                response={"choices": [
                    {"message": {"content": "plan"}}]},
                prompt_tokens=42, completion_tokens=11,
                duration_ms=200,
            )
            rec.record_node(
                role="planner", kind="node_exit", duration_ms=210,
            )
            rec.finalize(status="completed")
        finally:
            rec.close()

        # Import the script as a module so we can call summarize()
        # directly without spawning a subprocess.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "consultants_trace_summary",
            Path(__file__).parents[1] / "scripts"
            / "consultants_trace_summary.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        # summarize() should run without error against the .db.
        # (Output goes to stdout; we just check it doesn't raise.)
        mod.summarize(db_path)

    def test_jsonl_path_emits_helpful_error(self, tmp_path):
        # If the user points the script at a v1.0 JSONL, error out
        # with a migration tip instead of a stack trace.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "consultants_trace_summary",
            Path(__file__).parents[1] / "scripts"
            / "consultants_trace_summary.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        bogus = tmp_path / "old.jsonl"
        bogus.write_text("{}\n")
        with pytest.raises(SystemExit) as ei:
            mod._resolve_path(str(bogus), tmp_path)
        assert "v1.0" in str(ei.value)
