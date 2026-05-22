"""Tests for the v1.10.0 opt-in, dep-aware skill installer flow.

Covers the four cases that drive ``_install_skills``:

  A. deps OK + not installed → "Install? [y/N]" (default N)
  B. deps OK + installed     → "Keep?    [Y/n]" (default Y;
                                content-changed → update)
  C. deps not OK + installed → "Remove?  [y/N]" (default N)
  D. deps not OK + not installed → warning only, no prompt

Plus the dep-check helpers (``_requires_binary``,
``_requires_recall_pipeline``, ``_requires_episodic_configured``,
``_requires_chat_backend``, ``_requires_lsp_engine``).

Each test uses a real on-disk SKILL.md surrogate so the
content-change branch is exercised faithfully rather than mocked.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# --------------------------------------------------------------------- #
# Dep-check helpers
# --------------------------------------------------------------------- #


class TestRequiresBinary(unittest.TestCase):
    def test_satisfied_via_installed_tools(self):
        check = install._requires_binary("caliber")
        ok, label = check({}, {"caliber": True})
        self.assertTrue(ok)
        self.assertIn("caliber", label)
        self.assertIn("installed", label)

    def test_satisfied_via_path(self):
        check = install._requires_binary("ls")  # always on PATH in CI
        ok, label = check({}, {})
        self.assertTrue(ok)
        self.assertIn("ls", label)

    def test_unsatisfied(self):
        check = install._requires_binary("no-such-binary-9f3a8")
        ok, label = check({}, {})
        self.assertFalse(ok)
        self.assertIn("not found", label)

    def test_back_compat_requires_tool_attr(self):
        """``SkillSpec.requires_tool`` reads back the binary name from
        the closure so the v1.9.3 manifest tests + a few other callers
        keep working without unpacking the callable."""
        check = install._requires_binary("caliber")
        self.assertEqual(getattr(check, "__claude_hooks_requires_tool__"), "caliber")


class TestRequiresRecallPipeline(unittest.TestCase):
    def test_empty_cfg_unsatisfied(self):
        ok, label = install._requires_recall_pipeline({}, {})
        self.assertFalse(ok)
        self.assertIn("no memory provider", label)

    def test_disabled_provider_unsatisfied(self):
        cfg = {"providers": {"pgvector": {"enabled": False}}}
        ok, _ = install._requires_recall_pipeline(cfg, {})
        self.assertFalse(ok)

    def test_one_enabled_provider_satisfied(self):
        cfg = {"providers": {"qdrant": {"enabled": True}}}
        ok, label = install._requires_recall_pipeline(cfg, {})
        self.assertTrue(ok)
        self.assertIn("qdrant", label)

    def test_multiple_enabled_lists_all(self):
        cfg = {"providers": {
            "qdrant": {"enabled": True},
            "pgvector": {"enabled": True},
            "memory_kg": {"enabled": False},
        }}
        ok, label = install._requires_recall_pipeline(cfg, {})
        self.assertTrue(ok)
        self.assertIn("pgvector", label)
        self.assertIn("qdrant", label)
        self.assertNotIn("memory_kg", label)


class TestRequiresEpisodicConfigured(unittest.TestCase):
    def test_missing_block_unsatisfied(self):
        ok, label = install._requires_episodic_configured({}, {})
        self.assertFalse(ok)
        self.assertIn("'off'", label)

    def test_mode_off_unsatisfied(self):
        ok, _ = install._requires_episodic_configured(
            {"episodic": {"mode": "off"}}, {},
        )
        self.assertFalse(ok)

    def test_client_mode_satisfied(self):
        ok, label = install._requires_episodic_configured(
            {"episodic": {"mode": "client"}}, {},
        )
        self.assertTrue(ok)
        self.assertIn("client", label)

    def test_server_mode_satisfied(self):
        ok, _ = install._requires_episodic_configured(
            {"episodic": {"mode": "server"}}, {},
        )
        self.assertTrue(ok)


class TestRequiresChatBackend(unittest.TestCase):
    def test_ollama_on_path_satisfied(self):
        with patch("install.shutil.which", return_value="/usr/bin/ollama"):
            ok, label = install._requires_chat_backend({}, {})
        self.assertTrue(ok)
        self.assertIn("ollama", label)

    def test_ollama_in_installed_tools_satisfied(self):
        with patch("install.shutil.which", return_value=None):
            ok, _ = install._requires_chat_backend({}, {"ollama": True})
        self.assertTrue(ok)

    def test_llamafile_registry_satisfied(self, tmp_path=None):
        with TemporaryDirectory() as tmp:
            reg = Path(tmp) / "llamafile-models.json"
            reg.write_text('{"models": {"qwen3": {"path": "/x.gguf", "port": 38101}}}')
            with patch("install.shutil.which", return_value=None), \
                 patch("install.os.path.expanduser", return_value=str(reg)):
                ok, label = install._requires_chat_backend({}, {})
        self.assertTrue(ok)
        self.assertIn("llamafile", label)
        self.assertIn("qwen3", label)

    def test_get_advice_backend_in_cfg_satisfied(self):
        with patch("install.shutil.which", return_value=None), \
             patch.object(Path, "exists", return_value=False):
            ok, label = install._requires_chat_backend(
                {"get_advice": {"backend": "openai_compat"}}, {},
            )
        self.assertTrue(ok)
        self.assertIn("openai_compat", label)

    def test_no_backend_unsatisfied(self):
        with patch("install.shutil.which", return_value=None), \
             patch.object(Path, "exists", return_value=False):
            ok, label = install._requires_chat_backend({}, {})
        self.assertFalse(ok)
        self.assertIn("no chat backend", label)


class TestRequiresLspEngine(unittest.TestCase):
    def test_missing_block_unsatisfied(self):
        ok, label = install._requires_lsp_engine({}, {})
        self.assertFalse(ok)
        self.assertIn("missing", label)

    def test_lsp_engine_disabled_still_satisfied(self):
        """The skill's role is to help enable the engine, so a
        present-but-disabled config is exactly the case it
        addresses."""
        cfg = {"hooks": {"lsp_engine": {"enabled": False}}}
        ok, label = install._requires_lsp_engine(cfg, {})
        self.assertTrue(ok)
        self.assertIn("disabled", label)

    def test_lsp_engine_enabled_satisfied(self):
        cfg = {"hooks": {"lsp_engine": {"enabled": True}}}
        ok, label = install._requires_lsp_engine(cfg, {})
        self.assertTrue(ok)
        self.assertIn("enabled", label)


# --------------------------------------------------------------------- #
# _skill_state
# --------------------------------------------------------------------- #


class TestSkillState(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_skills = self.root / "repo" / ".claude" / "skills"
        self.user_skills = self.root / "user" / ".claude" / "skills"
        self.repo_skills.mkdir(parents=True)
        self.user_skills.mkdir(parents=True)

    def _make_repo_skill(self, name: str, body: str = "version 1"):
        d = self.repo_skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(body)

    def _make_user_skill(self, name: str, body: str = "version 1"):
        d = self.user_skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(body)

    def test_standalone_skill_deps_always_ok(self):
        self._make_repo_skill("wrapup")
        spec = install.SkillSpec(name="wrapup", requires=None)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertTrue(st["src_exists"])
        self.assertFalse(st["is_installed"])
        self.assertTrue(st["deps_ok"])
        self.assertEqual(st["dep_label"], "standalone")

    def test_dep_callable_returning_false(self):
        self._make_repo_skill("consultants")

        def fake_req(cfg, tools):
            return (False, "claude-consultants missing")

        spec = install.SkillSpec(name="consultants", requires=fake_req)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertFalse(st["deps_ok"])
        self.assertIn("missing", st["dep_label"])

    def test_content_changed_branch(self):
        self._make_repo_skill("reflect", body="version 2")
        self._make_user_skill("reflect", body="version 1")
        spec = install.SkillSpec(name="reflect", requires=None)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertTrue(st["is_installed"])
        self.assertTrue(st["content_changed"])

    def test_content_same_branch(self):
        self._make_repo_skill("reflect", body="identical")
        self._make_user_skill("reflect", body="identical")
        spec = install.SkillSpec(name="reflect", requires=None)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertTrue(st["is_installed"])
        self.assertFalse(st["content_changed"])

    def test_repo_skill_missing(self):
        # No file at all in the repo dir
        spec = install.SkillSpec(name="ghost", requires=None)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertFalse(st["src_exists"])

    def test_dep_callable_raising_returns_unsatisfied(self):
        self._make_repo_skill("boomer")

        def boom(cfg, tools):
            raise RuntimeError("dep probe crashed")

        spec = install.SkillSpec(name="boomer", requires=boom)
        st = install._skill_state(spec, {}, {}, self.repo_skills, self.user_skills)
        self.assertFalse(st["deps_ok"])
        self.assertIn("dep-check error", st["dep_label"])


# --------------------------------------------------------------------- #
# _install_skills opt-in flow
# --------------------------------------------------------------------- #


class _OptInFixture(unittest.TestCase):
    """Shared fixture: a tmp HOME with empty user-skills + a tmp repo
    with controlled in-repo SKILL.md fixtures. Patches
    ``os.path.expanduser`` so ``~`` resolves into the fixture HOME, and
    patches ``install.HERE`` so the repo points at our tmp tree.

    Always passes ``non_interactive=False`` so the prompt branches are
    exercised; mocks ``builtins.input`` for the answer.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.user_skills = self.home / ".claude" / "skills"
        self.user_skills.mkdir(parents=True)

        self.repo = self.root / "repo"
        self.repo_skills = self.repo / ".claude" / "skills"
        self.repo_skills.mkdir(parents=True)

        # Stash original HERE; restore in tearDown.
        self._original_here = install.HERE
        install.HERE = self.repo
        self.addCleanup(setattr, install, "HERE", self._original_here)

    def make_repo_skill(self, name: str, body: str = "default body"):
        d = self.repo_skills / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(body)

    def make_user_skill(self, name: str, body: str = "default body"):
        d = self.user_skills / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(body)

    def run_installer(
        self,
        skills: list[install.SkillSpec],
        *,
        answers: list[str],
        cfg: dict | None = None,
        installed_tools: dict | None = None,
        non_interactive: bool = False,
        dry_run: bool = False,
    ) -> tuple[str, list]:
        """Drive ``_install_skills`` with a stubbed SKILLS list,
        canned ``input()`` answers, and a patched HOME.

        Returns ``(stdout_text, input_prompts)`` — the second tuple
        element is the list of prompt strings the mocked ``input()``
        was called with, used by tests that need to assert on prompt
        shape (mocked input() doesn't echo its prompt to stdout)."""
        input_mock = MagicMock(side_effect=answers)
        with patch.object(install, "SKILLS", skills), \
             patch.object(install.os.path, "expanduser",
                          side_effect=lambda p: p.replace("~", str(self.home))), \
             patch("builtins.input", input_mock):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                install._install_skills(
                    cfg or {},
                    installed_tools or {},
                    non_interactive=non_interactive,
                    dry_run=dry_run,
                )
        prompts = [c.args[0] for c in input_mock.call_args_list if c.args]
        return buf.getvalue(), prompts


