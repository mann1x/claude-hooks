"""Tests for v1.5.4 install.py pgvector validate-only path.

Before v1.5.4, ``_setup_pgvector_mcp`` only offered a Y/N prompt
("Set up pgvector? [Y/n]"), defaulting Y whenever a DSN was already
present in cfg. That meant a re-run on a fully-configured host
forced the user through the full setup path (re-verify DSN +
re-probe embedder + re-drop launcher + re-register MCP server) even
though they really only wanted a sanity-check.

v1.5.4 adds a third option: when pgvector is fully configured
(DSN + enabled + launcher), prompt
"[V]alidate only / [R]e-install / [S]kip? [V/r/s]" with V default.
V runs probes only — no writes. The non-interactive path is
unchanged (DSN present -> assume yes -> full re-install).
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402
from tests._fixtures_net import FIXTURE_LAN_HOST, FIXTURE_PG_DSN  # noqa: E402


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


def _fully_configured_cfg() -> dict:
    return {"providers": {"pgvector": {
        "enabled": True,
        "dsn": FIXTURE_PG_DSN,
        "table": "memories_qwen3",
        "embedder": "llamafile",
        "embedder_options": {
            "url": "http://127.0.0.1:38092/embedding",
            "model": "qwen3-embedding:0.6b",
        },
    }}}


# ----------------------------------------------------------------- #
# Validate-only path
# ----------------------------------------------------------------- #

class TestValidateOnly(unittest.TestCase):

    def test_validate_runs_dsn_probe_and_no_writes(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        # Probe succeeds; no write helpers should be called
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(True, "")) as verify, \
             patch.object(install, "_write_pgvector_launcher") as drop, \
             patch.object(install, "_register_pgvector_mcp_in_claude_json") as reg, \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        verify.assert_called_once_with(cfg["providers"]["pgvector"]["dsn"])
        drop.assert_not_called()
        reg.assert_not_called()
        # Output mentions validate-only completion
        self.assertIn("validate-only complete", out.getvalue())

    def test_validate_reports_dsn_failure(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(False, "connection refused")), \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("connection refused", out.getvalue())
        # Doesn't proceed to embedder probe after DSN failure
        self.assertNotIn("Probing Ollama at", out.getvalue())

    def test_validate_skips_embedder_probe_when_llamafile(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        # llamafile embedder shouldn't trigger an Ollama probe
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(True, "")), \
             patch.object(install, "_ollama_model_present") as ollama_probe, \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        ollama_probe.assert_not_called()
        # Default daemon_ensure (unset) → True → "daemon-managed" label.
        self.assertIn("daemon-managed", out.getvalue())
        self.assertNotIn("remote, no local supervision", out.getvalue())

    def test_validate_remote_llamafile_labels_correctly(self):
        """Consumer-of-LAN-shared-llamafile config: pgvector + llamafile +
        daemon_ensure=false. The validator must NOT claim it's
        daemon-managed — the daemon does not spawn it locally — and
        must NOT print the "daemon spawns on demand" note. URL uses
        a TEST-NET-1 (RFC 5737) address so the assertion is local to
        the test and doesn't bake any real infra into the suite."""
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        remote_url = f"http://{FIXTURE_LAN_HOST}:38092/embedding"
        cfg["providers"]["pgvector"]["embedder_options"]["url"] = remote_url
        cfg["providers"]["pgvector"]["embedder_options"]["daemon_ensure"] = False
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(True, "")), \
             patch.object(install, "_ollama_model_present") as ollama_probe, \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        ollama_probe.assert_not_called()
        text = out.getvalue()
        self.assertIn("remote, no local supervision", text)
        self.assertNotIn("(daemon-managed)", text)
        # And the "daemon spawns llamafile on demand" note must not fire
        # — that note is wrong when this host isn't the spawning host.
        self.assertNotIn("daemon spawns llamafile on demand", text)
        # The configured URL is surfaced so the user can verify their
        # config points where they expect. Reference the URL we put in
        # the fixture, not any real network address.
        self.assertIn(remote_url, text)

    def test_validate_runs_ollama_probe_when_ollama_embedder(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        cfg["providers"]["pgvector"]["embedder"] = "ollama"
        cfg["providers"]["pgvector"]["embedder_options"]["url"] = (
            "http://localhost:11434/api/embeddings"
        )
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(True, "")), \
             patch.object(install, "_ollama_model_present",
                          return_value=True) as ollama_probe, \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        ollama_probe.assert_called_once()
        self.assertIn("present", out.getvalue())

    def test_validate_warns_when_ollama_model_missing(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        cfg["providers"]["pgvector"]["embedder"] = "ollama"
        with patch.object(install, "_verify_pgvector_dsn",
                          return_value=(True, "")), \
             patch.object(install, "_ollama_model_present",
                          return_value=False), \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        self.assertIn("missing", out.getvalue())
        self.assertIn("ollama pull", out.getvalue())

    def test_validate_no_dsn_short_circuits(self):
        out = io.StringIO()
        cfg = {"providers": {"pgvector": {"enabled": True}}}
        with patch.object(install, "_verify_pgvector_dsn") as verify, \
             patch.object(sys, "stdout", out):
            install._validate_pgvector_only(cfg)
        verify.assert_not_called()
        self.assertIn("No DSN", out.getvalue())


# ----------------------------------------------------------------- #
# Prompt flow when fully configured
# ----------------------------------------------------------------- #

class TestSetupPromptFullyConfigured(unittest.TestCase):

    def _setup_mocks(self, tmp_path: Path):
        """Build a tmp launcher file so launcher_present is True."""
        launcher = tmp_path / "pgvector-mcp"
        launcher.write_text("dummy")
        return launcher

    def test_fully_configured_offers_vrs_prompt(self):
        # Capture the prompt by recording each prompt string the
        # scripted input() helper sees.
        prompts: list[str] = []

        def capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return ""  # empty -> defaults to V

        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = self._setup_mocks(Path(d))
            out = io.StringIO()
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch("builtins.input", capture), \
                 patch.object(sys, "stdout", out):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_called_once()
        # Prompt mentions both options and the V default
        self.assertTrue(any("alidate" in p for p in prompts),
                        f"no Validate in prompts: {prompts}")
        self.assertTrue(any("V/r/s" in p for p in prompts),
                        f"no V/r/s in prompts: {prompts}")
        # stdout shows the launcher path (read-only info)
        self.assertIn(str(launcher), out.getvalue())

    def test_explicit_V_runs_validate(self):
        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = self._setup_mocks(Path(d))
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch("builtins.input", _scripted_input(["v"])):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_called_once()

    def test_S_skips_silently(self):
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = self._setup_mocks(Path(d))
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn") as verify, \
                 patch("builtins.input", _scripted_input(["s"])), \
                 patch.object(sys, "stdout", out):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_not_called()
        verify.assert_not_called()
        self.assertIn("Skipped", out.getvalue())

    def test_R_falls_through_to_full_install(self):
        """R answer should go into the existing install path, which
        calls _verify_pgvector_dsn. We patch verify to return False
        so the test exits without touching the filesystem."""
        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = self._setup_mocks(Path(d))
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn",
                              return_value=(False, "test stub")) as verify, \
                 patch("builtins.input",
                       _scripted_input(["r", ""])):
                # Second prompt is "Postgres DSN [existing]:" — empty
                # accepts the existing DSN, then verify fails and we
                # bail out. That's enough to confirm we entered the
                # install path (not the validate path).
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_not_called()
        verify.assert_called_once()

    def test_y_treated_as_validate_for_fully_configured(self):
        """The most natural 'yes' answer for an already-working install
        is 'yes confirm it's working' — not 'yes redo it'. So y / yes
        should map to validate here (matches the V default), and only
        explicit r / re-install triggers the rewrite."""
        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = self._setup_mocks(Path(d))
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn") as verify, \
                 patch("builtins.input", _scripted_input(["y"])):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_called_once()
        verify.assert_not_called()


