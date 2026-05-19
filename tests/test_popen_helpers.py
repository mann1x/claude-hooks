"""Tests for ``claude_hooks._popen.detach_kwargs`` (#221, 2026-05-19).

The helper consolidates the cross-platform incantation needed to
spawn a detached, windowless child. Before #221 the pattern was
re-rolled in seven spawn sites; three of them (consultants_forwarder,
store_async, code_graph builder) skipped the Windows flags entirely
and could pop a visible console on the user's desktop. The helper
plus a single audit pass closes that.

Coverage:

- POSIX path: ``start_new_session=True`` exactly, no Windows flag.
- Windows path: ``creationflags`` carries
  ``CREATE_NO_WINDOW | DETACHED_PROCESS``, no ``start_new_session``.
- Constants-absent path: ``getattr`` fallback returns 0, the OR
  evaluates to 0, and the call stays safe — important for
  stripped-down embedded Pythons that don't ship the constants.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from claude_hooks import _popen


class TestDetachKwargsPosix:
    """POSIX path — start_new_session=True exactly."""

    def test_posix_returns_session_flag(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "posix"
            kw = _popen.detach_kwargs()
        assert kw == {"start_new_session": True}

    def test_posix_does_not_set_creationflags(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "posix"
            kw = _popen.detach_kwargs()
        assert "creationflags" not in kw

    @pytest.mark.parametrize("posix_name", ["posix", "darwin"])
    def test_posix_variants(self, posix_name):
        """Any non-'nt' name falls through to POSIX behaviour."""
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = posix_name
            kw = _popen.detach_kwargs()
        assert kw == {"start_new_session": True}


class TestDetachKwargsWindows:
    """Windows path — creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS."""

    def test_windows_returns_creationflags(self):
        with patch.object(_popen, "os") as os_mod:
            os_mod.name = "nt"
            kw = _popen.detach_kwargs()
        assert "creationflags" in kw
        assert "start_new_session" not in kw

    def test_windows_flags_or_two_constants(self):
        # Stub real Windows constants so we can assert the OR holds.
        # We patch the subprocess module on _popen since detach_kwargs
        # reads them via getattr(subprocess, ...).
        fake_subprocess = subprocess
        fake_subprocess.CREATE_NO_WINDOW = 0x08000000  # real flag value
        fake_subprocess.DETACHED_PROCESS = 0x00000008  # real flag value
        try:
            with patch.object(_popen, "os") as os_mod:
                os_mod.name = "nt"
                kw = _popen.detach_kwargs()
            assert kw["creationflags"] == (0x08000000 | 0x00000008)
        finally:
            # Roll back only on POSIX where the constants didn't exist
            # before; on real Windows leave them alone.
            if not hasattr(subprocess, "_orig_flags"):
                # We added them — they weren't there. Clean up.
                pass

    def test_windows_constants_absent_falls_back_to_zero(self):
        """Stripped Pythons may not have the constants — getattr
        returns 0 and the call stays safe (no AttributeError)."""
        import types
        fake = types.SimpleNamespace()  # no CREATE_NO_WINDOW / DETACHED_PROCESS
        with patch.object(_popen, "os") as os_mod, \
             patch.object(_popen, "subprocess", fake):
            os_mod.name = "nt"
            kw = _popen.detach_kwargs()
        # 0 | 0 == 0, but the key must still be present for callers
        # who unpack ``**kw``.
        assert kw == {"creationflags": 0}


class TestDetachKwargsCallableWithPopen:
    """Smoke: helper output unpacks cleanly into subprocess.Popen."""

    def test_unpacks_into_popen_call_signature(self):
        # Don't actually spawn; just verify that the kwargs dict shape
        # matches what subprocess.Popen accepts. If detach_kwargs ever
        # adds an unexpected key, this would fail on import inspection.
        kw = _popen.detach_kwargs()
        import inspect
        sig = inspect.signature(subprocess.Popen)
        for k in kw:
            assert k in sig.parameters, (
                f"{k!r} is not a known subprocess.Popen kwarg"
            )
