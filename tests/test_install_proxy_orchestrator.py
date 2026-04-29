"""Tests for the proxy orchestrator and the settings.json env-var helper.

Covers ``install._setup_proxy_orchestrator`` (local vs remote-URL
choice) and ``install._set_settings_env_vars`` (idempotent merge into
the env block).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# ===================================================================== #
# _set_settings_env_vars
# ===================================================================== #
class TestSetSettingsEnvVars:
    def test_creates_file_when_missing(self, tmp_path):
        settings_path = tmp_path / "claude" / "settings.json"
        assert not settings_path.exists()

        install._set_settings_env_vars(
            settings_path,
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:38080"},
        )

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:38080"

    def test_merges_into_existing_env(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "env": {"OTHER": "1"},
            "permissions": {"allow": ["Bash"]},
        }), encoding="utf-8")

        install._set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": "http://lan:38080"},
        )

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["OTHER"] == "1"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://lan:38080"
        # Sibling keys preserved.
        assert data["permissions"]["allow"] == ["Bash"]

    def test_idempotent_when_already_set(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "http://x:1"},
        }), encoding="utf-8")
        mtime_before = settings_path.stat().st_mtime_ns

        install._set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": "http://x:1"},
        )

        # No write -- avoids racing CC's own settings rewrites.
        assert settings_path.stat().st_mtime_ns == mtime_before

    def test_creates_backup_before_overwriting(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "http://old:1"},
        }), encoding="utf-8")

        install._set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": "http://new:2"},
        )

        # A *.bak-* file should exist next to the original.
        baks = list(tmp_path.glob("settings.json.bak-*"))
        assert len(baks) == 1
        old = json.loads(baks[0].read_text(encoding="utf-8"))
        assert old["env"]["ANTHROPIC_BASE_URL"] == "http://old:1"

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        settings_path = tmp_path / "settings.json"
        install._set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": "http://x"},
            dry_run=True,
        )
        assert not settings_path.exists()
        assert "[dry-run]" in capsys.readouterr().out

    def test_repairs_non_dict_env(self, tmp_path):
        # Some CC settings.json variants put weird stuff in ``env``;
        # reset to a dict rather than failing.
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": ["bogus"]}), encoding="utf-8")

        install._set_settings_env_vars(
            settings_path, {"ANTHROPIC_BASE_URL": "http://x:1"},
        )
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"] == {"ANTHROPIC_BASE_URL": "http://x:1"}


# ===================================================================== #
# _setup_proxy_orchestrator
# ===================================================================== #
@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / "settings.json"


class TestOrchestratorSkip:
    def test_non_interactive_is_no_op(self, settings_path, capsys):
        cfg = {"proxy": {"enabled": True}}  # pre-existing state
        install._setup_proxy_orchestrator(
            cfg, settings_path,
            non_interactive=True, dry_run=False,
        )
        # Nothing changed (state preserved).
        assert cfg["proxy"]["enabled"] is True
        assert not settings_path.exists()
        # No header printed in non-interactive mode -- silent skip.
        assert "claude-hooks API proxy" not in capsys.readouterr().out

    def test_user_says_no_when_currently_disabled(
        self, settings_path, capsys,
    ):
        cfg = {}
        with patch("builtins.input", side_effect=["n"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is False
        assert not settings_path.exists()

    def test_user_says_no_preserves_existing_enabled_state(
        self, settings_path,
    ):
        # If the user already had it on and answers "n" by accident,
        # don't silently flip to off -- prefer preserving existing state.
        cfg = {"proxy": {"enabled": True, "listen_port": 38080}}
        with patch("builtins.input", side_effect=["n"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is True


class TestOrchestratorLocal:
    def test_local_choice_enables_and_writes_base_url(
        self, settings_path, capsys,
    ):
        cfg = {"proxy": {"listen_host": "127.0.0.1", "listen_port": 38080}}
        # Inputs: yes, choice 1, yes (set base URL)
        with patch("builtins.input", side_effect=["y", "1", "y"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is True
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:38080"

    def test_local_choice_skip_base_url(self, settings_path, capsys):
        cfg = {"proxy": {"listen_port": 38090}}
        with patch("builtins.input", side_effect=["y", "1", "n"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is True
        # No settings.json written.
        assert not settings_path.exists()
        assert "Set it manually later" in capsys.readouterr().out

    def test_local_choice_translates_zero_host_to_loopback(
        self, settings_path,
    ):
        cfg = {"proxy": {"listen_host": "0.0.0.0", "listen_port": 38080}}
        with patch("builtins.input", side_effect=["y", "1", "y"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        # Client should always use loopback when listen_host is 0.0.0.0,
        # not 0.0.0.0 itself.
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:38080"


class TestOrchestratorRemote:
    def test_remote_choice_writes_base_url_and_disables_local(
        self, settings_path, capsys,
    ):
        cfg = {"proxy": {"enabled": True}}  # was on, switching to remote
        with patch(
            "builtins.input",
            side_effect=["y", "2", "http://192.168.178.2:38080"],
        ):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        # Local install must NOT run.
        assert cfg["proxy"]["enabled"] is False
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://192.168.178.2:38080"
        assert "Local proxy NOT installed" in capsys.readouterr().out

    def test_remote_choice_strips_trailing_slash(self, settings_path):
        cfg = {}
        with patch(
            "builtins.input",
            side_effect=["y", "2", "http://lan:38080/"],
        ):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://lan:38080"

    def test_remote_choice_rejects_bare_hostname(self, settings_path):
        cfg = {}
        # First URL bad, second URL good.
        with patch(
            "builtins.input",
            side_effect=["y", "2", "lan:38080", "http://lan:38080"],
        ):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://lan:38080"


class TestOrchestratorBadInputs:
    def test_invalid_choice_re_prompts(self, settings_path):
        cfg = {}
        # Inputs: yes, "3" (bad), "1", "n" (skip base url)
        with patch("builtins.input", side_effect=["y", "3", "1", "n"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is True

    def test_default_choice_is_local(self, settings_path):
        # Empty answer to the choice prompt should default to "1"
        # (local install).
        cfg = {}
        with patch("builtins.input", side_effect=["y", "", "n"]):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        assert cfg["proxy"]["enabled"] is True
