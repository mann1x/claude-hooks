"""Tests for the GrowthBook flag pin (``claude_hooks.kairos_pin``)."""
from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claude_hooks.kairos_pin import is_pin_needed, main, pin_flags


def _write(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class PinFlagsTests(unittest.TestCase):
    def test_flips_when_flag_is_false(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            changes = list(pin_flags(path=path))
            self.assertEqual(
                changes, [("tengu_kairos_cron_durable", False, True)]
            )
            self.assertTrue(
                _read(path)["cachedGrowthBookFeatures"]["tengu_kairos_cron_durable"]
            )

    def test_no_op_when_flag_is_already_true(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": True},
            })
            mtime_before = path.stat().st_mtime_ns
            self.assertEqual(list(pin_flags(path=path)), [])
            # No write must happen when nothing changes -- the systemd
            # path watcher would trigger an infinite loop otherwise.
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_flips_when_flag_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {"cachedGrowthBookFeatures": {}})
            changes = list(pin_flags(path=path))
            self.assertEqual(
                changes, [("tengu_kairos_cron_durable", False, True)]
            )

    def test_silent_when_growthbook_section_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {"numStartups": 7})
            self.assertEqual(list(pin_flags(path=path)), [])
            self.assertEqual(_read(path), {"numStartups": 7})

    def test_silent_when_file_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent.json"
            self.assertEqual(list(pin_flags(path=path)), [])

    def test_silent_when_file_corrupt(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(list(pin_flags(path=path)), [])

    def test_custom_pins_override_default(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {
                    "tengu_kairos_cron_durable": False,
                    "some_other_flag": False,
                },
            })
            changes = list(pin_flags(
                path=path, pins={"some_other_flag": True},
            ))
            self.assertEqual(changes, [("some_other_flag", False, True)])
            data = _read(path)["cachedGrowthBookFeatures"]
            # Default pin must NOT be touched when caller passes custom pins.
            self.assertFalse(data["tengu_kairos_cron_durable"])
            self.assertTrue(data["some_other_flag"])

    def test_preserves_other_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            original = {
                "numStartups": 234,
                "cachedGrowthBookFeatures": {
                    "tengu_kairos_cron_durable": False,
                    "tengu_kairos_cron": True,
                    "unrelated_flag": "preserved",
                },
                "userID": "abc",
            }
            _write(path, original)
            list(pin_flags(path=path))
            data = _read(path)
            self.assertEqual(data["numStartups"], 234)
            self.assertEqual(data["userID"], "abc")
            self.assertTrue(data["cachedGrowthBookFeatures"]["tengu_kairos_cron"])
            self.assertEqual(
                data["cachedGrowthBookFeatures"]["unrelated_flag"], "preserved",
            )

    def test_empty_pins_dict_is_no_op(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            self.assertEqual(list(pin_flags(path=path, pins={})), [])
            self.assertFalse(
                _read(path)["cachedGrowthBookFeatures"]["tengu_kairos_cron_durable"]
            )


class IsPinNeededTests(unittest.TestCase):
    def test_returns_true_when_flag_is_false(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            self.assertTrue(is_pin_needed(path))

    def test_returns_false_when_flag_is_true(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": True},
            })
            self.assertFalse(is_pin_needed(path))

    def test_returns_false_when_file_missing(self) -> None:
        # Don't push installation onto hosts where ~/.claude.json
        # doesn't exist yet (Claude Code never run, fresh installs).
        with TemporaryDirectory() as tmp:
            self.assertFalse(is_pin_needed(Path(tmp) / "nope.json"))

    def test_returns_false_when_growthbook_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {"numStartups": 1})
            self.assertFalse(is_pin_needed(path))


class CliMainTests(unittest.TestCase):
    def test_default_flips_and_prints(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            with patch("sys.stdout", new_callable=StringIO) as out:
                rc = main(["--path", str(path)])
            self.assertEqual(rc, 0)
            self.assertIn("tengu_kairos_cron_durable", out.getvalue())
            self.assertTrue(
                _read(path)["cachedGrowthBookFeatures"]["tengu_kairos_cron_durable"]
            )

    def test_quiet_suppresses_output(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            with patch("sys.stdout", new_callable=StringIO) as out:
                rc = main(["--path", str(path), "--quiet"])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue(), "")

    def test_check_returns_1_when_pin_needed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": False},
            })
            mtime_before = path.stat().st_mtime_ns
            rc = main(["--path", str(path), "--check"])
            self.assertEqual(rc, 1)
            # --check must NOT modify the file.
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_check_returns_0_when_pin_not_needed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            _write(path, {
                "cachedGrowthBookFeatures": {"tengu_kairos_cron_durable": True},
            })
            self.assertEqual(main(["--path", str(path), "--check"]), 0)

    def test_unknown_argument_returns_2(self) -> None:
        with patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(main(["--bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
