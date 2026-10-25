"""The commit secret gate (scripts/secret_scan.py + .gitleaks.toml).

c2f2caa committed the pgvector DSN with its password and nothing
stopped it: no gate ran, and gitleaks' stock rules scan that commit
clean. Every canary below is a shape a config file carries; secret
values are generated per run so this file holds none.
"""
from __future__ import annotations

import importlib.util
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("secret_scan",
                                               REPO / "scripts" / "secret_scan.py")
ss = importlib.util.module_from_spec(_spec)
sys.modules["secret_scan"] = ss
_spec.loader.exec_module(ss)

GITLEAKS = ss.find_gitleaks()


def _val(n=24):
    return secrets.token_urlsafe(n)[:n]


# ------------------------------------------------------------ the rules
def _staged_findings(tmp_path, files: dict) -> dict:
    """gitleaks --staged over a throw-away repo holding ``files``:
    ``{path: {rule ids}}``."""
    import json
    repo = tmp_path / "r"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["init", "-q"], check=True)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(git + ["add", "-A", "-f"], check=True)
    report = tmp_path / "report.json"
    subprocess.run([GITLEAKS, "git", "--staged", "--config",
                    str(REPO / ".gitleaks.toml"), "--no-banner", "-f", "json",
                    "-r", str(report), "--exit-code", "0", str(repo)],
                   check=True, capture_output=True)
    out: dict = {}
    for f in json.loads(report.read_text() or "[]"):
        out.setdefault(f["File"], set()).add(f["RuleID"])
    return out


needs_gitleaks = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks not installed")


@needs_gitleaks
def test_every_config_shape_of_a_secret_is_caught(tmp_path):
    pw = _val()
    leaks = {
        # the c2f2caa leak itself: a DSN in a JSON config backup
        "config/claude-hooks.json.bak-x": f'{{"dsn": "postgresql://claude:{pw}@127.0.0.1:5432/memory"}}\n',
        "app/settings.toml": f'pgvector_dsn = "postgresql://claude:{_val()}@db:5432/m"\n',
        "svc/broker.yaml": f"url: amqp://bot:{_val()}@rabbit:5672/\n",
        "code/conn.py": f'DSN = ("postgresql://claude:{_val()}@"\n       "host:5432/db")\n',
        "a/creds.toml": f'password = "{_val()}"\n',
        "b/conf.json": f'{{"api_key": "{_val()}", "client_secret": "{_val()}"}}\n',
        "c/conf.yml": f"auth_token: {_val()}\n",
        "d/app.ini": f"[db]\npasswd = {_val()}\n",
        "e/.env": f"OLLAMA_API_KEY={_val(40)}\n",
        "f/unit.service": f"Environment=SERVICE_TOKEN={_val()}\n",
        "g/x.conf": f"secret_key: '{_val()}'\n",
        "keys/deploy.pem": "-----BEGIN " + "PRIVATE KEY-----\n",   # split: the default rule reads this file
        "home/ollama-api-key": f"{_val()}\n",
        ".claude-hooks/consultants.toml": "effort = 'high'\n",
    }
    found = _staged_findings(tmp_path, leaks)
    assert set(found) == set(leaks), sorted(set(leaks) - set(found))
    # each of our rules catches its own shapes, not only by overlap with
    # the defaults or each other
    url, cfg, path = "url-credentials", "config-secret-value", "local-config-or-key-file"
    expected = {
        "config/claude-hooks.json.bak-x": {url, path},
        "app/settings.toml": {url}, "svc/broker.yaml": {url}, "code/conn.py": {url},
        "a/creds.toml": {cfg}, "b/conf.json": {cfg}, "c/conf.yml": {cfg},
        "d/app.ini": {cfg}, "e/.env": {path}, "f/unit.service": {cfg},
        "g/x.conf": {cfg}, "keys/deploy.pem": {path},
        "home/ollama-api-key": {path}, ".claude-hooks/consultants.toml": {path},
    }
    missing = {f: sorted(r - found.get(f, set())) for f, r in expected.items()
               if not r <= found.get(f, set())}
    assert not missing, missing


@needs_gitleaks
def test_placeholders_and_ordinary_config_pass(tmp_path):
    clean = {
        "config/claude-hooks.example.json": '{"dsn": "postgresql://claude:<password>@host:5432/memory"}\n',
        ".claude-hooks/consultants.example.toml": 'pgvector_dsn = "postgresql://user:YOUR_PASSWORD@host/db"\n',
        "docs/runbook.md": "export DSN=postgresql://claude:${PGPASSWORD}@127.0.0.1/memory\n",
        "a/budget.toml": "max_tokens = 32768\nnum_predict_tokens = 4096\n",
        "b/conf.toml": 'key_file = "~/.claude/ollama-api-key"\napi_key = ""\n',
        "c/conf.json": '{"token": "${GITHUB_TOKEN}", "url": "https://ollama.com/api"}\n',
        "d/conf.yaml": "password: changeme\nsecret: <your-secret>\n",
        "tests/test_x.py": 'dsn = "postgresql://claude:test-only-not-a-real-password@h/db"\n',
        ".env.example": "OLLAMA_API_KEY=\n",
    }
    assert _staged_findings(tmp_path, clean) == {}


