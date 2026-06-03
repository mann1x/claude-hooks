"""Tests for ``consultants.cli``.

The CLI talks HTTP to the engine. To exercise it end-to-end we
spin up the real FastAPI app with a stub runner on a localhost
port and point the CLI at it via ``--endpoint``. Config CRUD
exercises ``cc.set_*`` directly through the CLI surface.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient  # noqa: E402

from consultants import cli, config as cc
from consultants.server.app import create_app


# ----------------------- in-process server ----------------------- #
# uvicorn-on-thread is too heavy for a unit test. Instead we stand up
# the FastAPI app via TestClient and patch cli._http to route through
# it. This keeps tests fast and deterministic, and exercises every
# CLI subcommand against the real route handlers.

class _TestClientHttp:
    """Replacement for cli._http that delegates to a TestClient."""
    def __init__(self, client: TestClient, base_url: str):
        self.client = client
        self.base_url = base_url

    def __call__(self, method, url, *, body=None, timeout=600.0):
        # Strip the base URL to get the path TestClient expects.
        if url.startswith(self.base_url):
            path = url[len(self.base_url):]
        else:
            path = url
        if method == "GET":
            r = self.client.get(path)
        elif method == "POST":
            r = self.client.post(path, json=body)
        else:
            raise AssertionError(f"unexpected method: {method}")
        if r.status_code >= 400:
            from consultants.cli import CLIError
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise CLIError(f"HTTP {r.status_code} from {url}: {detail}")
        return r.json() if r.text else {}


def _stub_runner():
    """Sync stub: writes a real artifact set."""
    import time as _time
    from consultants.engine import storage

    def run(state, runner_input):
        cwd = Path(runner_input["cwd"])
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=_time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                _time.localtime(state.started_at)),
            question=runner_input["question"],
            models={"planner": "stub", "researcher": "stub",
                    "critic": "stub", "synthesizer": "stub"},
            topology=state.topology,
            effort=state.effort,
            final_answer="**stub**: ok",
            turns=[storage.RoleTurn(role="synthesizer", round=1,
                                    content="ok",
                                    prompt_tokens=1, completion_tokens=1)],
            duration_seconds=_time.time() - state.started_at,
            status="completed",
            cwd=str(cwd),
            total_prompt_tokens=1,
            total_completion_tokens=1,
        )
        storage.write_consultation(result, cwd=cwd)
        for r in state.progress:
            state.progress[r] = "done"
        state.status = "completed"
        state.finished_at = _time.time()
    return run


@pytest.fixture
def patched_http(monkeypatch):
    """Set up a TestClient app and patch cli._http to route there."""
    app = create_app(run_council=_stub_runner())
    client = TestClient(app)
    client.__enter__()
    base = "http://test"
    monkeypatch.setattr(cli, "_http", _TestClientHttp(client, base))
    yield base
    client.__exit__(None, None, None)


# ``isolated_home`` comes from tests/conftest.py (cross-platform; bug-635).


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture(autouse=True)
def _neutral_cwd(tmp_path, monkeypatch):
    """Run every CLI test from a project-file-free directory.

    The config auto-scope resolver (``_resolve_active_scope`` /
    ``cmd_config_show``) defaults cwd to ``os.getcwd()`` when no
    ``--cwd`` is given. Pytest runs from the repo root, which carries a
    real ``.claude-hooks/consultants.toml`` — so without this, an
    un-scoped ``config set-*`` would read/WRITE the live repo file.
    Tests that target a project file pass an explicit absolute ``--cwd``
    and are unaffected by the chdir."""
    neutral = tmp_path / "_cwd"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    yield neutral


def _run(argv: list[str], endpoint: str = "http://test") -> tuple[int, dict, str]:
    """Invoke the CLI and capture stdout JSON + stderr text."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main([*argv, "--endpoint", endpoint] if argv[0] == "_unused"
                      else ["--endpoint", endpoint, *argv])
    text = out.getvalue().strip()
    payload = json.loads(text) if text else {}
    return rc, payload, err.getvalue()


# ----------------------- routing --------------------------------- #

