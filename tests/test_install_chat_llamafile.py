"""Tests for the v1.5 ``_setup_llamafile_chat_models`` installer flow.

Scripted-input coverage of the interactive sub-dialog that registers
a GGUF + label in the ``~/.claude/llamafile-models.json`` registry and
optionally writes ``hyde_model_ref`` / ``reflect.model_ref`` /
``consolidate.model_ref`` into the config.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _scripted_input(answers):
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


@pytest.fixture
def tmp_registry(monkeypatch):
    """Redirect the registry's DEFAULT_REGISTRY_PATH to a tmp file so
    tests never touch ~/.claude/llamafile-models.json."""
    with TemporaryDirectory() as d:
        reg_path = Path(d) / "llamafile-models.json"
        from claude_hooks import chat_model_registry as cmr
        monkeypatch.setattr(cmr, "DEFAULT_REGISTRY_PATH", reg_path)
        yield reg_path


@pytest.fixture
def fake_gguf():
    """Materialise a tmp file that passes the GGUF magic check."""
    with TemporaryDirectory() as d:
        p = Path(d) / "model.gguf"
        p.write_bytes(b"GGUF" + b"\x00" * 16)
        yield p


def test_skip_when_user_says_no(monkeypatch, tmp_registry):
    cfg: dict = {}
    monkeypatch.setattr("builtins.input", _scripted_input(["n"]))
    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )
    assert "hooks" not in cfg
    assert not tmp_registry.exists()


def test_non_interactive_skips(monkeypatch, tmp_registry):
    cfg: dict = {}
    # No scripted answers — must not call input() at all.
    install._setup_llamafile_chat_models(
        cfg, non_interactive=True, dry_run=False,
    )
    assert not tmp_registry.exists()


def test_dry_run_does_not_register(monkeypatch, tmp_registry):
    cfg: dict = {}
    monkeypatch.setattr("builtins.input", _scripted_input(["y"]))
    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=True,
    )
    assert not tmp_registry.exists()


def test_happy_path_with_wiring(monkeypatch, tmp_registry, fake_gguf):
    cfg: dict = {}
    answers = [
        "y",                   # Register a llamafile chat model now?
        str(fake_gguf),        # GGUF path
        "gemma-local",         # Label
        "8192",                # Context size
        "auto",                # Mode
        "",                    # Port (auto-allocate)
        "y",                   # Use for HyDE + reflect + consolidate?
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    # Registry written
    assert tmp_registry.exists()
    from claude_hooks.chat_model_registry import Registry
    spec = Registry(tmp_registry).get("gemma-local")
    assert spec.ctx_size == 8192
    assert spec.mode == "auto"
    assert 38093 <= spec.port <= 38099  # default range

    # Wiring applied to cfg
    ref = "llamafile://gemma-local"
    assert cfg["hooks"]["user_prompt_submit"]["hyde_model_ref"] == ref
    assert cfg["reflect"]["model_ref"] == ref
    assert cfg["consolidate"]["model_ref"] == ref


def test_happy_path_skip_wiring(monkeypatch, tmp_registry, fake_gguf):
    cfg: dict = {}
    answers = [
        "y", str(fake_gguf), "g", "", "", "",  # accept defaults
        "n",                                    # don't wire
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    from claude_hooks.chat_model_registry import Registry
    assert Registry(tmp_registry).exists("g")
    # No wiring
    assert "hooks" not in cfg


def test_explicit_port(monkeypatch, tmp_registry, fake_gguf):
    cfg: dict = {}
    answers = [
        "y", str(fake_gguf), "g", "", "", "38095", "n",
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    from claude_hooks.chat_model_registry import Registry
    assert Registry(tmp_registry).get("g").port == 38095


def test_invalid_port_falls_back_to_auto(monkeypatch, tmp_registry, fake_gguf):
    cfg: dict = {}
    answers = [
        "y", str(fake_gguf), "g", "", "", "not-a-number", "n",
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    from claude_hooks.chat_model_registry import Registry
    spec = Registry(tmp_registry).get("g")
    assert 38093 <= spec.port <= 38099


def test_bad_gguf_path_reprompts(monkeypatch, tmp_registry, fake_gguf):
    cfg: dict = {}
    answers = [
        "y",
        "/does/not/exist.gguf",  # rejected; reprompts
        str(fake_gguf),
        "g", "", "", "", "n",
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    from claude_hooks.chat_model_registry import Registry
    assert Registry(tmp_registry).exists("g")


def test_existing_label_offered_then_declined(
    monkeypatch, tmp_registry, fake_gguf,
):
    # Pre-seed registry with one entry
    from claude_hooks.chat_model_registry import Registry
    reg = Registry(tmp_registry)
    reg.add("preexisting", str(fake_gguf))

    cfg: dict = {}
    answers = [
        "y",   # Register a llamafile chat model now?
        "n",   # Add another? -> no, exit
    ]
    monkeypatch.setattr("builtins.input", _scripted_input(answers))

    install._setup_llamafile_chat_models(
        cfg, non_interactive=False, dry_run=False,
    )

    # Registry unchanged (still just the one entry)
    assert Registry(tmp_registry).list_labels() == ["preexisting"]


def test_setup_chat_backends_runs_both_halves(
    monkeypatch, tmp_registry, fake_gguf,
):
    """Top-level dispatcher should run both Ollama and llamafile halves."""
    calls = []

    def _ollama(cfg, *, non_interactive, dry_run):
        calls.append(("ollama", non_interactive, dry_run))

    def _llamafile(cfg, *, non_interactive, dry_run):
        calls.append(("llamafile", non_interactive, dry_run))

    monkeypatch.setattr(install, "_setup_ollama_chat", _ollama)
    monkeypatch.setattr(
        install, "_setup_llamafile_chat_models", _llamafile,
    )

    install._setup_chat_backends({}, non_interactive=True, dry_run=False)
    assert calls == [
        ("ollama", True, False),
        ("llamafile", True, False),
    ]
