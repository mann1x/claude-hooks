"""Tests for install.py's companion-tools detector.

v1.6.1 — the ``episodic-memory`` row is special-cased: when the host
runs in CLIENT mode (POSTs to a remote episodic server), the binary
isn't needed locally and the row should report ``n/a (CLIENT)``
instead of ``MISSING``. Mirrors the same "don't conflate on-disk
state with role" fix v1.6.1 made for the API proxy dialog.
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


def _run(cfg):
    """Drive _detect_companion_tools with shutil.which stubbed so the
    episodic-memory binary appears MISSING, and capture stdout."""
    out = io.StringIO()

    def fake_which(name):
        # Pretend nothing is installed — we want to exercise the
        # MISSING / n/a branch for episodic-memory specifically.
        return None

    with patch.object(install.shutil, "which", side_effect=fake_which), \
         patch.object(install, "_ensure_marketplace"), \
         patch.object(sys, "stdout", out):
        result = install._detect_companion_tools(cfg)
    return result, out.getvalue()


class TestEpisodicSpecialCase(unittest.TestCase):

    def test_client_mode_reports_na_not_missing(self):
        cfg = {"episodic": {"mode": "client",
                            "server_url": "http://192.168.178.2:11435"}}
        result, text = _run(cfg)
        # Row marker is [ok], status is n/a (CLIENT)
        self.assertIn("[ok] episodic-memory", text)
        self.assertIn("n/a (CLIENT)", text)
        # MUST NOT print MISSING for episodic-memory in client mode
        for line in text.splitlines():
            if "episodic-memory" in line:
                self.assertNotIn("MISSING", line)
        # Result dict reports it as found so the "install via npm"
        # hint below doesn't bait the user.
        self.assertTrue(result["episodic-memory"])

    def test_client_mode_does_not_offer_npm_install_for_episodic(self):
        cfg = {"episodic": {"mode": "client",
                            "server_url": "http://x:11435"}}
        _, text = _run(cfg)
        # The "X tool(s) can be installed via npm" block must NOT
        # mention episodic-memory (it has no npm package anyway, but
        # double-guard).
        for line in text.splitlines():
            if "npm install" in line:
                self.assertNotIn("episodic-memory", line)

    def test_server_mode_still_warns_missing(self):
        # Server-mode host with no binary installed → still MISSING.
        cfg = {"episodic": {"mode": "server"}}
        result, text = _run(cfg)
        self.assertIn("[!!] episodic-memory", text)
        self.assertIn("MISSING", text)
        self.assertFalse(result["episodic-memory"])

    def test_off_mode_still_warns_missing(self):
        # Episodic never configured → keep the MISSING warning as a
        # discovery hint (current default behavior).
        cfg = {"episodic": {"mode": "off"}}
        result, text = _run(cfg)
        self.assertIn("MISSING", text)
        self.assertFalse(result["episodic-memory"])

    def test_no_cfg_passed_still_warns_missing(self):
        # Backwards-compat: callers that don't pass cfg get the
        # legacy behavior (warn).
        result, text = _run(None)
        self.assertIn("MISSING", text)

    def test_other_tools_unaffected_by_episodic_mode(self):
        # The special case must be scoped to the episodic-memory row.
        # Other MISSING tools (mnemex, caliber, claudekit) still warn
        # in client mode.
        cfg = {"episodic": {"mode": "client",
                            "server_url": "http://x:11435"}}
        _, text = _run(cfg)
        self.assertIn("[!!] mnemex", text)
        self.assertIn("[!!] caliber", text)
        self.assertIn("[!!] claudekit", text)


if __name__ == "__main__":
    unittest.main()