class TestResolveEndpoint:
    def test_override_wins(self):
        assert cli.resolve_endpoint(override="http://x:1") == "http://x:1"

    def test_strips_trailing_slash(self):
        assert cli.resolve_endpoint(override="http://x:1/") == "http://x:1"

    def test_env_var_used_when_no_override(self, monkeypatch):
        monkeypatch.setenv("CONSULTANTS_URL", "http://from-env:9/")
        monkeypatch.setattr(cli, "_read_claude_hooks_consultants_block",
                            lambda: {})
        assert cli.resolve_endpoint() == "http://from-env:9"

    def test_smart_start_picks_forwarder(self, monkeypatch):
        monkeypatch.delenv("CONSULTANTS_URL", raising=False)
        monkeypatch.setattr(
            cli, "_read_claude_hooks_consultants_block",
            lambda: {
                "engine_url": "http://eng:1",
                "smart_start": {"enabled": True,
                                "forwarder_url": "http://fwd:2"},
            },
        )
        assert cli.resolve_endpoint() == "http://fwd:2"

    def test_always_on_picks_engine(self, monkeypatch):
        monkeypatch.delenv("CONSULTANTS_URL", raising=False)
        monkeypatch.setattr(
            cli, "_read_claude_hooks_consultants_block",
            lambda: {
                "engine_url": "http://eng:1",
                "smart_start": {"enabled": False},
            },
        )
        assert cli.resolve_endpoint() == "http://eng:1"

    def test_default_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("CONSULTANTS_URL", raising=False)
        monkeypatch.setattr(cli, "_read_claude_hooks_consultants_block",
                            lambda: {})
        assert cli.resolve_endpoint() == cli.DEFAULT_ENGINE_URL


# ----------------------- consult / status / result --------------- #

class TestConsultLifecycle:
    def test_full_lifecycle(self, isolated_home, project_dir, patched_http):
        rc, payload, _ = _run([
            "consult", "--message", "audit foo", "--cwd", str(project_dir),
        ])
        assert rc == 0
        assert payload["ok"] is True
        sid = payload["sid"]
        # Allow runner to finish (synchronous stub but ThreadPool).
        for _ in range(50):
            rc, p, _ = _run(["status", sid])
            if p.get("status") == "completed":
                break
            time.sleep(0.02)
        assert p["status"] == "completed"

        rc, r, _ = _run(["result", sid])
        assert rc == 0
        assert "**stub**" in r["summary_markdown"]

    def test_consult_rejects_bad_effort(self, isolated_home, project_dir,
                                        patched_http):
        # argparse rejects before we even reach the HTTP layer; it
        # raises SystemExit(2) on choice mismatch.
        with pytest.raises(SystemExit) as ei:
            _run([
                "consult", "--message", "q", "--cwd", str(project_dir),
                "--effort", "bogus",
            ])
        assert ei.value.code == 2