class TestSetupPromptPartiallyConfigured(unittest.TestCase):

    def test_dsn_set_but_disabled_uses_legacy_Y_N_prompt(self):
        """Existing DSN but enabled=False -> partially configured ->
        legacy Y/N prompt with Y default (not V/r/s)."""
        prompts: list[str] = []

        def capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return "n"

        cfg = _fully_configured_cfg()
        cfg["providers"]["pgvector"]["enabled"] = False
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = Path(d) / "pgvector-mcp"
            launcher.write_text("dummy")
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn",
                              return_value=(False, "stub")) as verify, \
                 patch("builtins.input", capture):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_not_called()
        verify.assert_not_called()
        # Legacy prompt shape, not V/r/s
        self.assertTrue(any("[Y/n]" in p for p in prompts),
                        f"no [Y/n] in prompts: {prompts}")
        self.assertFalse(any("V/r/s" in p for p in prompts),
                         f"unexpected V/r/s in prompts: {prompts}")

    def test_launcher_missing_uses_legacy_prompt(self):
        prompts: list[str] = []

        def capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return "n"

        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = Path(d) / "nonexistent-launcher"
            # NOT created, so launcher.exists() is False
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn",
                              return_value=(False, "stub")), \
                 patch("builtins.input", capture):
                install._setup_pgvector_mcp(
                    cfg, non_interactive=False, dry_run=False,
                )
        val.assert_not_called()
        self.assertTrue(any("[Y/n]" in p for p in prompts),
                        f"no [Y/n] in prompts: {prompts}")


class TestNonInteractiveUnchanged(unittest.TestCase):

    def test_non_interactive_with_dsn_still_assumes_yes(self):
        """v1.5.4 explicitly does NOT change non-interactive behaviour.
        DSN present -> assume yes -> proceed to full install path."""
        out = io.StringIO()
        cfg = _fully_configured_cfg()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            launcher = Path(d) / "pgvector-mcp"
            launcher.write_text("dummy")
            with patch.object(install, "_pgvector_launcher_path",
                              return_value=launcher), \
                 patch.object(install, "_validate_pgvector_only") as val, \
                 patch.object(install, "_verify_pgvector_dsn",
                              return_value=(False, "stub")) as verify, \
                 patch.object(sys, "stdout", out):
                # No input() patched — non-interactive shouldn't prompt
                install._setup_pgvector_mcp(
                    cfg, non_interactive=True, dry_run=False,
                )
        val.assert_not_called()
        # Verification was attempted (full install path) — not validate
        verify.assert_called_once()
        self.assertIn("assuming yes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
