"""Deploy must stay complete — the enforcement layer, not a unit test.

On 2026-08-02 ``~/.claude/skills/consultants/SKILL.md`` was found still
at its **21 May** content: 791 lines against the repo's 1591. Ten weeks
of sessions had been loading half a skill — no wait patterns, no review
loop, no ``accept`` / ``tool-ack`` verbs — while the engine underneath
had moved several releases on. Nothing surfaced it, because a stale
skill does not error. It just instructs the model to drive something
that no longer exists.

The cause was a *routine*: "deploy" meant ``pip install -e .`` plus a
service restart, which makes the engine current and touches nothing
else. Skills are not loaded by any service, so they fell outside the
definition and drifted silently.

A routine cannot be trusted to stay complete, so these tests hold the
line instead:

* every deployable artifact class is handled by ``scripts/deploy.py``
* every one is *checked* by ``scripts/verify_deploy.py``
* the deploy script discovers rather than hardcodes, so the next
  artifact of an existing class is picked up automatically
* a failed step can never be reported as a successful deploy
* both systemd scopes are searched, because this host splits them
* ``CLAUDE.md`` tells the next session to use the script

Adding a new class of deployable artifact means adding it here first.
That is the point.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts" / "deploy.py"
VERIFY = REPO / "scripts" / "verify_deploy.py"


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestDeployScriptExists(unittest.TestCase):
    def test_there_is_a_single_deploy_entry_point(self):
        self.assertTrue(
            DEPLOY.is_file(),
            "scripts/deploy.py is the only supported deploy path; an "
            "ad-hoc 'pip install + restart' is what let a skill go stale "
            "for ten weeks",
        )

    def test_it_parses(self):
        ast.parse(_src(DEPLOY))

    def test_it_is_executable(self):
        self.assertTrue(DEPLOY.stat().st_mode & 0o111,
                        "scripts/deploy.py should be chmod +x")


class TestEveryArtifactClassIsDeployed(unittest.TestCase):
    """Each class below drifts silently when skipped — none of them
    raises at runtime when it is behind."""

    def test_packages(self):
        self.assertIn("pip", _src(DEPLOY))
        self.assertIn("install", _src(DEPLOY))

    def test_skills(self):
        src = _src(DEPLOY)
        self.assertIn("def step_skills", src)
        self.assertIn(".claude", src)
        self.assertIn("SKILL.md", src)

    def test_services(self):
        src = _src(DEPLOY)
        self.assertIn("def step_services", src)
        self.assertIn("systemctl", src)

    def test_verification_is_a_step_not_a_suggestion(self):
        src = _src(DEPLOY)
        self.assertIn("def step_verify", src)
        self.assertIn("verify_deploy.py", src)


class TestDeployDiscoversRatherThanHardcodes(unittest.TestCase):
    """A hardcoded list is how the *next* artifact gets forgotten."""

    def test_skills_are_globbed_not_listed(self):
        src = _src(DEPLOY)
        self.assertRegex(
            src, r"glob\(\s*[\"']\*/SKILL\.md[\"']\s*\)",
            "deploy.py must glob the skills directory; naming skills "
            "individually means a new one is missed",
        )

    def test_no_skill_is_named_literally_in_the_deploy_logic(self):
        # The docstring may cite the incident by name; the code may not
        # branch on one.
        tree = ast.parse(_src(DEPLOY))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.get_source_segment(_src(DEPLOY), node) or ""
            # Strip the function docstring before looking.
            if ast.get_docstring(node):
                body = body.replace(ast.get_docstring(node), "")
            self.assertNotIn(
                "consultants/SKILL", body,
                f"{node.name} hardcodes a specific skill",
            )

    def test_envs_are_discovered(self):
        src = _src(DEPLOY)
        self.assertIn("def _envs_with_package", src)

    def test_units_are_discovered_from_unit_files(self):
        src = _src(DEPLOY)
        self.assertIn("def _repo_units", src)
        self.assertIn(".service", src)

    def test_both_systemd_scopes_are_searched(self):
        # This host splits them: the consultants engine is a --user unit
        # while daemon / proxy / dashboard are system units. Knowing about
        # one scope leaves the other running old code.
        src = _src(DEPLOY)
        self.assertIn("/etc/systemd/system", src)
        self.assertIn("systemd/user", src)
        self.assertIn('"--user"', src)


class TestNoPartialSuccess(unittest.TestCase):
    """A deploy that reports success while one artifact class is behind
    is precisely the failure this replaces."""

    def test_a_failed_step_fails_the_deploy(self):
        src = _src(DEPLOY)
        self.assertIn("DEPLOY FAILED", src)
        self.assertRegex(src, r"failed\s*=\s*\[.*not s\.ok")

    def test_verification_is_skipped_when_a_step_failed(self):
        # Verifying a deploy that did not happen would report a green
        # check on a stale host.
        self.assertIn("SKIPPED (an earlier step failed)", _src(DEPLOY))

    def test_exit_code_is_nonzero_on_failure(self):
        self.assertRegex(_src(DEPLOY), r"DEPLOY FAILED[\s\S]{0,120}return 1")


class TestVerifierCoversTheSameClasses(unittest.TestCase):
    """Deploying and verifying must not disagree about what matters."""

    def test_verifier_checks_skills(self):
        self.assertIn("def check_skills", _src(VERIFY))

    def test_a_stale_skill_is_a_FAIL_not_a_warning(self):
        # WARN would scroll past. This is the exact condition that went
        # unnoticed for ten weeks.
        src = _src(VERIFY)
        block = src[src.index("def check_skills"):]
        block = block[:block.index("def main")]
        self.assertRegex(
            block, r"r\.add\(FAIL,\s*[\"']skills STALE",
            "a stale skill must FAIL verification, not warn",
        )

    def test_verifier_runs_skills_by_default(self):
        src = _src(VERIFY)
        main = src[src.index("def main"):]
        self.assertIn("check_skills(r)", main)

    def test_verifier_still_checks_the_store_and_version(self):
        main = _src(VERIFY)[_src(VERIFY).index("def main"):]
        for fn in ("check_store(r)", "check_version(r)", "check_providers(r)"):
            self.assertIn(fn, main)


class TestTheRoutineIsWrittenDown(unittest.TestCase):
    """The root cause was a habit in an assistant's head. The fix has to
    live somewhere a fresh session reads."""

    def test_claude_md_names_the_deploy_script(self):
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(
            "scripts/deploy.py", text,
            "CLAUDE.md must tell the next session that deploy means "
            "scripts/deploy.py, or the ad-hoc routine comes back",
        )

    def test_claude_md_says_deploy_is_full_deploy(self):
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8").lower()
        self.assertTrue(
            "full deploy" in text,
            "CLAUDE.md must state that deploy is a full deploy",
        )


#: Skills known to ship without YAML frontmatter. Without ``name:`` /
#: ``description:`` a file cannot appear in the skill listing at all, so
#: these are effectively un-invokable — pre-existing debt, found
#: 2026-08-02 while auditing the deploy path and deliberately NOT fixed
#: in the same change. The allowlist exists so the debt is visible and
#: cannot grow: a new skill missing frontmatter fails the suite.
_NO_FRONTMATTER_DEBT = {"consolidate", "reflect"}


def _skill_frontmatter(src: pathlib.Path) -> str | None:
    text = src.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    parts = text.split("---", 2)
    return parts[1] if len(parts) > 2 else None


class TestSkillsInRepoAreWellFormed(unittest.TestCase):
    """Cheap guard so the thing being synced is loadable at all."""

    def test_no_new_skill_ships_without_frontmatter(self):
        offenders = {
            src.parent.name
            for src in sorted((REPO / ".claude" / "skills").glob("*/SKILL.md"))
            if _skill_frontmatter(src) is None
        }
        new = offenders - _NO_FRONTMATTER_DEBT
        self.assertEqual(
            new, set(),
            f"skill(s) without YAML frontmatter: {sorted(new)} — without "
            "name:/description: they never appear in the skill listing",
        )

    def test_the_debt_list_does_not_outlive_the_debt(self):
        # When one is fixed, drop it from the allowlist rather than
        # leaving a stale exemption that would hide a regression.
        offenders = {
            src.parent.name
            for src in sorted((REPO / ".claude" / "skills").glob("*/SKILL.md"))
            if _skill_frontmatter(src) is None
        }
        stale = _NO_FRONTMATTER_DEBT - offenders
        self.assertEqual(
            stale, set(),
            f"{sorted(stale)} now has frontmatter — remove it from "
            "_NO_FRONTMATTER_DEBT",
        )

    def test_skill_dir_name_matches_the_declared_name(self):
        # A mismatch installs under one name and is invoked under
        # another — the skill silently never loads.
        for src in sorted((REPO / ".claude" / "skills").glob("*/SKILL.md")):
            head = _skill_frontmatter(src)
            if head is None:
                continue
            m = re.search(r"^name:\s*(\S+)", head, re.M)
            self.assertIsNotNone(m, f"{src.parent.name}: no name:")
            self.assertEqual(
                m.group(1), src.parent.name,
                f"{src.parent.name}: frontmatter name is {m.group(1)!r}",
            )
