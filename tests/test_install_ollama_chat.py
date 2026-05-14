"""Tests for the v1.4 ``_setup_ollama_chat`` installer flow.

Covers the HyDE + reflect + consolidate dialog. Until v1.4 those
three sections had **no** interactive prompts (hard-coded defaults
in config.py); this dialog closes that gap.

The dialog is driven by ``input()``; we replace it with a scripted
iterator and patch ``urllib.request.urlopen`` so the /api/tags
validation step never touches the network.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _scripted_input(answers):
    """Replacement for builtins.input. Raises on overrun so a test
    omission shows up as a clean failure, not a hang."""
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


class _FakeTagsResp:
    """Pretends to be an /api/tags response with at least one model."""

    def __init__(self, models=("gemma4:e2b", "qwen3-embedding:0.6b")):
        body = json.dumps({
            "models": [{"name": m} for m in models]
        }).encode("utf-8")
        self._buf = io.BytesIO(body)

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()


@pytest.fixture(autouse=True)
def stub_tags():
    """Default: /api/tags always succeeds. Individual tests can
    override to exercise the failure path."""
    with patch("urllib.request.urlopen", return_value=_FakeTagsResp()):
        yield


# --------------------------------------------------------------------- #
# Skip paths
# --------------------------------------------------------------------- #

class TestSkipPaths:
    def test_non_interactive_no_existing_config_skips(self, capsys):
        cfg: dict = {}
        install._setup_ollama_chat(
            cfg, non_interactive=True, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "skipping" in out.lower()
        # No keys written.
        assert "hooks" not in cfg
        assert "reflect" not in cfg
        assert "consolidate" not in cfg

    def test_interactive_n_skips(self, monkeypatch, capsys):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input(["n"]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "Skipped" in out
        assert "hooks" not in cfg

    def test_validation_failure_with_decline(self, monkeypatch, capsys):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",                              # use Ollama? yes
            "http://nope:9999/api/generate",  # bad URL
            "n",                              # keep URL anyway? no
        ]))
        with patch("urllib.request.urlopen",
                   side_effect=ConnectionRefusedError("nope")):
            install._setup_ollama_chat(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "Skipped Ollama chat setup" in out
        # Nothing written.
        assert "hooks" not in cfg


# --------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------- #

class TestEnableSharedSkills:
    def test_interactive_default_shared_skills(self, monkeypatch):
        """Most common flow: accept defaults everywhere, one model
        for HyDE, same model for reflect + consolidate."""
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",      # use Ollama? yes
            "",       # URL default
            "",       # HyDE enabled default Y
            "",       # HyDE model default
            "",       # HyDE fallback default (= model)
            "",       # HyDE ctx default
            "",       # share skills? Y
            "",       # skills model default (= HyDE model)
            "",       # skills ctx default
        ]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        ups = cfg["hooks"]["user_prompt_submit"]
        assert ups["hyde_url"] == "http://localhost:11434/api/generate"
        assert ups["hyde_enabled"] is True
        assert ups["hyde_model"] == "gemma4:e2b"
        assert ups["hyde_fallback_model"] == "gemma4:e2b"
        assert ups["hyde_num_ctx"] == 16384

        # Skills share the HyDE model + URL.
        assert cfg["reflect"]["ollama_url"] == ups["hyde_url"]
        assert cfg["reflect"]["ollama_model"] == "gemma4:e2b"
        assert cfg["reflect"]["num_ctx"] == 16384
        assert cfg["consolidate"]["ollama_url"] == ups["hyde_url"]
        assert cfg["consolidate"]["ollama_model"] == "gemma4:e2b"

    def test_interactive_custom_model(self, monkeypatch):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",                                     # use Ollama? yes
            "http://192.168.1.5:11434/api/generate", # URL
            "",                                      # HyDE enabled default
            "gemma4:e4b-32000",                      # HyDE model
            "gemma4:e4b-32000",                      # HyDE fallback (same)
            "32768",                                 # HyDE ctx
            "",                                      # share skills? Y
            "gemma4:e4b-32000",                      # skills model
            "32768",                                 # skills ctx
        ]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        ups = cfg["hooks"]["user_prompt_submit"]
        assert ups["hyde_url"] == "http://192.168.1.5:11434/api/generate"
        assert ups["hyde_model"] == "gemma4:e4b-32000"
        assert ups["hyde_num_ctx"] == 32768
        assert cfg["reflect"]["num_ctx"] == 32768
        assert cfg["consolidate"]["ollama_model"] == "gemma4:e4b-32000"

    def test_hyde_disabled_still_writes_block(self, monkeypatch):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",
            "",
            "n",      # HyDE enabled? no
            "",       # model default
            "",       # fallback default
            "",       # ctx default
            "",       # share skills? Y
            "",       # skills model default
            "",       # skills ctx default
        ]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        assert cfg["hooks"]["user_prompt_submit"]["hyde_enabled"] is False
        # Other settings still persisted so toggling back on is one-line edit.
        assert cfg["hooks"]["user_prompt_submit"]["hyde_model"]


class TestEnableSeparateSkills:
    def test_separate_models_for_reflect_consolidate(self, monkeypatch):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y", "",                # use Ollama, URL default
            "",                     # HyDE enabled default
            "gemma4:e2b",           # HyDE model
            "gemma4:e4b",           # HyDE fallback
            "16384",                # HyDE ctx
            "n",                    # share skills? no
            "gemma4:e4b-32000",     # reflect model
            "32768",                # reflect ctx
            "gemma4:e2b",           # consolidate model
            "8192",                 # consolidate ctx
        ]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        assert cfg["reflect"]["ollama_model"] == "gemma4:e4b-32000"
        assert cfg["reflect"]["num_ctx"] == 32768
        assert cfg["consolidate"]["ollama_model"] == "gemma4:e2b"
        assert cfg["consolidate"]["num_ctx"] == 8192


# --------------------------------------------------------------------- #
# Re-run / idempotency
# --------------------------------------------------------------------- #

class TestExistingConfig:
    def test_non_interactive_keeps_existing(self, monkeypatch):
        cfg: dict = {
            "hooks": {"user_prompt_submit": {
                "hyde_url": "http://192.168.178.2:11433/api/generate",
                "hyde_enabled": True,
                "hyde_model": "gemma4:e4b-32000",
                "hyde_fallback_model": "gemma4:e4b-32000",
                "hyde_num_ctx": 32768,
            }},
            "reflect": {"ollama_model": "gemma4:e4b-32000", "num_ctx": 32768},
            "consolidate": {"ollama_model": "gemma4:e4b-32000", "num_ctx": 32768},
        }
        # No input() should be called.
        monkeypatch.setattr("builtins.input", _scripted_input([]))
        install._setup_ollama_chat(
            cfg, non_interactive=True, dry_run=False,
        )
        ups = cfg["hooks"]["user_prompt_submit"]
        # Existing values preserved (and chat_url rewrites consistent
        # across the three blocks).
        assert ups["hyde_model"] == "gemma4:e4b-32000"
        assert ups["hyde_num_ctx"] == 32768
        assert cfg["reflect"]["ollama_url"] == ups["hyde_url"]
        assert cfg["consolidate"]["ollama_url"] == ups["hyde_url"]

    def test_interactive_existing_defaults_visible(self, monkeypatch):
        """Re-run with existing config: defaults visible in prompts;
        user accepts everything -> config unchanged."""
        cfg: dict = {
            "hooks": {"user_prompt_submit": {
                "hyde_url": "http://h:1/api/generate",
                "hyde_enabled": True,
                "hyde_model": "model-A",
                "hyde_fallback_model": "model-B",
                "hyde_num_ctx": 8192,
            }},
            "reflect": {"ollama_model": "model-A", "num_ctx": 4096},
            "consolidate": {"ollama_model": "model-A", "num_ctx": 4096},
        }
        monkeypatch.setattr("builtins.input", _scripted_input([
            "",   # use Ollama -> default Y (because existing)
            "",   # URL default
            "",   # HyDE enabled default
            "",   # HyDE model
            "",   # fallback
            "",   # ctx
            "",   # share skills? Y
            "",   # skills model default (existing model-A)
            "",   # skills ctx default (existing 4096)
        ]))
        install._setup_ollama_chat(
            cfg, non_interactive=False, dry_run=False,
        )
        ups = cfg["hooks"]["user_prompt_submit"]
        assert ups["hyde_url"] == "http://h:1/api/generate"
        assert ups["hyde_model"] == "model-A"
        assert ups["hyde_fallback_model"] == "model-B"
        assert ups["hyde_num_ctx"] == 8192
        assert cfg["reflect"]["ollama_model"] == "model-A"
        assert cfg["reflect"]["num_ctx"] == 4096


# --------------------------------------------------------------------- #
# main() wiring
# --------------------------------------------------------------------- #

class TestMainWiring:
    def test_main_calls_chat_backends_before_pgvector(self):
        """Regression guard: _setup_chat_backends (v1.5+, was
        _setup_ollama_chat in v1.4) must run BEFORE _setup_pgvector_mcp
        so the chat URL is established before the embedder dialog
        uses it."""
        src = (REPO / "install.py").read_text(encoding="utf-8")
        i_chat = src.find("_setup_chat_backends(\n        cfg,")
        i_pg = src.find("_setup_pgvector_mcp(\n        cfg,")
        i_sv = src.find("_setup_sqlite_vec_mcp(\n        cfg,")
        assert i_chat > 0 and i_pg > 0 and i_sv > 0
        assert i_chat < i_pg < i_sv
