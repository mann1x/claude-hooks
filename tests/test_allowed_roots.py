"""Tests for the shared allowed-root discoverer.

The discoverer is the single place that reads Claude Code's
``permissions.additionalDirectories`` settings into a runtime
allow-list. Drift between callers would silently break the sandbox
in either direction — too permissive (security regression) or too
restrictive (UX regression). Pin the behavior here.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_hooks.allowed_roots import (
    discover_allowed_roots,
    discover_allowed_roots_with_display,
    render_for_log,
)


def _write_settings(path: str, additional: list) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"additionalDirectories": additional}}, fh)


class TestDiscoverEmpty(unittest.TestCase):

    def test_no_settings_anywhere_returns_only_cwd(self):
        with tempfile.TemporaryDirectory() as cwd:
            # Point at a nonexistent settings file to avoid reading
            # whatever's in ~/.claude/settings.json on the test box.
            fake = os.path.join(cwd, ".claude", "settings.json")
            roots = discover_allowed_roots(
                cwd, settings_files=[fake],
            )
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_cwd_only_with_explicit_empty_list(self):
        with tempfile.TemporaryDirectory() as cwd:
            roots = discover_allowed_roots(cwd, settings_files=[])
            self.assertEqual(roots, [os.path.realpath(cwd)])


class TestDiscoverFromSettings(unittest.TestCase):

    def test_user_global_contributes(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            extra = os.path.join(base, "shared-lib")
            os.makedirs(cwd)
            os.makedirs(extra)
            settings = os.path.join(base, "settings.json")
            _write_settings(settings, [extra])

            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(
                roots,
                [os.path.realpath(cwd), os.path.realpath(extra)],
            )

    def test_local_settings_layered_with_global(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            a = os.path.join(base, "a")
            b = os.path.join(base, "b")
            for p in (cwd, a, b):
                os.makedirs(p)
            user_settings = os.path.join(base, "user-settings.json")
            local_settings = os.path.join(cwd, ".claude", "settings.local.json")
            _write_settings(user_settings, [a])
            _write_settings(local_settings, [b])

            roots = discover_allowed_roots(
                cwd, settings_files=[user_settings, local_settings],
            )
            self.assertEqual(
                roots,
                [
                    os.path.realpath(cwd),
                    os.path.realpath(a),
                    os.path.realpath(b),
                ],
            )

    def test_three_files_order_preserved(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            dirs = [os.path.join(base, n) for n in ("z", "a", "m")]
            for p in [cwd] + dirs:
                os.makedirs(p)
            f1 = os.path.join(base, "user.json")
            f2 = os.path.join(cwd, ".claude", "settings.json")
            f3 = os.path.join(cwd, ".claude", "settings.local.json")
            _write_settings(f1, [dirs[0]])
            _write_settings(f2, [dirs[1]])
            _write_settings(f3, [dirs[2]])
            roots = discover_allowed_roots(cwd, settings_files=[f1, f2, f3])
            self.assertEqual(
                [os.path.basename(r) for r in roots],
                ["proj", "z", "a", "m"],
            )


class TestDiscoverFromAddDirs(unittest.TestCase):

    def test_add_dirs_after_settings(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            settings_dir = os.path.join(base, "from-settings")
            cli_dir = os.path.join(base, "from-cli")
            for p in (cwd, settings_dir, cli_dir):
                os.makedirs(p)
            settings = os.path.join(base, "s.json")
            _write_settings(settings, [settings_dir])
            roots = discover_allowed_roots(
                cwd, add_dirs=[cli_dir], settings_files=[settings],
            )
            self.assertEqual(
                roots,
                [
                    os.path.realpath(cwd),
                    os.path.realpath(settings_dir),
                    os.path.realpath(cli_dir),
                ],
            )

    def test_empty_add_dirs_ok(self):
        with tempfile.TemporaryDirectory() as cwd:
            roots = discover_allowed_roots(
                cwd, add_dirs=[], settings_files=[],
            )
            self.assertEqual(roots, [os.path.realpath(cwd)])


class TestDedupAndFiltering(unittest.TestCase):

    def test_dedup_by_realpath(self):
        # Same dir listed twice + once via a symlink → one entry.
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            target = os.path.join(base, "real")
            link = os.path.join(base, "link")
            for p in (cwd, target):
                os.makedirs(p)
            os.symlink(target, link)
            settings = os.path.join(base, "s.json")
            _write_settings(settings, [target, link, target])
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(
                roots,
                [os.path.realpath(cwd), os.path.realpath(target)],
            )

    def test_nonexistent_dir_dropped(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            settings = os.path.join(base, "s.json")
            _write_settings(settings, ["/does/not/exist/here"])
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_file_not_dir_dropped(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            f = os.path.join(base, "not-a-dir.txt")
            Path(f).write_text("hi")
            settings = os.path.join(base, "s.json")
            _write_settings(settings, [f])
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_tilde_expansion(self):
        # ~/ entries expand to the test runner's HOME.
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            settings = os.path.join(base, "s.json")
            _write_settings(settings, ["~"])
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            home_real = os.path.realpath(os.path.expanduser("~"))
            self.assertIn(home_real, roots)


class TestMalformedSettings(unittest.TestCase):

    def test_invalid_json_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text("{ not valid json")
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_missing_permissions_key_ok(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text('{"other": "value"}')
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_wrong_type_for_additional_directories(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            os.makedirs(cwd)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text(
                '{"permissions": {"additionalDirectories": "not a list"}}'
            )
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(roots, [os.path.realpath(cwd)])

    def test_non_string_entries_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            cwd = os.path.join(base, "proj")
            real = os.path.join(base, "real")
            os.makedirs(cwd)
            os.makedirs(real)
            settings = os.path.join(base, "s.json")
            Path(settings).write_text(
                json.dumps({"permissions": {"additionalDirectories": [
                    real, 42, None, "", {"x": "y"}, real,
                ]}})
            )
            roots = discover_allowed_roots(cwd, settings_files=[settings])
            self.assertEqual(
                roots,
                [os.path.realpath(cwd), os.path.realpath(real)],
            )


class TestRenderForLog(unittest.TestCase):

    def test_primary_only(self):
        out = render_for_log(["/foo"])
        self.assertEqual(out, "primary: /foo")

    def test_with_extras(self):
        out = render_for_log(["/foo", "/bar", "/baz"])
        self.assertIn("primary: /foo", out)
        self.assertIn("extra:", out)
        self.assertIn("  /bar", out)
        self.assertIn("  /baz", out)

    def test_empty(self):
        self.assertEqual(render_for_log([]), "primary: (none)")

    def test_display_roots_same_as_real(self):
        # When display matches real, render just the path (no parens).
        out = render_for_log(
            ["/foo", "/bar"], display_roots=["/foo", "/bar"],
        )
        self.assertIn("primary: /foo", out)
        self.assertIn("  /bar", out)
        self.assertNotIn("(", out)

    def test_display_roots_differ_show_realpath_paren(self):
        # When display differs from real (symlink resolution), show both:
        # the user-facing form on the left, realpath in parens.
        out = render_for_log(
            ["/srv/foo", "/srv/bar"],
            display_roots=["/shared/foo", "/shared/bar"],
        )
        self.assertIn("primary: /shared/foo  (/srv/foo)", out)
        self.assertIn("  /shared/bar  (/srv/bar)", out)

    def test_display_roots_length_mismatch_ignored(self):
        # Defensive: parallel-list length must match. Mismatched =
        # fall back to realpath display (no crash).
        out = render_for_log(
            ["/foo", "/bar"], display_roots=["/shared/foo"],
        )
        self.assertIn("primary: /foo", out)
        self.assertIn("  /bar", out)


class TestDiscoverWithDisplay(unittest.TestCase):
    """``discover_allowed_roots_with_display`` keeps the user-facing
    path next to the realpath. Same shape (length / order) as
    ``discover_allowed_roots``, with one extra string per entry.
    """

    def test_cwd_only(self):
        with tempfile.TemporaryDirectory() as cwd:
            reals, displays = discover_allowed_roots_with_display(
                cwd, settings_files=[],
            )
            self.assertEqual(len(reals), 1)
            self.assertEqual(len(displays), 1)
            self.assertEqual(displays[0], cwd)  # cwd is its own display
            self.assertEqual(reals[0], os.path.realpath(cwd))

    def test_symlink_keeps_display(self):
        # Build a symlink chain: <real_dir>/sub  ←  <link_dir>/sub
        with tempfile.TemporaryDirectory() as real_root, \
             tempfile.TemporaryDirectory() as link_root:
            real_sub = os.path.join(real_root, "sub")
            os.makedirs(real_sub)
            link_sub = os.path.join(link_root, "sub")
            os.symlink(real_sub, link_sub)
            with tempfile.TemporaryDirectory() as cwd:
                settings = os.path.join(cwd, ".claude", "settings.json")
                _write_settings(settings, [link_sub])
                reals, displays = discover_allowed_roots_with_display(
                    cwd, settings_files=[settings],
                )
                # 2 entries: cwd + the symlinked dir.
                self.assertEqual(len(reals), 2)
                self.assertEqual(len(displays), 2)
                # display stays as the symlink path; realpath resolves.
                self.assertEqual(displays[1], link_sub)
                self.assertEqual(reals[1], os.path.realpath(real_sub))
                self.assertNotEqual(displays[1], reals[1])

    def test_dedup_by_realpath_still_dedups(self):
        # Two entries pointing at the same realpath via different
        # symlinks: only the first survives in both lists.
        with tempfile.TemporaryDirectory() as real_root, \
             tempfile.TemporaryDirectory() as link_a, \
             tempfile.TemporaryDirectory() as link_b:
            real_sub = os.path.join(real_root, "sub")
            os.makedirs(real_sub)
            la = os.path.join(link_a, "sub")
            lb = os.path.join(link_b, "sub")
            os.symlink(real_sub, la)
            os.symlink(real_sub, lb)
            with tempfile.TemporaryDirectory() as cwd:
                settings = os.path.join(cwd, ".claude", "settings.json")
                _write_settings(settings, [la, lb])
                reals, displays = discover_allowed_roots_with_display(
                    cwd, settings_files=[settings],
                )
                # cwd + one entry (the second symlink dedups out).
                self.assertEqual(len(reals), 2)
                self.assertEqual(len(displays), 2)
                self.assertEqual(displays[1], la)

    def test_add_dirs_carried_with_display(self):
        with tempfile.TemporaryDirectory() as cwd, \
             tempfile.TemporaryDirectory() as extra:
            reals, displays = discover_allowed_roots_with_display(
                cwd, add_dirs=[extra], settings_files=[],
            )
            self.assertEqual(len(reals), 2)
            self.assertEqual(displays[1], extra)
            self.assertEqual(reals[1], os.path.realpath(extra))

    def test_consistent_with_discover_allowed_roots(self):
        # The reals list must equal what discover_allowed_roots returns.
        with tempfile.TemporaryDirectory() as cwd, \
             tempfile.TemporaryDirectory() as extra:
            reals_only = discover_allowed_roots(
                cwd, add_dirs=[extra], settings_files=[],
            )
            reals, _ = discover_allowed_roots_with_display(
                cwd, add_dirs=[extra], settings_files=[],
            )
            self.assertEqual(reals, reals_only)


if __name__ == "__main__":
    unittest.main()
