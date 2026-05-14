"""Tests for the v1.4 ``_setup_sqlite_vec_mcp`` installer flow.

sqlite_vec previously had **zero** installer code (the dialog
existed only for pgvector); v1.4 closes that gap. The embedder
dialog itself is exercised in
``tests/test_install_embedding_engine.py`` — these tests cover the
sqlite_vec-specific top-level shape:

- enable/skip prompt + non-interactive default
- db_path prompt with sensible default and parent-mkdir
- existing-enabled path (preserve config, no surprises on re-run)
- the embedder dialog is delegated only on a first wire-up
- a clean breadcrumb when the Python ``sqlite_vec`` package is absent

The interactive dialog is driven by a scripted ``input()``
replacement; helpers patched on the install module so no real
network / disk traffic happens beyond the tmp_path filesystem.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _scripted_input(answers):
    """Build a replacement for builtins.input that pops scripted
    answers; raises on overrun so a test omission shows up cleanly
    rather than as a hang."""
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


@pytest.fixture(autouse=True)
def stub_ollama_helpers():
    """No real /api/tags traffic when the embedder dialog fires."""
    with patch.object(install, "_ollama_model_present", return_value=True):
        with patch.object(install, "_ollama_pull", return_value=True):
            yield


@pytest.fixture(autouse=True)
def stub_composite_present():
    """Pretend the composite is on disk so the embedder dialog
    doesn't try to download from GitHub at test time."""
    with patch.object(Path, "is_file", return_value=True):
        yield


@pytest.fixture(autouse=True)
def stub_gpu_probe():
    """Stable GPU answer for tests that hit the llamafile sub-dialog."""
    with patch("claude_hooks.gpu_probe.probe", return_value={
            "vendor": "nvidia", "total_mb": 24000, "free_mb": 22000, "raw": "x",
    }):
        yield


# --------------------------------------------------------------------- #
# Skip paths
# --------------------------------------------------------------------- #

class TestSkipPaths:
    def test_non_interactive_skips_when_not_enabled(self, capsys):
        cfg: dict = {}
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=True, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "skipping sqlite_vec" in out
        # No provider config side-effects.
        assert cfg.get("providers") is None or "sqlite_vec" not in cfg.get("providers", {})

    def test_interactive_n_answer_skips(self, monkeypatch, capsys):
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input(["n"]))
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=False, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "Skipped." in out
        assert cfg.get("providers") is None or "sqlite_vec" not in cfg.get("providers", {})


# --------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------- #