@needs_gitleaks
def test_the_history_scan_passes_with_the_accepted_findings_only():
    """Every finding in public history is listed in .gitleaksignore; a
    history scan that fails means a new one landed."""
    rc = subprocess.run([sys.executable, str(REPO / "scripts" / "secret_scan.py"),
                         "--history"], capture_output=True).returncode
    assert rc == 0


# ------------------------------------------------------- host secrets
def test_host_secrets_come_from_configs_key_files_and_env(tmp_path):
    pw, key, tok, envv = _val(), _val(40), _val(), _val(30)
    kf = tmp_path / "jev.key"
    kf.write_text(key + "\n")
    cfg = tmp_path / "consultants.toml"
    cfg.write_text(f'pgvector_dsn = "postgresql://claude:{pw}@127.0.0.1/m"\n'
                   f'key_file = "{kf}"\n'
                   f'[mcp]\nauthorization = "Bearer {tok}"\n'
                   'max_tokens = 32768\nmodel = "gemma4:31b-cloud"\n')
    pgp = tmp_path / "vendor.key"
    pgp.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    got = dict((v, where) for where, v in ss.known_secrets(
        [cfg], [str(tmp_path / "*.key")],
        {"HF_TOKEN": envv, "PATH": "/usr/bin", "MAX_TOKENS_X": "12"}))
    assert set(got) == {pw, key, tok, envv}
    assert "jev.key" in got[key] and got[envv] == "$HF_TOKEN"


def test_a_run_of_a_host_secret_is_found_and_never_printed(tmp_path, capsys):
    pw = _val(40)
    sec = [("~/.claude/consultants-config.toml", pw)]
    lines = [("a.py", 1, "nothing here"),
             ("tests/t.py", 7, f'x = "{pw[:32]}"'),         # the 781ea3a prefix
             ("b.json", 3, f'"p": "{pw[10:22]}"'),          # 12 characters
             ("c.txt", 9, pw[:11])]                         # too short a run
    assert ss.find_known(lines, sec) == [
        ("tests/t.py", 7, "~/.claude/consultants-config.toml"),
        ("b.json", 3, "~/.claude/consultants-config.toml")]


def test_added_lines_track_files_and_line_numbers():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,0 +5,2 @@\n+one\n+two\n"
            "diff --git a/y b/y\n--- /dev/null\n+++ b/y\n@@ -0,0 +1 @@\n+three\n")
    assert list(ss.added_lines(diff)) == [("x", 5, "one"), ("x", 6, "two"),
                                          ("y", 1, "three")]


def test_the_staged_gate_refuses_a_host_secret(tmp_path, monkeypatch, capsys):
    pw = _val(40)
    monkeypatch.setattr(ss, "known_secrets", lambda: [("~/x.key", pw)])
    diff = f"+++ b/notes.md\n@@ -0,0 +1 @@\n+the pw is {pw}\n"
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, diff, ""))
    assert ss.check_known_staged() == 1
    err = capsys.readouterr().err
    assert "notes.md:1" in err and "~/x.key" in err and pw not in err


def test_missing_gitleaks_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(ss, "find_gitleaks", lambda: None)
    assert ss.run_gitleaks("staged") == 2
    assert "refused" in capsys.readouterr().err


# ---------------------------------------------------------------- hook
def test_the_hook_is_installed_once_after_the_shebang(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho caliber\n")
    assert ss.install_hook(tmp_path) == "installed"
    text = (hooks / "pre-commit").read_text()
    assert text.startswith("#!/bin/sh\n# >>> " + ss.HOOK_MARK)
    assert text.index("--staged || exit 1") < text.index("echo caliber")
    assert ss.install_hook(tmp_path) == "current"
    assert (hooks / "pre-commit").read_text() == text
    assert ss.hook_installed(tmp_path)


def test_a_missing_hook_is_created(tmp_path):
    assert ss.install_hook(tmp_path) == "installed"
    assert (tmp_path / "hooks" / "pre-commit").read_text().startswith("#!/bin/sh\n")


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_the_hook_block_fails_the_commit_when_the_gate_fails(tmp_path):
    """The block is real shell: a failing gate exits non-zero."""
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "secret_scan.py").write_text("import sys; sys.exit(1)\n")
    script = "#!/bin/sh\n" + ss.HOOK_BLOCK + "echo reached\n"
    out = subprocess.run(["sh", "-c", script], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 1 and "reached" not in out.stdout


def test_an_older_hook_block_is_replaced_in_place(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    old = (f"#!/bin/sh\n# >>> {ss.HOOK_MARK} (old) >>>\npython scripts/x.py\n"
           f"{ss.HOOK_END}\necho caliber\n")
    (hooks / "pre-commit").write_text(old)
    assert ss.install_hook(tmp_path) == "updated"
    text = (hooks / "pre-commit").read_text()
    assert text == "#!/bin/sh\n" + ss.HOOK_BLOCK + "echo caliber\n"
    assert ss.install_hook(tmp_path) == "current"
