"""Tests for the v1.4 install.py embedding-engine dialog.

Covers ``_setup_embedding_engine`` (the parameterised dialog reused
by pgvector / sqlite_vec), ``_setup_llamafile_engine`` (the
sub-dialog for llamafile knobs), ``_download_composite_llamafile``
(GH-Release asset fetch + SHA verification), and the small
utilities (``_verify_sha256``, ``_gguf_magic_ok``,
``_read_committed_composite_sha``).

The dialog drives the user via ``input()``; we replace that with a
scripted iterator so tests can walk every branch without an
interactive shell.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _scripted_input(answers):
    """Build a replacement for builtins.input that pops scripted
    answers one at a time. Raises if the dialog asks more questions
    than we scripted (so a test omission shows up as a clean failure
    rather than a hang)."""
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


def _stub_ollama_ok(*, has_model: bool = True):
    """Patch the install.py-level Ollama helpers so the dialog never
    actually touches the network."""
    return [
        patch.object(install, "_ollama_model_present", return_value=has_model),
        patch.object(install, "_ollama_pull", return_value=True),
    ]


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# --------------------------------------------------------------------- #
# _verify_sha256 + _gguf_magic_ok + _read_committed_composite_sha
# --------------------------------------------------------------------- #

class TestSha256Verify:
    def test_matches(self, tmp_path):
        p = tmp_path / "a"
        p.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert install._verify_sha256(p, expected) is True

    def test_mismatch(self, tmp_path):
        p = tmp_path / "a"
        p.write_bytes(b"hello world")
        assert install._verify_sha256(p, "0" * 64) is False

    def test_missing_file(self, tmp_path):
        assert install._verify_sha256(tmp_path / "missing", "x") is False


class TestGgufMagic:
    def test_real_gguf(self, tmp_path):
        p = tmp_path / "f.gguf"
        p.write_bytes(b"GGUF\x03\x00\x00\x00rest")
        assert install._gguf_magic_ok(str(p)) is True

    def test_not_gguf(self, tmp_path):
        p = tmp_path / "f.gguf"
        p.write_bytes(b"NOPE")
        assert install._gguf_magic_ok(str(p)) is False

    def test_missing(self):
        assert install._gguf_magic_ok("/nope/missing.gguf") is False


class TestReadCommittedSha:
    def test_missing_file_returns_empty(self, tmp_path):
        with patch.object(install, "_LLAMAFILE_SHA_FILE", tmp_path / "missing"):
            assert install._read_committed_composite_sha() == ""

    def test_parses_sha_with_filename(self, tmp_path):
        sha = "a" * 64
        f = tmp_path / "SHA256SUMS.composite"
        f.write_text(f"# comment\n{sha}  {install._DEFAULT_LLAMAFILE_ASSET}\n")
        with patch.object(install, "_LLAMAFILE_SHA_FILE", f):
            assert install._read_committed_composite_sha() == sha

    def test_parses_bare_sha(self, tmp_path):
        sha = "b" * 64
        f = tmp_path / "SHA256SUMS.composite"
        f.write_text(f"{sha}\n")
        with patch.object(install, "_LLAMAFILE_SHA_FILE", f):
            assert install._read_committed_composite_sha() == sha


# --------------------------------------------------------------------- #
# _download_composite_llamafile
# --------------------------------------------------------------------- #

class TestDownloadComposite:
    def test_dry_run_no_op(self, tmp_path):
        target = tmp_path / "out.llamafile"
        assert install._download_composite_llamafile(
            target, sha256="", dry_run=True,
        ) is True
        assert not target.exists()

    def test_gh_path_success(self, tmp_path):
        target = tmp_path / "out.llamafile"

        def _fake_run(cmd, **kw):
            # gh release download is expected to deposit the asset
            # at target.parent / asset_name (with --output).
            target.write_bytes(b"binary-bytes")
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        sha = hashlib.sha256(b"binary-bytes").hexdigest()
        with patch.object(install.shutil, "which", return_value="/usr/bin/gh"):
            with patch.object(install.subprocess, "run", side_effect=_fake_run):
                ok = install._download_composite_llamafile(
                    target, sha256=sha, dry_run=False,
                )
        assert ok is True
        assert target.exists()

    def test_gh_fail_falls_back_to_urlopen(self, tmp_path):
        target = tmp_path / "out.llamafile"

        def _fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            m.stderr = "auth required"
            return m

        class _FakeBytesResp:
            def __init__(self, data):
                self._data = data
                self._i = 0

            def read(self, n=-1):
                chunk = self._data[self._i:]
                self._i = len(self._data)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        payload = b"payload"
        sha = hashlib.sha256(payload).hexdigest()

        with patch.object(install.shutil, "which", return_value="/usr/bin/gh"):
            with patch.object(install.subprocess, "run", side_effect=_fake_run):
                with patch("urllib.request.urlopen",
                           return_value=_FakeBytesResp(payload)):
                    ok = install._download_composite_llamafile(
                        target, sha256=sha, dry_run=False,
                    )
        assert ok is True
        assert target.read_bytes() == payload

    def test_sha_mismatch_removes_file(self, tmp_path):
        target = tmp_path / "out.llamafile"

        def _fake_run(cmd, **kw):
            target.write_bytes(b"wrong-bytes")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch.object(install.shutil, "which", return_value="/usr/bin/gh"):
            with patch.object(install.subprocess, "run", side_effect=_fake_run):
                ok = install._download_composite_llamafile(
                    target, sha256="0" * 64, dry_run=False,
                )
        assert ok is False
        assert not target.exists()

    def test_no_sha_skips_verification(self, tmp_path):
        target = tmp_path / "out.llamafile"

        def _fake_run(cmd, **kw):
            target.write_bytes(b"anything")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch.object(install.shutil, "which", return_value="/usr/bin/gh"):
            with patch.object(install.subprocess, "run", side_effect=_fake_run):
                ok = install._download_composite_llamafile(
                    target, sha256="", dry_run=False,
                )
        assert ok is True
        assert target.exists()


# --------------------------------------------------------------------- #
# _setup_llamafile_engine
# --------------------------------------------------------------------- #

class TestSetupLlamafileEngine:
    def test_non_interactive_defaults(self, tmp_path):
        cfg = {}
        # Pretend the composite is already on disk so no download
        # attempt fires.
        target = install._LLAMAFILE_DIST_DIR / install._DEFAULT_LLAMAFILE_ASSET
        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(
                cfg, non_interactive=True, dry_run=False,
            )
        assert block["enabled"] is True
        assert block["llamafile_path"] == str(target)
        assert block["model_gguf"] == ""
        assert block["ctx_size"] == install._DEFAULT_LLAMAFILE_CTX
        assert block["pooling"] == "last"
        assert block["mode"] == "auto"  # existing missing -> default auto
        assert block["port"] == 38092

    def test_interactive_default_path(self, tmp_path, monkeypatch):
        """User accepts defaults + chooses 'auto' GPU mode."""
        cfg = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "",   # accept default settings
            "",   # accept GPU mode default (auto when GPU detected)
            "",   # #242: accept LAN-exposure default (loopback)
        ]))
        with patch.object(Path, "is_file", return_value=True):
            # Pretend an NVIDIA GPU was detected.
            with patch("claude_hooks.gpu_probe.probe", return_value={
                    "vendor": "nvidia", "total_mb": 24000,
                    "free_mb": 22000, "raw": "x",
            }):
                block = install._setup_llamafile_engine(
                    cfg, non_interactive=False, dry_run=False,
                )
        assert block["mode"] == "auto"
        assert block["model_gguf"] == ""  # default = baked composite
        # #242: default LAN-exposure answer is loopback.
        assert block["host"] == "127.0.0.1"

    def test_interactive_cpu_mode(self, tmp_path, monkeypatch):
        cfg = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "",      # accept default settings
            "cpu",   # force CPU mode
            "",      # #242: accept LAN-exposure default (loopback)
        ]))
        with patch.object(Path, "is_file", return_value=True):
            with patch("claude_hooks.gpu_probe.probe", return_value={
                    "vendor": "nvidia", "total_mb": 24000,
                    "free_mb": 22000, "raw": "x",
            }):
                block = install._setup_llamafile_engine(
                    cfg, non_interactive=False, dry_run=False,
                )
        assert block["mode"] == "cpu"

    def test_interactive_custom_gguf(self, tmp_path, monkeypatch):
        # Make a fake GGUF on disk.
        gguf = tmp_path / "custom.gguf"
        gguf.write_bytes(b"GGUF\x03\x00\x00\x00rest-of-header")

        cfg = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "n",          # decline default settings
            str(gguf),    # custom GGUF path
            "8192",       # custom ctx
            "cpu",        # CPU mode
            "",           # #242: accept LAN-exposure default (loopback)
        ]))
        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        assert block["model_gguf"] == str(gguf)
        assert block["ctx_size"] == 8192
        assert block["mode"] == "cpu"

    def test_invalid_gguf_re_prompts(self, tmp_path, monkeypatch):
        not_gguf = tmp_path / "not_gguf"
        not_gguf.write_bytes(b"NOPE-not-a-gguf")
        good = tmp_path / "good.gguf"
        good.write_bytes(b"GGUF\x03\x00\x00\x00rest")

        cfg = {}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "n",                # decline default
            str(not_gguf),      # bad path (magic wrong) -> re-prompt
            str(good),          # good path
            "",                 # accept default ctx
            "cpu",              # CPU mode
            "",                 # #242: accept LAN-exposure default (loopback)
        ]))
        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        assert block["model_gguf"] == str(good)


# --------------------------------------------------------------------- #
# _setup_embedding_engine
# --------------------------------------------------------------------- #

class TestSetupEmbeddingEngine:
    @pytest.fixture(autouse=True)
    def stub_ollama_helpers(self):
        ps = _stub_ollama_ok(has_model=True)
        _enter(ps)
        yield
        _exit(ps)

    @pytest.fixture(autouse=True)
    def stub_composite_present(self):
        # Pretend the composite is on disk so the dialog never fetches.
        with patch.object(Path, "is_file", return_value=True):
            yield

    def test_non_interactive_default_is_composite(self):
        cfg = {"providers": {"pgvector": {}}}
        install._setup_embedding_engine(
            cfg, provider="pgvector",
            non_interactive=True, dry_run=False,
        )
        pcfg = cfg["providers"]["pgvector"]
        assert pcfg["embedder"] == "composite"
        opts = pcfg["embedder_options"]
        assert opts["primary"] == "ollama"
        assert opts["primary_options"]["model"] == install._DEFAULT_LLAMAFILE_MODEL
        assert opts["fallback"] == "llamafile"
        # The shared embedding block is also written.
        assert cfg["embedding"]["enabled"] is True
        assert cfg["embedding"]["port"] == 38092

    def test_interactive_ollama_only(self, monkeypatch):
        """User picks Ollama primary + declines llamafile fallback."""
        cfg = {"providers": {"pgvector": {}}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",      # use Ollama? yes
            "",       # URL default
            "",       # model default
            "",       # num_ctx default
            "n",      # use llamafile fallback? no
        ]))
        install._setup_embedding_engine(
            cfg, provider="pgvector",
            non_interactive=False, dry_run=False,
        )
        pcfg = cfg["providers"]["pgvector"]
        assert pcfg["embedder"] == "ollama"
        assert pcfg["embedder_options"]["model"] == install._DEFAULT_LLAMAFILE_MODEL
        # No shared embedding block since llamafile wasn't enabled.
        assert "embedding" not in cfg

    def test_interactive_ollama_with_llamafile_fallback(self, monkeypatch):
        cfg = {"providers": {"pgvector": {}}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "y",      # use Ollama? yes
            "",       # URL default
            "",       # model default
            "",       # num_ctx default
            "y",      # use llamafile fallback? yes
            "",       # accept llamafile defaults
            "cpu",    # CPU mode for predictability
            "",       # #242: accept LAN-exposure default (loopback)
        ]))
        with patch("claude_hooks.gpu_probe.probe", return_value={
                "vendor": "nvidia", "total_mb": 1, "free_mb": 1, "raw": "x"
        }):
            install._setup_embedding_engine(
                cfg, provider="pgvector",
                non_interactive=False, dry_run=False,
            )
        pcfg = cfg["providers"]["pgvector"]
        assert pcfg["embedder"] == "composite"
        opts = pcfg["embedder_options"]
        assert opts["primary"] == "ollama"
        assert opts["fallback"] == "llamafile"
        assert opts["fallback_options"]["url"].endswith(":38092/embedding")
        assert cfg["embedding"]["enabled"] is True
        assert cfg["embedding"]["mode"] == "cpu"

    def test_interactive_llamafile_only(self, monkeypatch):
        """User declines Ollama AND declines OpenAI -> llamafile is
        the primary (mandatory)."""
        cfg = {"providers": {"pgvector": {}}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "n",      # use Ollama? no
            "n",      # #237: use remote llamafile? no
            "n",      # use OpenAI? no
            "",       # accept llamafile defaults
            "cpu",    # CPU mode
            "",       # #242: accept LAN-exposure default (loopback)
        ]))
        with patch("claude_hooks.gpu_probe.probe", return_value={
                "vendor": "none", "total_mb": None, "free_mb": None, "raw": None
        }):
            install._setup_embedding_engine(
                cfg, provider="pgvector",
                non_interactive=False, dry_run=False,
            )
        pcfg = cfg["providers"]["pgvector"]
        assert pcfg["embedder"] == "llamafile"
        assert pcfg["embedder_options"]["url"].endswith(":38092/embedding")
        assert cfg["embedding"]["enabled"] is True

    def test_interactive_openai_primary(self, monkeypatch):
        cfg = {"providers": {"pgvector": {}}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "n",                                  # use Ollama? no
            "n",                                  # #237: use remote llamafile? no
            "y",                                  # use OpenAI? yes
            "https://my-server/v1/embeddings",    # URL
            "text-embedding-3-large",             # model
            "${OPENAI_API_KEY}",                  # api_key
            "y",                                  # use llamafile fallback? yes
            "",                                   # llamafile defaults
            "cpu",                                # CPU
            "",                                   # #242: LAN-exposure default
        ]))
        with patch("claude_hooks.gpu_probe.probe", return_value={
                "vendor": "none", "total_mb": None, "free_mb": None, "raw": None
        }):
            install._setup_embedding_engine(
                cfg, provider="pgvector",
                non_interactive=False, dry_run=False,
            )
        pcfg = cfg["providers"]["pgvector"]
        assert pcfg["embedder"] == "composite"
        opts = pcfg["embedder_options"]
        assert opts["primary"] == "openai_compatible"
        assert opts["primary_options"]["model"] == "text-embedding-3-large"
        assert opts["fallback"] == "llamafile"

    def test_provider_param_routes_to_sqlite_vec(self, monkeypatch):
        """Same dialog works for sqlite_vec — config is written to the
        right provider block."""
        cfg = {"providers": {"sqlite_vec": {"db_path": "~/x.db"}}}
        monkeypatch.setattr("builtins.input", _scripted_input([
            "n",      # use Ollama? no
            "n",      # #237: use remote llamafile? no
            "n",      # use OpenAI? no
            "",       # llamafile defaults
            "cpu",    # CPU
            "",       # #242: LAN-exposure default
        ]))
        with patch("claude_hooks.gpu_probe.probe", return_value={
                "vendor": "none", "total_mb": None, "free_mb": None, "raw": None
        }):
            install._setup_embedding_engine(
                cfg, provider="sqlite_vec",
                non_interactive=False, dry_run=False,
            )
        # Wrote to sqlite_vec, not pgvector.
        assert "embedder" in cfg["providers"]["sqlite_vec"]
        assert cfg["providers"]["sqlite_vec"]["embedder"] == "llamafile"
        # pgvector untouched.
        assert "pgvector" not in cfg["providers"]

    def test_existing_embedder_skipped_in_pgvector_path(self):
        """Reading _setup_pgvector_mcp: if cfg already has
        ``embedder`` set, the new dialog must NOT fire on upgrade."""
        # This is an integration-shape check via the _setup_pgvector_mcp
        # branch, not _setup_embedding_engine directly. We just verify
        # the protective condition is wired by checking the source.
        src = (REPO / "install.py").read_text(encoding="utf-8")
        assert 'if not cfg["providers"]["pgvector"].get("embedder"):' in src