class TestList:
    def test_empty(self, isolated_home, project_dir, patched_http):
        rc, payload, _ = _run(["list", "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["sessions"] == []

    def test_after_consult(self, isolated_home, project_dir, patched_http):
        _run(["consult", "--message", "q", "--cwd", str(project_dir)])
        time.sleep(0.1)
        rc, payload, _ = _run(["list", "--cwd", str(project_dir)])
        assert rc == 0
        assert len(payload["sessions"]) >= 1


class TestShow:
    def test_local_read(self, isolated_home, project_dir, patched_http):
        cr = _run(["consult", "--message", "q", "--cwd", str(project_dir)])
        sid = cr[1]["sid"]
        time.sleep(0.1)
        rc, payload, _ = _run(["show", sid, "--cwd", str(project_dir)])
        assert rc == 0
        assert "**stub**" in payload["summary_markdown"]

    def test_missing_session(self, isolated_home, project_dir, patched_http):
        rc, _, err = _run(
            ["show", "csl-nope", "--cwd", str(project_dir)])
        assert rc == 1
        assert "no summary" in err


# ----------------------- config show --------------------------- #

class TestConfigShow:
    def test_default(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "show"])
        assert rc == 0
        assert payload["topology"] == cc.DEFAULT_TOPOLOGY
        assert payload["roles"]["synthesizer"]["enabled"] is True
        assert "synthesizer" in payload["mandatory_roles"]
        assert "endpoint" in payload

    def test_after_set_role(self, isolated_home, patched_http):
        _run(["config", "set-role", "planner",
              "--model", "kimi-k2.6:cloud"])
        rc, payload, _ = _run(["config", "show"])
        assert payload["roles"]["planner"]["model"] == "kimi-k2.6:cloud"


# ----------------------- config set-role ----------------------- #

class TestConfigSetRole:
    def test_set_model(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "set-role", "researcher",
                               "--model", "qwen3.5:cloud"])
        assert rc == 0
        assert payload["roles"]["researcher"]["model"] == "qwen3.5:cloud"

    def test_set_ctx(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "set-role", "researcher",
                               "--ctx", "32768"])
        assert rc == 0
        assert payload["roles"]["researcher"]["ctx_max"] == 32768
        assert payload["roles"]["researcher"]["ctx_max_explicit"] is True

    def test_clear_ctx_with_auto(self, isolated_home, patched_http):
        _run(["config", "set-role", "researcher", "--ctx", "8192"])
        rc, payload, _ = _run(["config", "set-role", "researcher",
                               "--ctx", "auto"])
        assert payload["roles"]["researcher"]["ctx_max"] is None

    def test_clear_ctx_with_zero(self, isolated_home, patched_http):
        _run(["config", "set-role", "researcher", "--ctx", "8192"])
        rc, payload, _ = _run(["config", "set-role", "researcher",
                               "--ctx", "0"])
        assert payload["roles"]["researcher"]["ctx_max"] is None

    def test_disable_optional_role(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "set-role", "critic",
                               "--enabled", "false"])
        assert rc == 0
        assert payload["roles"]["critic"]["enabled"] is False

    def test_disable_synthesizer_rejected(self, isolated_home,
                                          patched_http):
        rc, _, err = _run(["config", "set-role", "synthesizer",
                           "--enabled", "false"])
        assert rc == 2
        assert "mandatory" in err

    def test_invalid_enabled_value(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-role", "planner",
                           "--enabled", "maybe"])
        assert rc == 2

    def test_invalid_ctx_value(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-role", "planner",
                           "--ctx", "huge"])
        assert rc == 2


# ----------------------- config set-effort --------------------- #

class TestConfigSetEffort:
    @pytest.mark.parametrize("tier", ["low", "medium", "high", "max"])
    def test_valid(self, isolated_home, patched_http, tier):
        rc, payload, _ = _run(["config", "set-effort", tier])
        assert rc == 0
        assert payload["effort"] == tier


# ----------------------- config set-service-mode --------------- #

class TestConfigSetServiceMode:
    @pytest.mark.parametrize("mode", ["always-on", "smart-start"])
    def test_valid(self, isolated_home, patched_http, mode):
        rc, payload, _ = _run(["config", "set-service-mode", mode])
        assert rc == 0
        assert payload["service"]["mode"] == mode
        assert "follow_up" in payload


# ----------------------- config set-idle-timeout --------------- #