class TestCaseA_DepsOkNotInstalled(_OptInFixture):
    """Deps satisfied + not installed → prompt 'Install? [y/N]' (default N)."""

    def test_default_n_does_not_install(self):
        self.make_repo_skill("wrapup")
        spec = install.SkillSpec(name="wrapup", requires=None,
                                  summary="session-end summary")
        out, prompts = self.run_installer([spec], answers=[""])  # empty → default N
        self.assertIn("[new]", out)
        # Prompt text is in input() arg, not stdout (mocked input)
        self.assertTrue(any("Install /wrapup?" in p for p in prompts),
                        f"Install prompt not found in: {prompts!r}")
        self.assertTrue(any("[y/N]" in p for p in prompts),
                        f"Default-N marker not found in: {prompts!r}")
        self.assertIn("Skipped", out)
        self.assertFalse((self.user_skills / "wrapup" / "SKILL.md").exists())

    def test_y_installs(self):
        self.make_repo_skill("wrapup")
        spec = install.SkillSpec(name="wrapup", requires=None)
        out, prompts = self.run_installer([spec], answers=["y"])
        self.assertIn("/wrapup installed", out)
        self.assertTrue((self.user_skills / "wrapup" / "SKILL.md").exists())

    def test_summary_shown_in_offer(self):
        self.make_repo_skill("wrapup")
        spec = install.SkillSpec(
            name="wrapup", requires=None,
            summary="session-end state summary for compaction recovery",
        )
        out, prompts = self.run_installer([spec], answers=[""])
        self.assertIn("session-end state summary", out)


