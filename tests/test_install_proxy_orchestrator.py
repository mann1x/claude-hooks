"""Tests for the settings.json env-var helper and the non-interactive
fast-path of the proxy orchestrator.

The proxy dialog itself was redesigned in v1.6.1 (single
"Use the API proxy?" question split into "Install locally?" +
"Use the API proxy?" with state-aware labels). Full dialog
coverage lives in ``tests/test_install_proxy_dialog.py``; the
legacy ``[1/2]`` two-mode tests that used to live here have been
removed because the shape they checked no longer exists.

What remains here:

- ``_set_settings_env_vars`` — pure helper, unchanged by v1.6.1.
- ``_setup_proxy_orchestrator`` non-interactive skip — verifies the
  installer never prompts when ``--non-interactive`` is set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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

    # All other TestOrchestrator* classes that used to live here
    # exercised the v1.x ``[1/2]`` two-mode prompt. v1.6.1 replaced
    # that shape entirely (see ``tests/test_install_proxy_dialog.py``
    # for the new coverage). The non-interactive test above is the
    # only orchestrator-level assertion that still applies — the
    # dialog itself is exercised in the v1.6.1 suite.
