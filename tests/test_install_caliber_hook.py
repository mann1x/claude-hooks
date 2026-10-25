"""scripts/install-caliber-hook.sh merges its block; it never replaces
the hook. It used to end in `cp`, which deleted a secret gate already in
.git/hooks/pre-commit (backup_models #1940)."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("sh") is None or shutil.which("git") is None,
                                reason="needs sh and git")

GATE = ("# GITLEAKS-GATE-SENTINEL\n"
        "gitleaks git --staged || exit 1\n")


def _repo(tmp_path, hook=None):
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    for name in ("install-caliber-hook.sh", "caliber-precommit.sh"):
        shutil.copy(REPO / "scripts" / name, r / "scripts" / name)
    if hook is not None:
        (r / ".git" / "hooks" / "pre-commit").write_text(hook)
    return r


def _install(r, *args):
    return subprocess.run(["sh", "scripts/install-caliber-hook.sh", *args],
                          cwd=r, capture_output=True, text=True)


def _hook(r):
    return (r / ".git" / "hooks" / "pre-commit").read_text()


def _code(text):
    """The lines that run (comments explain the old cap by name)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _block():
    text = (REPO / "scripts" / "caliber-precommit.sh").read_text()
    return text[text.index("# caliber:pre-commit:start"):]


def test_an_existing_gate_is_kept_first_and_the_block_appended(tmp_path):
    r = _repo(tmp_path, "#!/bin/sh\n" + GATE)
    out = _install(r)
    assert out.returncode == 0, out.stderr
    hook = _hook(r)
    assert hook.startswith("#!/bin/sh\n" + GATE)
    assert hook.endswith(_block())
    assert hook.index("GITLEAKS-GATE-SENTINEL") < hook.index("# caliber:pre-commit:start")
    assert list((r / ".git" / "hooks").glob("pre-commit.bak-*"))


def test_reinstall_is_idempotent(tmp_path):
    r = _repo(tmp_path, "#!/bin/sh\n" + GATE)
    _install(r)
    first, backups = _hook(r), len(list((r / ".git" / "hooks").glob("*.bak-*")))
    out = _install(r)
    assert out.returncode == 0 and "up to date" in out.stdout
    assert _hook(r) == first
    assert len(list((r / ".git" / "hooks").glob("*.bak-*"))) == backups


@pytest.mark.parametrize("marker", ["", ":v3"])
def test_only_an_older_caliber_span_is_replaced(tmp_path, marker):
    old = (f"# caliber:pre-commit{marker}:start\n"
           "/usr/bin/timeout 30 caliber refresh\n"
           f"# caliber:pre-commit{marker}:end\n")
    r = _repo(tmp_path, "#!/bin/sh\n" + GATE + "echo before\n" + old + "echo after\n")
    assert _install(r).returncode == 0
    hook = _hook(r)
    assert "timeout 30" not in _code(hook) and hook.count("caliber:pre-commit:") == 2
    assert hook == "#!/bin/sh\n" + GATE + "echo before\n" + _block() + "echo after\n"


def test_a_missing_hook_is_created_with_a_shebang(tmp_path):
    r = _repo(tmp_path)
    assert _install(r).returncode == 0
    assert _hook(r) == "#!/bin/sh\n" + _block()
    assert os.access(r / ".git" / "hooks" / "pre-commit", os.X_OK)


def test_an_unmatched_marker_changes_nothing(tmp_path):
    broken = "#!/bin/sh\n" + GATE + "# caliber:pre-commit:start\necho x\n"
    r = _repo(tmp_path, broken)
    out = _install(r)
    assert out.returncode == 1 and "unmatched" in out.stderr
    assert _hook(r) == broken


def test_an_earlier_exit_is_warned(tmp_path):
    r = _repo(tmp_path, "#!/bin/sh\necho hi\nexit 0\n")
    out = _install(r)
    assert out.returncode == 0 and "line 3 (exit 0)" in out.stderr


def test_a_gate_below_the_block_is_warned(tmp_path):
    hook = ("#!/bin/sh\n" + _block() +
            "# >>> claude-hooks secret gate (x) >>>\nexit 1\n")
    r = _repo(tmp_path, hook.replace("# caliber:pre-commit:start",
                                     "# caliber:pre-commit:start\n# old"))
    out = _install(r)
    assert "runs after the caliber block" in out.stderr


def test_the_shipped_block_has_no_wall_clock_cap():
    text = (REPO / "scripts" / "caliber-precommit.sh").read_text()
    assert "timeout" not in _code(text) and "CALIBER_CLAUDE_CLI_TIMEOUT_MS=3600000" in text
    assert "CALIBER_STREAM_INACTIVITY_TIMEOUT_MS=600000" in text
    assert "quiet refresh recorded nothing" not in text      # no bug-917 instrumentation


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_the_real_secret_gate_still_refuses_after_install(tmp_path):
    """End to end: secret_scan.py's gate, then the caliber installer, then
    a staged fake key — the hook still exits 1, before caliber runs."""
    import importlib.util
    r = _repo(tmp_path)
    shutil.copy(REPO / "scripts" / "secret_scan.py", r / "scripts" / "secret_scan.py")
    shutil.copy(REPO / ".gitleaks.toml", r / ".gitleaks.toml")
    spec = importlib.util.spec_from_file_location("ss_gate", r / "scripts" / "secret_scan.py")
    ss = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ss)
    assert ss.install_hook(r / ".git") == "installed"
    assert _install(r).returncode == 0
    hook = _hook(r)
    assert hook.index(ss.HOOK_MARK) < hook.index("# caliber:pre-commit:start")
    (r / "creds.toml").write_text(f'api_key = "{secrets.token_urlsafe(24)}"\n')
    subprocess.run(["git", "-C", str(r), "add", "creds.toml"], check=True)
    env = dict(os.environ, PATH=os.environ["PATH"])
    run = subprocess.run(["sh", ".git/hooks/pre-commit"], cwd=r, env=env,
                         capture_output=True, text=True)
    assert run.returncode == 1
    assert "caliber: refreshing" not in run.stdout