class TestCaseB_DepsOkInstalled(_OptInFixture):
    """Deps satisfied + installed → prompt 'Keep? [Y/n]' (default Y).
    Content-changed → action is 'update'; content-same → no-op."""

    def test_default_y_keeps_content_same(self):
        self.make_repo_skill("reflect", body="content X")
        self.make_user_skill("reflect", body="content X")
        spec = install.SkillSpec(name="reflect", requires=None)
        out, prompts = self.run_installer([spec], answers=[""])
        self.assertTrue(any("Keep /reflect?" in p for p in prompts),
                        f"Keep prompt not found in: {prompts!r}")
        self.assertTrue(any("[Y/n]" in p for p in prompts),
                        f"Default-Y marker not found in: {prompts!r}")
        # No copy needed — content already matches.
        self.assertNotIn("/reflect updated", out)
        self.assertNotIn("/reflect installed", out)
        self.assertTrue((self.user_skills / "reflect" / "SKILL.md").exists())

    def test_default_y_updates_when_content_changed(self):
        self.make_repo_skill("reflect", body="NEW content")
        self.make_user_skill("reflect", body="OLD content")
        spec = install.SkillSpec(name="reflect", requires=None)
        out, prompts = self.run_installer([spec], answers=[""])
        self.assertIn("update available", out)
        self.assertIn("/reflect updated", out)
        self.assertEqual(
            (self.user_skills / "reflect" / "SKILL.md").read_text(),
            "NEW content",
        )

    def test_n_removes(self):
        self.make_repo_skill("reflect")
        self.make_user_skill("reflect")
        spec = install.SkillSpec(name="reflect", requires=None)
        out, prompts = self.run_installer([spec], answers=["n"])
        self.assertIn("/reflect removed", out)
        self.assertFalse((self.user_skills / "reflect").exists())


