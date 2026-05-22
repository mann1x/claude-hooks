"""Tests that install.SKILLS enumerates every skill dir in
.claude/skills/.

**Why this test exists**

Skills live as on-disk directories under ``.claude/skills/`` in the
repo. ``install.py`` only copies the skills whose names appear in
the ``SKILLS`` constant. If a contributor adds a skill dir to the
repo but forgets to add the corresponding ``("name", requires_tool)``
tuple to ``SKILLS``, the skill silently never reaches user hosts —
even though the repo, CHANGELOG, and docs may all refer to it as if
it ships.

That is exactly what happened with ``setup-compile-aware`` in the
v1.9.0 release cut: the skill landed in ``.claude/skills/`` but was
omitted from ``install.SKILLS``, so no host had ``/setup-compile-
aware`` available despite the LSP-engine docs treating it as the
canonical setup path. Caught manually after v1.9.2 deploy.

This test fixes that release-cut bug class permanently. Every
skill dir with a ``SKILL.md`` must appear in ``SKILLS``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _on_disk_skill_names() -> set[str]:
    """Return the set of skill directory names that ship in the repo,
    i.e. every subdir of ``.claude/skills/`` that contains a
    ``SKILL.md``."""
    skills_dir = REPO / ".claude" / "skills"
    if not skills_dir.exists():
        return set()
    return {
        d.name
        for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    }


def _enumerated_skill_names() -> set[str]:
    """Return the set of skill names enumerated in
    ``install.SKILLS``. v1.10.0+: entries are ``SkillSpec`` dataclass
    instances, not tuples."""
    return {spec.name for spec in install.SKILLS}


class TestSkillsManifestCompleteness:
    """The SKILLS constant must list every in-repo skill dir."""

    def test_every_repo_skill_is_in_install_manifest(self):
        """Any skill present at ``.claude/skills/<name>/SKILL.md``
        must also be in ``install.SKILLS``. Failure means a release
        cut would silently fail to ship the skill (see module
        docstring for the v1.9.0 setup-compile-aware regression)."""
        on_disk = _on_disk_skill_names()
        enumerated = _enumerated_skill_names()
        missing_from_manifest = on_disk - enumerated
        assert not missing_from_manifest, (
            f"Skills present in .claude/skills/ but missing from "
            f"install.SKILLS: {sorted(missing_from_manifest)}. "
            f"Add ('<name>', None) — or ('<name>', '<tool>') if it "
            f"requires a binary — to the SKILLS list in install.py."
        )

    def test_every_manifest_entry_has_on_disk_skill(self):
        """Symmetric guard: if a skill is enumerated in SKILLS, the
        on-disk ``.claude/skills/<name>/SKILL.md`` must exist. A
        stale enumeration would print confusing 'skipped' lines and
        suggest a skill that doesn't ship. Cleanup of removed
        skills should also remove the SKILLS entry."""
        on_disk = _on_disk_skill_names()
        enumerated = _enumerated_skill_names()
        stale_in_manifest = enumerated - on_disk
        assert not stale_in_manifest, (
            f"Skills enumerated in install.SKILLS but missing from "
            f".claude/skills/: {sorted(stale_in_manifest)}. Either "
            f"add the skill directory or remove the manifest entry."
        )

    def test_setup_compile_aware_present(self):
        """Regression guard for the original v1.9.0 omission. The
        skill must remain enumerated as long as the LSP engine ships
        as an opt-in feature."""
        assert "setup-compile-aware" in _enumerated_skill_names(), (
            "setup-compile-aware is missing from install.SKILLS — "
            "this is the exact bug pattern from v1.9.0 → v1.9.2 "
            "where the skill shipped to the repo but never to user "
            "hosts. Restore the ('setup-compile-aware', None) entry."
        )

    def test_skills_manifest_is_a_list_of_skillspec(self):
        """Structural shape guard. v1.10.0+ uses ``SkillSpec``
        dataclass entries with ``name``, optional ``requires``
        callable, and a ``summary`` string."""
        for entry in install.SKILLS:
            assert isinstance(entry, install.SkillSpec), (
                f"SKILLS entry is not a SkillSpec: {entry!r}"
            )
            assert isinstance(entry.name, str) and entry.name, (
                f"SKILLS entry has empty/non-str name: {entry!r}"
            )
            assert entry.requires is None or callable(entry.requires), (
                f"SKILLS requires must be None or callable: {entry!r}"
            )
            assert isinstance(entry.summary, str), (
                f"SKILLS summary must be a str (use '' if none): {entry!r}"
            )

    def test_skills_manifest_has_no_duplicates(self):
        """A duplicate would cause _install_skills to copy the same
        skill twice and confuse the install counter."""
        names = [spec.name for spec in install.SKILLS]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, (
            f"Duplicate skill names in install.SKILLS: {sorted(duplicates)}"
        )

    def test_every_dep_check_returns_tuple_bool_str(self):
        """v1.10.0+: each non-None ``requires`` must return
        ``(deps_ok: bool, dep_label: str)`` when called with an empty
        cfg + empty installed_tools dict. The _install_skills flow
        unpacks the tuple; a wrong return shape would crash the
        installer."""
        for spec in install.SKILLS:
            if spec.requires is None:
                continue
            result = spec.requires({}, {})
            assert isinstance(result, tuple) and len(result) == 2, (
                f"/{spec.name} requires() returned non-tuple: {result!r}"
            )
            deps_ok, dep_label = result
            assert isinstance(deps_ok, bool), (
                f"/{spec.name} requires() returned non-bool deps_ok: {deps_ok!r}"
            )
            assert isinstance(dep_label, str) and dep_label, (
                f"/{spec.name} requires() returned empty dep_label: {dep_label!r}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
