"""Tests for ``install._install_proxy_windows`` (Windows scheduled task for the proxy).

Behavior tests target ``_install_proxy_windows_steps`` directly (which
assumes Windows) so we don't have to globally mutate ``os.name`` --
that breaks pathlib's class selector.
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


@pytest.fixture
def fake_pyw(tmp_path, monkeypatch):
    """Pretend pythonw.exe exists at a known path."""
    pyw = tmp_path / "pythonw.exe"
    pyw.write_bytes(b"")
    monkeypatch.setattr(install, "find_conda_env_pythonw", lambda: pyw)
    return pyw


class _XmlWriterRecord:
    """Holds the (path, rendered_text) tuples captured by stub_xml_writer.
    Text is captured in-memory so assertions still work after the
    installer's finally-block has deleted the on-disk file."""

    def __init__(self):
        self.calls: list[dict] = []

    def __bool__(self):
        return bool(self.calls)

    def __getitem__(self, idx):
        return self.calls[idx]

    @property
    def last_text(self) -> str:
        return self.calls[-1]["text"]


@pytest.fixture
def stub_xml_writer(tmp_path, monkeypatch):
    """Replace _write_proxy_task_xml so we don't touch tempfile / whoami."""
    record = _XmlWriterRecord()

    def _writer(*, command, arguments, workdir):
        path = tmp_path / f"proxy-task-{len(record.calls)}.xml"
        text = f"command={command}\narguments={arguments}\nworkdir={workdir}\n"
        path.write_text(text, encoding="utf-8")
        record.calls.append({"path": path, "text": text,
                              "command": command,
                              "arguments": arguments,
                              "workdir": workdir})
        return path

    monkeypatch.setattr(install, "_write_proxy_task_xml", _writer)
    return record


# ---------------------------------------------------------------- #
# OS gate (outer entry)
# ---------------------------------------------------------------- #
class TestOuterOsGate:
    def test_no_op_on_posix(self, capsys):
        # We're running on Linux, so the outer function returns
        # without calling steps. Patch steps to ensure it isn't called.
        with patch.object(install, "_install_proxy_windows_steps") as steps:
            install._install_proxy_windows(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=False,
            )
        steps.assert_not_called()
        assert "scheduled task" not in capsys.readouterr().out.lower()


# ---------------------------------------------------------------- #
# Config gate (steps fn)
# ---------------------------------------------------------------- #
class TestConfigGate:
    def test_no_op_when_proxy_disabled(self, capsys):
        with patch.object(install, "_run_schtasks_elevated") as m:
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": False}},
                non_interactive=True, dry_run=False,
            )
        m.assert_not_called()

    def test_dry_run_skips_schtasks(self, capsys):
        with patch.object(install, "_run_schtasks_elevated") as m:
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=True,
            )
        m.assert_not_called()
        assert "dry-run" in capsys.readouterr().out


# ---------------------------------------------------------------- #
# Install path (steps fn)
# ---------------------------------------------------------------- #
class TestInstall:
    def test_creates_and_runs_when_task_absent(
        self, fake_pyw, stub_xml_writer, capsys,
    ):
        with patch.object(install, "_windows_task_exists", return_value=False), \
             patch.object(install, "_run_schtasks_elevated", return_value=True) as run, \
             patch.object(install, "_print_proxy_post_install_hint") as hint:
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=False,
            )

        # Two elevated calls expected: /Create then /Run.
        verbs = [c.args[0].split()[0] for c in run.call_args_list]
        assert verbs == ["/Create", "/Run"]
        # XML was actually written for /Create.
        assert stub_xml_writer
        # Post-install reminder triggered.
        hint.assert_called_once()

    def test_already_exists_leave_as_is(
        self, fake_pyw, stub_xml_writer, capsys,
    ):
        with patch.object(install, "_windows_task_exists", return_value=True), \
             patch.object(install, "_run_schtasks_elevated") as run, \
             patch.object(install, "_print_proxy_post_install_hint") as hint:
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=False,
            )

        # Non-interactive default for "already exists" prompt is "n".
        run.assert_not_called()
        hint.assert_called_once()

    def test_pythonw_missing_falls_back_to_cmd_shim(
        self, monkeypatch, stub_xml_writer, capsys,
    ):
        monkeypatch.setattr(install, "find_conda_env_pythonw", lambda: None)

        with patch.object(install, "_windows_task_exists", return_value=False), \
             patch.object(install, "_run_schtasks_elevated", return_value=True), \
             patch.object(install, "_print_proxy_post_install_hint"):
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=False,
            )

        xml_text = stub_xml_writer.last_text
        assert "claude-hooks-proxy.cmd" in xml_text
        assert "pythonw" not in xml_text

    def test_create_failure_does_not_call_run(
        self, fake_pyw, stub_xml_writer, capsys,
    ):
        # _run_schtasks_elevated returns False on /Create -- helper
        # must short-circuit and not attempt /Run.
        def _elevated(argstr, argv):
            return not argstr.startswith("/Create")

        with patch.object(install, "_windows_task_exists", return_value=False), \
             patch.object(install, "_run_schtasks_elevated", side_effect=_elevated) as run, \
             patch.object(install, "_print_proxy_post_install_hint") as hint:
            install._install_proxy_windows_steps(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=False,
            )

        # Only one call (the /Create that failed); /Run skipped.
        assert run.call_count == 1
        # On /Create failure the post-install hint still prints --
        # we acknowledge the install was incomplete via the schtasks
        # failure message rather than suppressing the BASE_URL pointer.
        # Actually: re-check the helper -- it prints hint ONLY on the
        # success path. So hint should NOT be called here.
        hint.assert_not_called()