class TestCaseC_DepsLostInstalled(_OptInFixture):
    """Deps NOT satisfied + installed → prompt 'Remove? [y/N]'
    (default N). Destruction always requires explicit consent."""

    def test_default_n_keeps_skill(self):
        self.make_repo_skill("consultants")
        self.make_user_skill("consultants")

        def dep_missing(cfg, tools):
            return (False, "claude-consultants not found")

        spec = install.SkillSpec(name="consultants", requires=dep_missing)
        out, prompts = self.run_installer([spec], answers=[""])  # empty → N
        self.assertIn("installed BUT dep missing", out)
        self.assertTrue(any("Remove /consultants?" in p for p in prompts),
                        f"Remove prompt not found in: {prompts!r}")
        self.assertTrue(any("[y/N]" in p for p in prompts),
                        f"Default-N marker (destructive) not found in: {prompts!r}")
        self.assertTrue((self.user_skills / "consultants").exists())

    def test_y_removes(self):
        self.make_repo_skill("consultants")
        self.make_user_skill("consultants")

        def dep_missing(cfg, tools):
            return (False, "claude-consultants not found")

        spec = install.SkillSpec(name="consultants", requires=dep_missing)
        out, prompts = self.run_installer([spec], answers=["y"])
        self.assertIn("/consultants removed", out)
        self.assertFalse((self.user_skills / "consultants").exists())


