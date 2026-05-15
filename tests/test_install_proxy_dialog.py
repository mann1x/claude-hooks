"""Tests for the v1.6.1 install.py API-proxy dialog redesign.

Before v1.6.1, ``_setup_proxy`` asked a single "Use the API proxy?"
question, defaulted off ``cfg.proxy.enabled``. That flag means "is
the proxy installed locally on this host" — so a host pointing at a
**remote** proxy via ANTHROPIC_BASE_URL (e.g. pandorum routing
through solidpc:38080) saw "current: no" even though it was clearly
using a proxy.

v1.6.1 splits the dialog in two:

1. **Install the API proxy locally?** — V/r/s on re-runs when a local
   service is already on disk; y/N otherwise.
2. **Use the API proxy?** — labels the current state as
   ``remote @ <url>`` / ``local @ <url>`` / ``no``; if Y, asks for
   the endpoint with a sensible default (configured URL, else local
   address if just installed).
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


def _capturing_input(answers, log):
    """Same as _scripted_input but logs each prompt to ``log`` so tests
    can assert on the label string the dialog showed."""
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        log.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


# --------------------------------------------------------------------- #
# _classify_proxy_url — pure
# --------------------------------------------------------------------- #


class TestClassify(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(install._classify_proxy_url(""), "none")

    def test_official(self):
        self.assertEqual(install._classify_proxy_url(
            "https://api.anthropic.com"), "official")
        self.assertEqual(install._classify_proxy_url(
            "https://api.anthropic.com/"), "official")

    def test_local_loopback(self):
        self.assertEqual(install._classify_proxy_url(
            "http://127.0.0.1:38080"), "local")
        self.assertEqual(install._classify_proxy_url(
            "http://localhost:38080"), "local")

    def test_remote_lan(self):
        self.assertEqual(install._classify_proxy_url(
            "http://192.168.178.2:38080"), "remote")
        self.assertEqual(install._classify_proxy_url(
            "https://proxy.example.com"), "remote")


# --------------------------------------------------------------------- #
# _read_current_anthropic_base_url
# --------------------------------------------------------------------- #


class TestReadCurrentUrl(unittest.TestCase):
    def test_missing_file(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            self.assertEqual(install._read_current_anthropic_base_url(p), "")

    def test_no_env_block(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({"hooks": {}}))
            self.assertEqual(install._read_current_anthropic_base_url(p), "")

    def test_present(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({
                "env": {"ANTHROPIC_BASE_URL": "http://192.168.178.2:38080"},
            }))
            self.assertEqual(
                install._read_current_anthropic_base_url(p),
                "http://192.168.178.2:38080",
            )

    def test_malformed_json(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text("{not json")
            self.assertEqual(install._read_current_anthropic_base_url(p), "")


# --------------------------------------------------------------------- #
# _proxy_locally_installed — platform-pinned, source-asserted
# --------------------------------------------------------------------- #


class TestProxyLocallyInstalled(unittest.TestCase):
    def test_returns_pair(self):
        # On any host the function must return a (bool, str) tuple.
        installed, kind = install._proxy_locally_installed()
        self.assertIsInstance(installed, bool)
        self.assertIn(kind, ("systemd", "launchd", "task", ""))

    def test_source_covers_all_three_platforms(self):
        # Pin the branch coverage — the function must handle systemd,
        # launchd, and Windows-scheduled-task install paths.
        src = (Path(install.__file__).read_text(encoding="utf-8"))
        i = src.index("def _proxy_locally_installed")
        j = src.index("def ", i + 1)
        body = src[i:j]
        self.assertIn("systemd", body)
        self.assertIn("launchd", body)
        self.assertIn("_windows_task_exists", body)
        self.assertIn("_PROXY_TASK_NAME", body)


# --------------------------------------------------------------------- #
# Dialog: Q1 (install) shape
# --------------------------------------------------------------------- #


class TestProxyDialogQ1(unittest.TestCase):

    def _run_dialog(self, *, installed, kind, current_url,
                     scripted, settings_path=None):
        """Run ``_setup_proxy`` with the requested install state and
        ANTHROPIC_BASE_URL stubbed, capturing every prompt string."""
        prompts: list[str] = []
        cfg: dict = {}
        if settings_path is None:
            settings_path = Path("/tmp/nonexistent-settings.json")
        with patch.object(install, "_proxy_locally_installed",
                          return_value=(installed, kind)), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value=current_url), \
             patch.object(install, "_verify_proxy_health",
                          return_value=(True, "HTTP 200 — ok")), \
             patch.object(install, "_set_settings_env_vars"), \
             patch("builtins.input",
                   _capturing_input(scripted, prompts)), \
             patch("sys.stdout", io.StringIO()):
            install._setup_proxy_orchestrator(
                cfg, settings_path,
                non_interactive=False, dry_run=False,
            )
        return cfg, prompts

    def test_not_installed_uses_install_y_n_prompt(self):
        """When no local service exists, Q1 must use ``Install ... [y/N]``,
        defaulting N. The dead 'Use the API proxy? [y/N]' shape from v1.6
        and earlier is gone.
        """
        cfg, prompts = self._run_dialog(
            installed=False, kind="",
            current_url="",
            scripted=["n", "n"],  # don't install + don't use any proxy
        )
        # First prompt must be the install prompt.
        self.assertTrue(any("Install the API proxy locally" in p
                            for p in prompts),
                        f"missing install prompt: {prompts}")
        # Must NOT have the legacy "Choose [1/2]" two-mode prompt.
        for p in prompts:
            self.assertNotIn("Choose [1/2]", p)

    def test_installed_shows_verify_reinstall_skip(self):
        """When the service is on disk, Q1 must offer V/r/s."""
        cfg, prompts = self._run_dialog(
            installed=True, kind="systemd",
            current_url="http://127.0.0.1:38080",
            scripted=["v", "n"],  # verify; don't change Q2
        )
        self.assertTrue(any("V]erify" in p and "R]e-install" in p
                            and "S]kip" in p for p in prompts),
                        f"missing V/r/s prompt: {prompts}")

    def test_verify_calls_health_probe_and_keeps_enabled(self):
        cfg: dict = {}
        with patch.object(install, "_proxy_locally_installed",
                          return_value=(True, "systemd")), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value="http://127.0.0.1:38080"), \
             patch.object(install, "_verify_proxy_health",
                          return_value=(True, "HTTP 200")) as probe, \
             patch.object(install, "_set_settings_env_vars"), \
             patch("builtins.input",
                   _scripted_input(["", "n"])), \
             patch("sys.stdout", io.StringIO()):
            # "" -> default V, "n" -> skip Q2
            install._setup_proxy_orchestrator(
                cfg, Path("/tmp/x"),
                non_interactive=False, dry_run=False,
            )
        probe.assert_called_once()
        # service is on disk — enabled flag reflects reality
        self.assertTrue(cfg["proxy"]["enabled"])

    def test_reinstall_marks_install_locally(self):
        cfg: dict = {}
        with patch.object(install, "_proxy_locally_installed",
                          return_value=(True, "systemd")), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value=""), \
             patch.object(install, "_verify_proxy_health",
                          return_value=(True, "ok")), \
             patch.object(install, "_set_settings_env_vars"), \
             patch("builtins.input",
                   _scripted_input(["r", "n"])), \
             patch("sys.stdout", io.StringIO()):
            install._setup_proxy_orchestrator(
                cfg, Path("/tmp/x"),
                non_interactive=False, dry_run=False,
            )
        # cfg.proxy.enabled = True so the per-OS installer writes again
        self.assertTrue(cfg["proxy"]["enabled"])


# --------------------------------------------------------------------- #
# Dialog: Q2 (use) shape
# --------------------------------------------------------------------- #


class TestProxyDialogQ2Labels(unittest.TestCase):

    def _label_in_q2(self, *, installed, kind, current_url, q1_answer):
        """Return the prompt string that contained 'Use the API proxy?'.
        Drives label coverage for the three states.
        """
        prompts: list[str] = []
        with patch.object(install, "_proxy_locally_installed",
                          return_value=(installed, kind)), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value=current_url), \
             patch.object(install, "_verify_proxy_health",
                          return_value=(True, "ok")), \
             patch.object(install, "_set_settings_env_vars"), \
             patch("builtins.input",
                   _capturing_input([q1_answer, "n"], prompts)), \
             patch("sys.stdout", io.StringIO()):
            install._setup_proxy_orchestrator(
                {}, Path("/tmp/x"),
                non_interactive=False, dry_run=False,
            )
        for p in prompts:
            if "Use the API proxy" in p:
                return p
        raise AssertionError(f"no Q2 prompt found: {prompts}")

    def test_label_remote(self):
        # Pandorum's scenario: remote URL, no local install.
        p = self._label_in_q2(
            installed=False, kind="",
            current_url="http://192.168.178.2:38080",
            q1_answer="n",  # don't install locally
        )
        self.assertIn("remote @ http://192.168.178.2:38080", p)
        self.assertIn("[Y/n]", p)  # default Y when configured

    def test_label_local(self):
        p = self._label_in_q2(
            installed=True, kind="systemd",
            current_url="http://127.0.0.1:38080",
            q1_answer="",  # default V
        )
        self.assertIn("local @ http://127.0.0.1:38080", p)
        self.assertIn("[Y/n]", p)

    def test_label_no(self):
        p = self._label_in_q2(
            installed=False, kind="",
            current_url="",
            q1_answer="n",  # don't install
        )
        self.assertIn("current: no", p)
        self.assertIn("[y/N]", p)  # default N when nothing configured

    def test_label_just_installed(self):
        # Q1 just installed locally, Q2 not yet wired.
        p = self._label_in_q2(
            installed=False, kind="",
            current_url="",
            q1_answer="y",  # install locally
        )
        self.assertIn("will install local", p)
        self.assertIn("[Y/n]", p)  # default Y so the URL gets wired


class TestProxyDialogQ2EndpointDefault(unittest.TestCase):

    def _run_and_capture_set(self, *, installed, current_url, q1, q2_url):
        """Drive the dialog through Q1 + Q2-yes and capture the URL
        passed to ``_set_settings_env_vars``."""
        captured = {}

        def fake_set(settings_path, vars_to_set, *, dry_run=False):
            captured.update(vars_to_set)

        with patch.object(install, "_proxy_locally_installed",
                          return_value=(installed, "systemd" if installed else "")), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value=current_url), \
             patch.object(install, "_verify_proxy_health",
                          return_value=(True, "ok")), \
             patch.object(install, "_set_settings_env_vars",
                          side_effect=fake_set), \
             patch("builtins.input",
                   _scripted_input([q1, "y", q2_url])), \
             patch("sys.stdout", io.StringIO()):
            install._setup_proxy_orchestrator(
                {}, Path("/tmp/x"),
                non_interactive=False, dry_run=False,
            )
        return captured

    def test_endpoint_default_uses_current_when_set(self):
        """Currently-configured remote URL is the proposed default —
        empty input accepts it."""
        captured = self._run_and_capture_set(
            installed=False, current_url="http://192.168.178.2:38080",
            q1="n",                  # don't install locally
            q2_url="",               # accept default (current URL)
        )
        self.assertEqual(
            captured.get("ANTHROPIC_BASE_URL"),
            "http://192.168.178.2:38080",
        )

    def test_endpoint_default_uses_local_when_just_installed(self):
        """Q1 installed locally, no URL was set before → propose
        local_url as the default."""
        captured = self._run_and_capture_set(
            installed=False, current_url="",
            q1="y",                  # install locally
            q2_url="",               # accept default
        )
        url = captured.get("ANTHROPIC_BASE_URL", "")
        self.assertTrue(url.startswith("http://127.0.0.1:"),
                        f"expected loopback default, got {url!r}")

    def test_endpoint_explicit_url_overrides_default(self):
        """User typing a URL overrides any default."""
        captured = self._run_and_capture_set(
            installed=False, current_url="",
            q1="n",                  # don't install locally
            q2_url="http://10.0.0.5:38080",  # explicit endpoint
        )
        self.assertEqual(
            captured.get("ANTHROPIC_BASE_URL"),
            "http://10.0.0.5:38080",
        )


# --------------------------------------------------------------------- #
# Skip path — answering N to Q2 must not strip an existing URL
# --------------------------------------------------------------------- #


class TestSkipPath(unittest.TestCase):
    def test_q2_no_leaves_settings_untouched(self):
        """If a remote URL is already configured and the user says N to
        Q2, the dialog must NOT call _set_settings_env_vars to strip
        ANTHROPIC_BASE_URL. (Removing it requires a manual edit — same
        posture as every other "stay as you are" prompt in install.py.)
        """
        set_called = []

        def fake_set(*args, **kwargs):
            set_called.append(args)

        with patch.object(install, "_proxy_locally_installed",
                          return_value=(False, "")), \
             patch.object(install, "_read_current_anthropic_base_url",
                          return_value="http://192.168.178.2:38080"), \
             patch.object(install, "_set_settings_env_vars",
                          side_effect=fake_set), \
             patch("builtins.input",
                   _scripted_input(["n", "n"])), \
             patch("sys.stdout", io.StringIO()):
            install._setup_proxy_orchestrator(
                {}, Path("/tmp/x"),
                non_interactive=False, dry_run=False,
            )
        self.assertEqual(set_called, [])


if __name__ == "__main__":
    unittest.main()