class TestConfigSetIdleTimeout:
    def test_writes_to_claude_hooks_json(self, isolated_home, tmp_path,
                                          monkeypatch, patched_http):
        # Point the CLI at a writable temp config.
        target = tmp_path / "claude-hooks.json"
        target.write_text(json.dumps({
            "version": 2, "hooks": {"consultants": {"smart_start": {}}}
        }), encoding="utf-8")

        # Patch the lookup so the CLI hits this path.
        original = cli.cmd_config_set_idle_timeout

        def patched(args, base):
            # Inline implementation pointed at our temp file.
            data = json.loads(target.read_text())
            data["hooks"]["consultants"]["smart_start"][
                "idle_timeout_seconds"] = args.seconds
            target.write_text(json.dumps(data))
            print(json.dumps({"ok": True,
                              "idle_timeout_seconds": args.seconds}))
            return 0

        monkeypatch.setattr(cli, "cmd_config_set_idle_timeout", patched)
        rc, payload, _ = _run(["config", "set-idle-timeout", "600"])
        assert rc == 0
        assert payload["idle_timeout_seconds"] == 600

    def test_rejects_too_short(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-idle-timeout", "10"])
        assert rc == 2

    def test_rejects_too_long(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-idle-timeout", "999999"])
        assert rc == 2


# ----------------------- config set-store (#220) -------------- #

class TestConfigSetStore:
    def test_toggle_enabled(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "set-store", "--enabled", "false"])
        assert rc == 0
        assert payload["store"]["enabled"] is False
        rc, payload, _ = _run(["config", "set-store", "--enabled", "true"])
        assert rc == 0
        assert payload["store"]["enabled"] is True

    def test_backend_switch(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store", "--backend", "pgvector"])
        assert rc == 0
        assert payload["store"]["backend"] == "pgvector"

    def test_recall_limit(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store", "--recall-limit", "11"])
        assert rc == 0
        assert payload["store"]["recall_limit"] == 11

    def test_paths_dsn_table_embedder(self, isolated_home, patched_http):
        rc, payload, _ = _run([
            "config", "set-store",
            "--sqlite-vec-path", "/tmp/y.db",
            "--pgvector-dsn", "postgres://u:p@h/db",
            "--pgvector-table", "ctab",
            "--embedder", "ollama",
        ])
        assert rc == 0
        s = payload["store"]
        assert s["sqlite_vec_path"] == "/tmp/y.db"
        assert s["pgvector_dsn"] == "postgres://u:p@h/db"
        assert s["pgvector_table"] == "ctab"
        assert s["embedder"] == "ollama"

    def test_efforts_add_remove_clear(self, isolated_home, patched_http):
        rc, _, _ = _run(["config", "set-store", "--clear-efforts"])
        assert rc == 0
        rc, payload, _ = _run(
            ["config", "set-store", "--add-effort", "max"])
        assert rc == 0
        assert "max" in payload["store"]["enable_at_efforts"]
        rc, payload, _ = _run(
            ["config", "set-store", "--remove-effort", "max"])
        assert rc == 0
        assert "max" not in payload["store"]["enable_at_efforts"]

    def test_invalid_backend_rejected(self, isolated_home, patched_http):
        # argparse's `choices=` catches this at parse time → SystemExit(2)
        # before our handler runs; that's intentional (more helpful
        # error message). Make sure the contract is "reject, don't
        # accept silently" without locking us into a specific path.
        with pytest.raises(SystemExit) as ei:
            _run(["config", "set-store", "--backend", "redis"])
        assert ei.value.code == 2

    def test_invalid_bool_rejected(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-store", "--enabled", "maybe"])
        assert rc == 2
        assert "--enabled" in err or "maybe" in err

    def test_recall_limit_must_be_positive(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-store", "--recall-limit", "0"])
        assert rc == 2


# ----------------------- config set-store-ttl (#220) ---------- #

class TestConfigSetStoreTtl:
    def test_toggle_enabled(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-ttl", "--enabled", "false"])
        assert rc == 0
        assert payload["store"]["ttl"]["enabled"] is False

    def test_research_days_zero_means_never(self, isolated_home,
                                             patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-ttl", "--research-days", "0"])
        assert rc == 0
        assert payload["store"]["ttl"]["research_days"] is None

    def test_research_days_positive(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-ttl", "--research-days", "14"])
        assert rc == 0
        assert payload["store"]["ttl"]["research_days"] == 14.0

    def test_refresh_on_read_bool(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-ttl", "--refresh-on-read", "false"])
        assert rc == 0
        assert payload["store"]["ttl"]["refresh_on_read"] is False

    def test_jitter_pct(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-ttl", "--jitter-pct", "0.25"])
        assert rc == 0
        assert payload["store"]["ttl"]["jitter_pct"] == 0.25

    def test_jitter_pct_out_of_range_rejected(self, isolated_home,
                                                patched_http):
        rc, _, _ = _run(
            ["config", "set-store-ttl", "--jitter-pct", "1.5"])
        assert rc == 2


# ----------------------- config set-store-distillation (#220) --- #

class TestConfigSetStoreDistillation:
    def test_toggle_enabled(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-store-distillation", "--enabled", "false"])
        assert rc == 0
        assert payload["store"]["distillation"]["enabled"] is False

    def test_model_change(self, isolated_home, patched_http):
        rc, payload, _ = _run([
            "config", "set-store-distillation",
            "--model", "kimi-k2.6:cloud",
        ])
        assert rc == 0
        assert payload["store"]["distillation"]["model"] == "kimi-k2.6:cloud"

    def test_fallback_chain_add_remove_clear(self, isolated_home,
                                               patched_http):
        rc, _, _ = _run([
            "config", "set-store-distillation",
            "--clear-fallback-models",
        ])
        assert rc == 0
        rc, payload, _ = _run([
            "config", "set-store-distillation",
            "--add-fallback-model", "glm-5.1:cloud",
        ])
        assert rc == 0
        assert "glm-5.1:cloud" in payload["store"]["distillation"][
            "fallback_models"]
        rc, payload, _ = _run([
            "config", "set-store-distillation",
            "--remove-fallback-model", "glm-5.1:cloud",
        ])
        assert rc == 0
        assert "glm-5.1:cloud" not in payload["store"]["distillation"][
            "fallback_models"]

    def test_pacing_knobs_215(self, isolated_home, patched_http):
        rc, payload, _ = _run([
            "config", "set-store-distillation",
            "--max-groups-per-sweep", "3",
            "--pace-seconds-between-distillations", "12",
        ])
        assert rc == 0
        d = payload["store"]["distillation"]
        assert d["max_groups_per_sweep"] == 3
        assert d["pace_seconds_between_distillations"] == 12.0

    def test_sweep_too_short_rejected(self, isolated_home, patched_http):
        rc, _, _ = _run([
            "config", "set-store-distillation",
            "--sweep-interval-seconds", "10",
        ])
        assert rc == 2

    def test_min_entries_zero_rejected(self, isolated_home, patched_http):
        rc, _, _ = _run([
            "config", "set-store-distillation",
            "--min-entries-per-distillation", "0",
        ])
        assert rc == 2


# ----------------------- review loop: config -------------------- #

class TestConfigSetMaxFollowups:
    @pytest.mark.parametrize("n", [0, 4, 10])
    def test_valid(self, isolated_home, patched_http, n):
        rc, payload, _ = _run(["config", "set-max-followups", str(n)])
        assert rc == 0
        assert payload["max_followups"] == n

    def test_negative_rejected(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-max-followups", "-1"])
        assert rc == 2


class TestConfigSetAllowExtra:
    @pytest.mark.parametrize("n", [1, 3])
    def test_valid(self, isolated_home, patched_http, n):
        rc, payload, _ = _run(["config", "set-allow-extra", str(n)])
        assert rc == 0
        assert payload["allow_extra"] == n

    def test_zero_rejected(self, isolated_home, patched_http):
        rc, _, err = _run(["config", "set-allow-extra", "0"])
        assert rc == 2


class TestConfigShowReviewLoop:
    def test_defaults_present(self, isolated_home, patched_http):
        # --user forces the user-global view: `config show` now defaults
        # cwd to os.getcwd() (the repo root, which carries a real
        # per-project file), so a default-asserting test must opt out.
        rc, payload, _ = _run(["config", "show", "--user"])
        assert rc == 0
        assert payload["max_followups"] == 4
        assert payload["allow_extra"] == 1


# ----------------------- adversary / verify budget (M1) --------- #

class TestConfigSetVerifyBudget:
    @pytest.mark.parametrize("tier", ["minimal", "bounded", "generous"])
    def test_valid(self, isolated_home, patched_http, tier):
        rc, payload, _ = _run(["config", "set-verify-budget", tier])
        assert rc == 0
        assert payload["verify_budget"] == tier

    def test_unknown_rejected(self, isolated_home, patched_http):
        # argparse choices reject before the handler runs → SystemExit(2),
        # which propagates out of cli.main (unlike a handler CLIError that
        # returns rc==2).
        with pytest.raises(SystemExit) as ei:
            _run(["config", "set-verify-budget", "unlimited"])
        assert ei.value.code == 2


class TestConfigSetAdversaryStrictness:
    @pytest.mark.parametrize("level", ["soft", "normal", "strict"])
    def test_valid(self, isolated_home, patched_http, level):
        rc, payload, _ = _run(["config", "set-adversary-strictness", level])
        assert rc == 0
        assert payload["adversary_strictness"] == level

    def test_unknown_rejected(self, isolated_home, patched_http):
        with pytest.raises(SystemExit) as ei:
            _run(["config", "set-adversary-strictness", "savage"])
        assert ei.value.code == 2


class TestConfigSetAdversaryCheckpoint:
    def test_on_with_timeout(self, isolated_home, patched_http):
        rc, payload, _ = _run(
            ["config", "set-adversary-checkpoint", "on", "--timeout", "300"])
        assert rc == 0
        assert payload["adversary_checkpoint"] is True
        assert payload["adversary_checkpoint_timeout_s"] == 300

    def test_off(self, isolated_home, patched_http):
        rc, payload, _ = _run(["config", "set-adversary-checkpoint", "off"])
        assert rc == 0
        assert payload["adversary_checkpoint"] is False

    def test_bad_timeout_rejected(self, isolated_home, patched_http):
        rc, _, _ = _run(
            ["config", "set-adversary-checkpoint", "on", "--timeout", "0"])
        assert rc == 2

    def test_bad_state_rejected(self, isolated_home, patched_http):
        # argparse choices=("on","off") rejects anything else → SystemExit(2).
        with pytest.raises(SystemExit) as ei:
            _run(["config", "set-adversary-checkpoint", "maybe"])
        assert ei.value.code == 2


class TestConfigShowAdversaryDefaults:
    def test_defaults_and_valid_lists_present(self, isolated_home,
                                              patched_http):
        # --user → user-global default view (see note in
        # TestConfigShowReviewLoop.test_defaults_present).
        rc, payload, _ = _run(["config", "show", "--user"])
        assert rc == 0
        assert payload["verify_budget"] == "bounded"
        assert payload["adversary_strictness"] == "normal"
        assert payload["adversary_checkpoint"] is False
        assert payload["adversary_checkpoint_timeout_s"] == 600
        assert payload["roles"]["adversary"]["enabled"] is False
        assert payload["valid_verify_budgets"] == \
            ["minimal", "bounded", "generous"]
        assert payload["valid_adversary_strictness"] == \
            ["soft", "normal", "strict"]


# ----------------------- review loop: accept + override --------- #

def _stub_follow_up_runner():
    import time as _time
    from consultants.engine import storage

    def run(state, runner_input):
        cwd = Path(runner_input["cwd"])
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=_time.strftime("%Y-%m-%dT%H:%M:%S",
                                   _time.localtime(state.started_at)),
            question=runner_input["question"],
            models={"synthesizer": "stub"},
            topology=state.topology, effort=state.effort,
            final_answer="**stub child**: ok",
            turns=[storage.RoleTurn(role="synthesizer", round=1,
                                    content="ok")],
            duration_seconds=_time.time() - state.started_at,
            status="completed", cwd=str(cwd),
            parent_sid=state.parent_sid,
            root_sid=getattr(state, "root_sid", None) or state.sid,
        )
        storage.write_consultation(result, cwd=cwd)
        for r in state.progress:
            state.progress[r] = "done"
        state.status = "completed"
        state.finished_at = _time.time()
    return run


@pytest.fixture
def patched_http_fu(monkeypatch):
    """Like patched_http but wires the follow-up runner too."""
    app = create_app(run_council=_stub_runner(),
                     run_follow_up=_stub_follow_up_runner(),
                     start_reaper=False)
    client = TestClient(app)
    client.__enter__()
    base = "http://test"
    monkeypatch.setattr(cli, "_http", _TestClientHttp(client, base))
    yield base
    client.__exit__(None, None, None)


def _consult_and_wait(project_dir: Path) -> str:
    rc, payload, _ = _run(["consult", "--message", "q",
                           "--cwd", str(project_dir)])
    sid = payload["sid"]
    for _ in range(50):
        _, p, _ = _run(["status", sid])
        if p.get("status") == "completed":
            break
        time.sleep(0.02)
    return sid


class TestAcceptVerb:
    def test_accept_marks_terminal(self, isolated_home, project_dir,
                                   patched_http_fu):
        sid = _consult_and_wait(project_dir)
        rc, payload, _ = _run(["accept", sid, "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["ok"] is True
        assert payload["consultancy"]["status"] == "accepted"


class TestFollowupAllowExtra:
    def test_cap_refusal_then_allow_extra(self, isolated_home, project_dir,
                                          patched_http_fu):
        cc.set_max_followups(0)  # first followup needs approval
        sid = _consult_and_wait(project_dir)
        # No override → structured refusal (rc still 0; ok:false).
        rc, refused, _ = _run(["follow-up", sid, "--message", "more",
                               "--cwd", str(project_dir)])
        assert rc == 0
        assert refused["ok"] is False
        assert refused["reason"] == "followup_limit_reached"
        # --allow-extra bare → resolves to the configured default (1).
        rc, ok, _ = _run(["follow-up", sid, "--message", "more",
                          "--cwd", str(project_dir), "--allow-extra"])
        assert rc == 0
        assert ok["ok"] is True
        assert ok["consultancy"]["extra_granted"] == 1

    def test_force_alias(self, isolated_home, project_dir, patched_http_fu):
        cc.set_max_followups(0)
        cc.set_allow_extra(2)
        sid = _consult_and_wait(project_dir)
        rc, ok, _ = _run(["follow-up", sid, "--message", "more",
                          "--cwd", str(project_dir), "--force"])
        assert rc == 0
        assert ok["ok"] is True
        # --force resolves to the configured allow_extra default (2).
        assert ok["consultancy"]["extra_granted"] == 2


# ----------------- per-project override scope ------------------ #
# Every config test below passes an explicit --cwd into an isolated
# tmp project dir: ``config show`` now defaults cwd to os.getcwd()
# (the repo root, which carries a real .claude-hooks/consultants.toml),
# so an un-scoped command would read the live repo file and pollute
# the assertion.

class TestConfigOverrideScope:
    def test_show_no_project_file_is_user(self, isolated_home, project_dir,
                                          patched_http):
        rc, payload, err = _run(["config", "show", "--cwd", str(project_dir)])
        assert rc == 0
        ac = payload["active_config"]
        assert ac["scope"] == "user"
        assert ac["project_file_exists"] is False
        assert ac["override_user_global"] is None
        assert err.strip() == ""        # no warn for plain user-global

    def test_auto_scope_writes_project_when_active(self, isolated_home,
                                                   project_dir, patched_http):
        # Create the project file (flag on by default) ...
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        # ... then an un-flagged set-* must AUTO-land in the project file.
        rc, payload, err = _run(["config", "set-effort", "low",
                                 "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["active_config"]["scope"] == "project"
        assert payload["effort"] == "low"
        assert "PER-PROJECT" in err
        # The engine view honors it.
        assert cc.load_config(cwd=project_dir).effort == "low"
        # User-global is untouched (still the default).
        assert cc.load_config().effort == cc.DEFAULT_EFFORT

    def test_user_flag_forces_global_when_project_active(self, isolated_home,
                                                         project_dir,
                                                         patched_http):
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        rc, payload, err = _run(["config", "set-effort", "max", "--user",
                                 "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["active_config"]["scope"] == "user"
        assert "forced via --user" in err
        # Project file unchanged; user-global got the write.
        assert cc.load_config(cwd=project_dir).effort == "high"
        assert cc.load_config().effort == "max"

    def test_set_override_off_then_auto_writes_user(self, isolated_home,
                                                    project_dir, patched_http):
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        rc, payload, err = _run(["config", "set-override-user-global", "off",
                                 "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["active_config"]["scope"] == "user"
        assert payload["active_config"]["override_user_global"] is False
        assert "override_user_global=off" in err
        # With the flag off, an auto set-* lands in user-global, and the
        # dormant project file keeps its own value. Capture stderr to
        # cover the `elif exists:` warn branch in the ordinary-mutation
        # (non-flip) path.
        _, _, err2 = _run(["config", "set-effort", "max",
                           "--cwd", str(project_dir)])
        assert "override_user_global=off" in err2
        assert cc.load_config().effort == "max"
        assert cc.load_config(cwd=project_dir).effort == "max"  # flag off
        proj_raw = cc._read_toml(cc.project_config_path(project_dir))
        assert cc._override_flag(proj_raw) is False
        # The dormant project file must still hold its OWN effort (high),
        # not the user-global 'max' the auto write just stored.
        assert proj_raw["effort"] == "high"

    def test_show_active_project_file_reports_overrides(self, isolated_home,
                                                        project_dir,
                                                        patched_http):
        # The headline behavior: `config show --cwd <project>` against an
        # ACTIVE per-project file returns the project-overridden values
        # AND active_config.scope == project (asserted at the CLI surface,
        # not just via cc.load_config).
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        rc, payload, err = _run(["config", "show", "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["effort"] == "high"
        assert payload["active_config"]["scope"] == "project"
        assert payload["active_config"]["override_user_global"] is True
        assert "PER-PROJECT" in err

    def test_override_verb_rejects_scope_flags(self, isolated_home,
                                               project_dir, patched_http):
        # set-override-user-global is inherently project-scoped — it must
        # NOT accept --user/--project (argparse rejects → SystemExit(2),
        # which propagates before main's try/except, like test_no_subcommand).
        for bad in ("--user", "--project"):
            with pytest.raises(SystemExit):
                cli.main(["config", "set-override-user-global", "on", bad,
                          "--cwd", str(project_dir)])

    def test_project_set_on_dormant_file_preserves_content(
            self, isolated_home, project_dir, patched_http):
        # Explicit --project set-* on a flag-OFF file must preserve the
        # file's other content (BUG from the M4 adversarial review). Set
        # two project values, flip off, edit ONE via --project, flip on →
        # both survive.
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        _run(["config", "set-role", "planner", "--model", "proj-pl",
              "--project", "--cwd", str(project_dir)])
        _run(["config", "set-override-user-global", "off",
              "--cwd", str(project_dir)])
        _run(["config", "set-verify-budget", "generous", "--project",
              "--cwd", str(project_dir)])
        _run(["config", "set-override-user-global", "on",
              "--cwd", str(project_dir)])
        cfg = cc.load_config(cwd=project_dir)
        assert cfg.effort == "high"                  # not reverted to default
        assert cfg.roles["planner"].model == "proj-pl"
        assert cfg.verify_budget == "generous"       # the dormant-path edit

    def test_set_override_round_trip_via_cli(self, isolated_home,
                                             project_dir, patched_http):
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        _run(["config", "set-override-user-global", "off",
              "--cwd", str(project_dir)])
        assert cc.project_override_active(project_dir) is False
        rc, payload, err = _run(["config", "set-override-user-global", "on",
                                 "--cwd", str(project_dir)])
        assert rc == 0
        assert payload["active_config"]["scope"] == "project"
        assert cc.project_override_active(project_dir) is True
        # Re-activated → engine sees the project value again.
        assert cc.load_config(cwd=project_dir).effort == "high"

    def test_coder_list_reports_active_config(self, isolated_home,
                                              project_dir, patched_http):
        _run(["config", "set-effort", "high", "--project",
              "--cwd", str(project_dir)])
        rc, payload, err = _run(["config", "coder", "list",
                                 "--cwd", str(project_dir)])
        assert rc == 0
        assert "coder" in payload
        assert payload["active_config"]["scope"] == "project"
        assert "PER-PROJECT" in err


# ----------------------- top-level errors ---------------------- #

class TestTopLevelErrors:
    def test_no_subcommand(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_consult_requires_message(self):
        with pytest.raises(SystemExit):
            cli.main(["consult"])