class TestCaseD_DepsLostNotInstalled(_OptInFixture):
    """Deps NOT satisfied + not installed → warning, NO prompt at all.
    A blocked skill the user doesn't have shouldn't waste a prompt."""

    def test_warning_no_prompt(self):
        self.make_repo_skill("consultants")

        def dep_missing(cfg, tools):
            return (False, "claude-consultants not found")

        spec = install.SkillSpec(name="consultants", requires=dep_missing)
        # No prompts expected — answers=[] would raise StopIteration if
        # input() were called. The test exercising that contract is
        # the assertion below; the StopIteration would surface as a
        # test failure here.
        out, prompts = self.run_installer([spec], answers=[])
        self.assertIn("[skip]", out)
        self.assertIn("claude-consultants not found", out)
        self.assertNotIn("Install /consultants?", out)
        self.assertNotIn("Remove /consultants?", out)
        self.assertFalse((self.user_skills / "consultants").exists())


class TestNonInteractive(_OptInFixture):
    """--non-interactive preserves state: keeps installed, doesn't
    install new, doesn't destroy on dep-loss."""

    def test_skips_new_install_offer(self):
        self.make_repo_skill("wrapup")
        spec = install.SkillSpec(name="wrapup", requires=None)
        out, prompts = self.run_installer([spec], answers=[], non_interactive=True)
        self.assertIn("--non-interactive: skipping", out)
        self.assertFalse((self.user_skills / "wrapup").exists())

    def test_keeps_installed_silently(self):
        self.make_repo_skill("reflect", body="X")
        self.make_user_skill("reflect", body="X")
        spec = install.SkillSpec(name="reflect", requires=None)
        out, prompts = self.run_installer([spec], answers=[], non_interactive=True)
        # No Keep prompt
        self.assertNotIn("Keep /reflect?", out)
        # Still on disk
        self.assertTrue((self.user_skills / "reflect").exists())

    def test_updates_installed_when_content_changed(self):
        """Update preserves the user's prior opt-in choice; content
        change is the only thing different, and updating in place is
        what they implicitly want by re-running install.py."""
        self.make_repo_skill("reflect", body="NEW")
        self.make_user_skill("reflect", body="OLD")
        spec = install.SkillSpec(name="reflect", requires=None)
        out, prompts = self.run_installer([spec], answers=[], non_interactive=True)
        self.assertIn("/reflect updated", out)
        self.assertEqual(
            (self.user_skills / "reflect" / "SKILL.md").read_text(),
            "NEW",
        )

    def test_does_not_remove_on_dep_loss(self):
        """Even when the dep is gone, non-interactive never destroys.
        User must come back interactively to clean up."""
        self.make_repo_skill("consultants")
        self.make_user_skill("consultants")

        def dep_missing(cfg, tools):
            return (False, "claude-consultants not found")

        spec = install.SkillSpec(name="consultants", requires=dep_missing)
        out, prompts = self.run_installer([spec], answers=[], non_interactive=True)
        self.assertIn("keeping (re-run interactively to remove)", out)
        self.assertTrue((self.user_skills / "consultants").exists())


class TestDryRun(_OptInFixture):
    def test_dry_run_does_not_write_or_remove(self):
        self.make_repo_skill("wrapup")
        spec = install.SkillSpec(name="wrapup", requires=None)
        out, prompts = self.run_installer([spec], answers=["y"], dry_run=True)
        self.assertIn("[dry-run] Would install: wrapup", out)
        self.assertFalse((self.user_skills / "wrapup").exists())


class TestSummaryLine(_OptInFixture):
    def test_summary_aggregates_actions(self):
        # Two new offered (one accepted, one declined) + one installed
        # to keep + one with dep gone (declined removal).
        self.make_repo_skill("a"); self.make_repo_skill("b")
        self.make_repo_skill("c"); self.make_user_skill("c")
        self.make_repo_skill("d"); self.make_user_skill("d")

        def dep_missing(cfg, tools):
            return (False, "x missing")

        skills = [
            install.SkillSpec(name="a", requires=None),
            install.SkillSpec(name="b", requires=None),
            install.SkillSpec(name="c", requires=None),
            install.SkillSpec(name="d", requires=dep_missing),
        ]
        # answers: install a → y, install b → n, keep c → empty (Y),
        # remove d → empty (N, keep).
        out, prompts = self.run_installer(skills, answers=["y", "n", "", ""])
        self.assertIn("Summary:", out)
        self.assertIn("installed", out)
        self.assertIn("kept", out)


if __name__ == "__main__":
    unittest.main()
