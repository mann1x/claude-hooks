"""Tests for install._install_skills legacy-variant cleanup (v1.3).

v1.3 collapsed nine per-verb slash-command skills (4 get-advice + 5
consultants variants) into two dispatcher skills. Upgraders need the
old `~/.claude/skills/<variant>/` directories removed so the slash-
command menu doesn't list ghost entries. The cleanup pass in
``_install_skills`` runs unconditionally and idempotently — no-op on
fresh installs, removes on upgrade, no-op on re-runs.
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


def _seed_stale_variants(user_skills_dir: Path, *names: str) -> None:
    """Create stale variant skill dirs as if they were left behind by a
    v1.2-era install."""
    for name in names:
        d = user_skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: legacy stub\n---\nstub",
            encoding="utf-8",
        )


def test_legacy_skill_dirs_constant_covers_all_v12_variants():
    """The constant must enumerate every v1.2 variant. If we add
    new variants in the future, they must also be added here so an
    upgrade removes them — but we never expect that to happen
    (variants are an anti-pattern post-v1.3)."""
    assert install.LEGACY_SKILL_DIRS == (
        "get-advice--model", "get-advice--effort", "get-advice--tools",
        "consultants--list", "consultants--show",
        "consultants--config", "consultants--followup",
    )


def test_cleanup_removes_all_legacy_variants(tmp_path, capsys):
    """Pre-seeded stale variant dirs are removed on install."""
    user_home = tmp_path / "home"
    user_skills = user_home / ".claude" / "skills"
    user_skills.mkdir(parents=True)
    _seed_stale_variants(
        user_skills,
        "get-advice--model",
        "consultants--config",
        "consultants--followup",
    )

    with patch.object(install.os.path, "expanduser",
                      side_effect=lambda p: p.replace("~", str(user_home))):
        install._install_skills(
            {},  # empty cfg — no skills with deps configured
            installed_tools={"claude-consultants": False, "caliber": False},
            non_interactive=True,
            dry_run=False,
        )

    # The three pre-seeded variants are gone.
    assert not (user_skills / "get-advice--model").exists()
    assert not (user_skills / "consultants--config").exists()
    assert not (user_skills / "consultants--followup").exists()

    out = capsys.readouterr().out
    assert "/get-advice--model" in out
    assert "/consultants--config" in out
    assert "/consultants--followup" in out
    assert "v1.3: collapsed into parent skill" in out


def test_cleanup_is_noop_on_fresh_install(tmp_path, capsys):
    """No legacy dirs → cleanup prints nothing and removes nothing."""
    user_home = tmp_path / "home"
    user_skills = user_home / ".claude" / "skills"
    user_skills.mkdir(parents=True)

    with patch.object(install.os.path, "expanduser",
                      side_effect=lambda p: p.replace("~", str(user_home))):
        install._install_skills(
            {},  # empty cfg — no skills with deps configured
            installed_tools={"claude-consultants": False, "caliber": False},
            non_interactive=True,
            dry_run=False,
        )

    out = capsys.readouterr().out
    # No cleanup messages emitted when there's nothing to clean.
    assert "[rm]" not in out
    assert "v1.3: collapsed" not in out
    # Skills dir still exists, no ghost variants created.
    assert user_skills.exists()
    for legacy in install.LEGACY_SKILL_DIRS:
        assert not (user_skills / legacy).exists()


def test_cleanup_is_idempotent(tmp_path, capsys):
    """Running twice in a row: first run cleans, second run is silent."""
    user_home = tmp_path / "home"
    user_skills = user_home / ".claude" / "skills"
    user_skills.mkdir(parents=True)
    _seed_stale_variants(user_skills, "get-advice--effort")

    with patch.object(install.os.path, "expanduser",
                      side_effect=lambda p: p.replace("~", str(user_home))):
        install._install_skills(
            {},  # empty cfg — no skills with deps configured
            installed_tools={"claude-consultants": False, "caliber": False},
            non_interactive=True,
            dry_run=False,
        )
        first = capsys.readouterr().out
        install._install_skills(
            {},  # empty cfg — no skills with deps configured
            installed_tools={"claude-consultants": False, "caliber": False},
            non_interactive=True,
            dry_run=False,
        )
        second = capsys.readouterr().out

    assert "/get-advice--effort" in first
    assert "[rm]" not in second
    assert not (user_skills / "get-advice--effort").exists()


def test_cleanup_respects_dry_run(tmp_path, capsys):
    """--dry-run prints the planned removal but doesn't touch disk."""
    user_home = tmp_path / "home"
    user_skills = user_home / ".claude" / "skills"
    user_skills.mkdir(parents=True)
    _seed_stale_variants(user_skills, "consultants--list")

    with patch.object(install.os.path, "expanduser",
                      side_effect=lambda p: p.replace("~", str(user_home))):
        install._install_skills(
            {},  # empty cfg
            installed_tools={"claude-consultants": False, "caliber": False},
            non_interactive=True,
            dry_run=True,
        )

    out = capsys.readouterr().out
    assert "/consultants--list" in out
    assert "[rm]" in out
    # Dry-run must NOT delete.
    assert (user_skills / "consultants--list").exists()


def test_skills_list_has_only_two_dispatcher_entries_for_renamed_families():
    """SKILLS no longer registers per-verb variants; just the two
    dispatchers for get-advice and consultants."""
    names = [spec.name for spec in install.SKILLS]
    # Dispatchers present.
    assert "get-advice" in names
    assert "consultants" in names
    # No variants registered.
    for legacy in install.LEGACY_SKILL_DIRS:
        assert legacy not in names, f"{legacy} should not be in SKILLS"
