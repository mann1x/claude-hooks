"""Server-level multi-root tests for caliber-grounding-proxy (v1.8+).

The proxy is the third runner that builds a tool executor through
``tools.make_executor``. Unlike the advisor + consultants CLIs, the
proxy explicitly does NOT honor request-body fields for sandbox
configuration — same posture as ``_cwd_for_request``: the body is
constructed by the LLM and is prompt-injectable. Extra roots come
from:

  * ``CALIBER_GROUNDING_ADD_DIRS`` env var (operator-trusted)
  * ``~/.claude/settings.json`` (operator-trusted)
  * ``<cwd>/.claude/settings.json`` and
    ``<cwd>/.claude/settings.local.json`` (operator-trusted)
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_hooks.caliber_proxy import server as caliber_server
from claude_hooks.caliber_proxy import tools as caliber_tools


class TestExtraRootsDiscovery(unittest.TestCase):

    def setUp(self):
        # Capture env so each test is hermetic.
        self._env = {
            k: os.environ.pop(k, None)
            for k in ("CALIBER_GROUNDING_ADD_DIRS", "CALIBER_GROUNDING_CWD")
        }

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_no_settings_no_env_returns_empty(self):
        # No settings.json on disk in tempdir → no extra roots.
        with tempfile.TemporaryDirectory() as cwd:
            os.environ["CALIBER_GROUNDING_CWD"] = cwd
            # Point ~/.claude/settings.json discovery at a nonexistent path
            # by patching the user-settings constant for the duration of the
            # call. Easier: mock discover_allowed_roots's settings_files.
            with mock.patch(
                "claude_hooks.allowed_roots._candidate_settings_files",
                return_value=[os.path.join(cwd, ".claude", "settings.json")],
            ):
                extras = caliber_server._extra_roots_for_request()
            self.assertEqual(extras, ())

    def test_env_var_contributes(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            extra1 = os.path.join(base, "extra1")
            extra2 = os.path.join(base, "extra2")
            for p in (cwd, extra1, extra2):
                os.makedirs(p)
            os.environ["CALIBER_GROUNDING_CWD"] = cwd
            os.environ["CALIBER_GROUNDING_ADD_DIRS"] = (
                f"{extra1}{os.pathsep}{extra2}"
            )
            with mock.patch(
                "claude_hooks.allowed_roots._candidate_settings_files",
                return_value=[],
            ):
                extras = caliber_server._extra_roots_for_request()
            self.assertEqual(set(extras), {
                os.path.realpath(extra1), os.path.realpath(extra2),
            })

    def test_settings_local_json_contributes(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            extra = os.path.join(base, "from-settings")
            for p in (cwd, extra):
                os.makedirs(p)
            settings_dir = os.path.join(cwd, ".claude")
            os.makedirs(settings_dir)
            settings_file = os.path.join(settings_dir, "settings.local.json")
            Path(settings_file).write_text(
                '{"permissions":{"additionalDirectories":["%s"]}}'
                % extra.replace("\\", "/")
            )
            os.environ["CALIBER_GROUNDING_CWD"] = cwd
            with mock.patch(
                "claude_hooks.allowed_roots._candidate_settings_files",
                return_value=[settings_file],
            ):
                extras = caliber_server._extra_roots_for_request()
            self.assertEqual(extras, (os.path.realpath(extra),))

    def test_env_unions_with_settings(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            from_env = os.path.join(base, "env-dir")
            from_set = os.path.join(base, "settings-dir")
            for p in (cwd, from_env, from_set):
                os.makedirs(p)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text(
                '{"permissions":{"additionalDirectories":["%s"]}}'
                % from_set.replace("\\", "/")
            )
            os.environ["CALIBER_GROUNDING_CWD"] = cwd
            os.environ["CALIBER_GROUNDING_ADD_DIRS"] = from_env
            with mock.patch(
                "claude_hooks.allowed_roots._candidate_settings_files",
                return_value=[settings],
            ):
                extras = caliber_server._extra_roots_for_request()
            self.assertEqual(set(extras), {
                os.path.realpath(from_env), os.path.realpath(from_set),
            })


class TestRunAgentLoopExecutor(unittest.TestCase):
    """``run_agent_loop`` builds a multi-root executor when extras are
    provided, and the closure-free fast path when they aren't."""

    def test_no_extras_uses_bare_execute(self):
        # When extras=(), make_executor() returns the bare execute fn.
        # We can't easily intercept the agent_loop_runner call without
        # stubbing the whole HTTP path, so just assert the make_executor
        # contract directly.
        self.assertIs(caliber_tools.make_executor(()), caliber_tools.execute)

    def test_request_body_extra_roots_are_ignored(self):
        # Security posture: even if the body declares extra_roots,
        # the proxy doesn't forward them. _extra_roots_for_request
        # reads only env + settings, never the body.
        import inspect
        sig = inspect.signature(caliber_server._extra_roots_for_request)
        # No parameters → cannot accept body input by design.
        self.assertEqual(len(sig.parameters), 0)


if __name__ == "__main__":
    unittest.main()