class TestEnable:
    def test_interactive_default_path(self, monkeypatch, tmp_path):
        """User enables, accepts default db_path, walks the embedder
        dialog with Ollama + llamafile-fallback default."""
        db = tmp_path / "sub" / "memory.db"
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",                # enable
            str(db),            # db_path
            "y",                # use Ollama? yes
            "",                 # URL default
            "",                 # model default
            "",                 # num_ctx default
            "y",                # llamafile fallback? yes
            "",                 # llamafile defaults
            "cpu",              # CPU mode
        ]))
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=False, dry_run=False,
        )
        sv = cfg["providers"]["sqlite_vec"]
        assert sv["enabled"] is True
        assert sv["db_path"] == str(db)
        assert sv["table"] == "memory"
        assert sv["recall_k"] == 5
        assert sv["embedder"] == "composite"
        # Embedding block written too (composite picked llamafile fallback).
        assert cfg["embedding"]["enabled"] is True
        # Parent dir got created.
        assert db.parent.exists()

    def test_dry_run_does_not_create_parent(self, monkeypatch, tmp_path, capsys):
        db = tmp_path / "nope-dont-make-me" / "memory.db"
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",
            str(db),
            "n",    # use Ollama? no
            "n",    # use OpenAI? no
            "",     # llamafile defaults
            "cpu",  # CPU mode
        ]))
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=False, dry_run=True,
        )
        out = capsys.readouterr().out
        # We always claim the mkdir under dry_run; never actually do it.
        assert "[dry-run] Would mkdir" in out
        assert not db.parent.exists()
        # Config still gets written even in dry-run (consistent with
        # _setup_pgvector_mcp's behavior — actual save happens later).
        assert cfg["providers"]["sqlite_vec"]["enabled"] is True

    def test_non_interactive_with_existing_enabled(self, monkeypatch, tmp_path):
        """When sqlite_vec is already enabled, --non-interactive
        preserves the existing config and re-uses the existing
        embedder choice (so the embedder dialog is NOT re-fired)."""
        existing_db = str(tmp_path / "preset.db")
        (tmp_path).mkdir(exist_ok=True)
        cfg: dict = {"providers": {"sqlite_vec": {
            "enabled": True,
            "db_path": existing_db,
            "embedder": "ollama",
            "embedder_options": {
                "url": "http://localhost:11434/api/embeddings",
                "model": "qwen3-embedding:0.6b",
            },
        }}}
        # We expect ZERO input() calls in this path.
        monkeypatch.setattr("builtins.input", _scripted_input([]))
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=True, dry_run=False,
        )
        sv = cfg["providers"]["sqlite_vec"]
        assert sv["enabled"] is True
        assert sv["db_path"] == existing_db
        # Embedder kept as configured — _setup_embedding_engine
        # NOT re-run.
        assert sv["embedder"] == "ollama"
        assert sv["embedder_options"]["model"] == "qwen3-embedding:0.6b"

    def test_interactive_keeps_existing_embedder(self, monkeypatch, tmp_path):
        """Interactive re-run with embedder already configured: the
        skipping is by the `if not sv.get('embedder')` guard, not by
        the user — so the embedder dialog never starts. The user
        still has to answer the top-level enable + db_path prompts."""
        db = tmp_path / "memory.db"
        cfg: dict = {"providers": {"sqlite_vec": {
            "enabled": True,
            "db_path": str(db),
            "embedder": "llamafile",
            "embedder_options": {"url": "http://127.0.0.1:38092/embedding"},
        }}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "",          # accept default Y (already enabled)
            str(db),     # accept db_path
            # No further inputs — embedder dialog is skipped.
        ]))
        install._setup_sqlite_vec_mcp(
            cfg, non_interactive=False, dry_run=False,
        )
        sv = cfg["providers"]["sqlite_vec"]
        assert sv["embedder"] == "llamafile"
        assert sv["embedder_options"]["url"] == "http://127.0.0.1:38092/embedding"

    def test_breadcrumb_when_sqlite_vec_dep_missing(
            self, monkeypatch, tmp_path, capsys,
    ):
        db = tmp_path / "memory.db"
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",
            str(db),
            "n",   # use Ollama? no
            "n",   # use OpenAI? no
            "",    # llamafile defaults
            "cpu", # CPU mode
        ]))
        with patch.object(install, "_sqlite_vec_extension_available",
                          return_value=False):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "pip install sqlite-vec" in out

    def test_no_breadcrumb_when_dep_present(
            self, monkeypatch, tmp_path, capsys,
    ):
        db = tmp_path / "memory.db"
        cfg: dict = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",
            str(db),
            "n",
            "n",
            "",
            "cpu",
        ]))
        with patch.object(install, "_sqlite_vec_extension_available",
                          return_value=True):
            install._setup_sqlite_vec_mcp(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "pip install sqlite-vec" not in out


# --------------------------------------------------------------------- #
# main() wiring
# --------------------------------------------------------------------- #

class TestMainWiring:
    def test_main_calls_setup_sqlite_vec(self):
        """Regression guard: main() must call _setup_sqlite_vec_mcp
        right after _setup_pgvector_mcp. Reading the source rather
        than running main() because main() does a great many other
        things and would need a deep mock surface."""
        src = (REPO / "install.py").read_text(encoding="utf-8")
        i_pg = src.find("_setup_pgvector_mcp(\n        cfg,")
        i_sv = src.find("_setup_sqlite_vec_mcp(\n        cfg,")
        i_proxy = src.find("_setup_proxy_orchestrator(\n        cfg,")
        assert i_pg > 0 and i_sv > 0 and i_proxy > 0
        assert i_pg < i_sv < i_proxy
