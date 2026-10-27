"""Plan §8: the host's Ollama URL — one resolver, no shipped host."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks import ollama_upstream as ou

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("CALIBER_GROUNDING_UPSTREAM", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    # Never read this host's real config/claude-hooks.json.
    monkeypatch.setattr(ou, "_host_config", lambda: {})


def test_default_is_ollamas_own():
    assert ou.resolve() == ("http://localhost:11434", "default")


def test_host_config_names_the_server():
    cfg = {"ollama": {"url": "http://gpu-box:11433/"}}
    assert ou.resolve(cfg) == ("http://gpu-box:11433", "config ollama.url")
    monkeypatch_cfg = {"ollama": {"url": ""}}
    assert ou.resolve(monkeypatch_cfg)[1] == "default"


def test_env_overrides_win_in_order(monkeypatch):
    cfg = {"ollama": {"url": "http://gpu-box:11433"}}
    monkeypatch.setenv("OLLAMA_HOST", "http://other:11434")
    assert ou.resolve(cfg) == ("http://other:11434", "env OLLAMA_HOST")
    monkeypatch.setenv("CALIBER_GROUNDING_UPSTREAM", "http://proxy:11433/v1/")
    assert ou.resolve(cfg) == ("http://proxy:11433",
                               "env CALIBER_GROUNDING_UPSTREAM")


def test_a_bind_address_is_not_a_server(monkeypatch):
    # pandorum sets OLLAMA_HOST=0.0.0.0 for its local daemon.
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    cfg = {"ollama": {"url": "http://gpu-box:11433"}}
    assert ou.resolve(cfg)[1] == "config ollama.url"


def test_callers_share_the_resolver(monkeypatch):
    from claude_hooks.caliber_proxy import ollama as cal
    from claude_hooks.get_advice import ctx_probe
    from consultants.server.runner import default_ollama_url
    monkeypatch.setattr(ou, "_host_config",
                        lambda: {"ollama": {"url": "http://gpu-box:11433"}})
    assert {cal.default_upstream(), ctx_probe.base_url_default(),
            default_ollama_url()} == {"http://gpu-box:11433"}


def test_main_prints_for_the_shims(capsys):
    ou.main([])
    assert capsys.readouterr().out.strip() == "http://localhost:11434"
    ou.main(["--explain"])
    assert capsys.readouterr().out.splitlines() == [
        "http://localhost:11434  (default)",
        "ollama cloud key: not set  (none)"]


def test_install_prompt_defaults_to_the_current_value(monkeypatch):
    sys.path.insert(0, str(REPO))
    import install
    cfg = {"ollama": {"url": "http://gpu-box:11433"}}
    monkeypatch.setattr("builtins.input", lambda _p: "")
    install._setup_ollama_upstream(cfg, non_interactive=False)
    assert cfg["ollama"]["url"] == "http://gpu-box:11433"
    monkeypatch.setattr("builtins.input", lambda _p: "http://new:11434/v1")
    install._setup_ollama_upstream(cfg, non_interactive=False)
    assert cfg["ollama"]["url"] == "http://new:11434"
    fresh: dict = {}
    install._setup_ollama_upstream(fresh, non_interactive=True)
    assert not fresh["ollama"].get("url")


def test_install_asks_for_the_cloud_key_file(monkeypatch, tmp_path):
    sys.path.insert(0, str(REPO))
    import install
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    kf = tmp_path / "k"
    cfg: dict = {}
    monkeypatch.setattr("builtins.input", lambda _p: str(kf))
    install._setup_ollama_cloud_key(cfg, non_interactive=False)
    assert "api_key_file" not in cfg.get("ollama", {})
    kf.write_text("test-only-not-a-real-key\n")
    install._setup_ollama_cloud_key(cfg, non_interactive=False)
    assert cfg["ollama"]["api_key_file"] == str(kf)
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("asked"))
    install._setup_ollama_cloud_key(cfg, non_interactive=False)
    fresh: dict = {}
    install._setup_ollama_cloud_key(fresh, non_interactive=True)
    assert fresh == {}


def test_verify_warns_when_hooks_name_a_server_the_engines_miss(monkeypatch):
    sys.path.insert(0, str(REPO / "scripts"))
    import verify_deploy as vd
    import claude_hooks.config as chc
    monkeypatch.setattr(chc, "load_config", lambda: {
        "reflect": {"ollama_url": "http://gpu-box:11433/api/generate"}})
    monkeypatch.setattr(vd.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    r = vd.Results(quiet=True)
    vd.check_ollama_upstream(r)
    rows = {name: (status, detail) for status, name, detail in r.rows}
    assert rows["ollama.url"][0] == vd.WARN
    assert "gpu-box" in rows["ollama.url"][1]
    assert rows["ollama http://localhost:11434"][0] == vd.WARN


#: Shipped code that may still spell a LAN address: none. Docs, tests,
#: benchmark records and the changelog describe hosts; code never
#: defaults to one.
_LAN = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")
_SHIPPED = ("claude_hooks/", "consultants/", "scripts/", "bin/", "systemd/",
            "config/", "install.py", "patches/")


def test_no_lan_address_in_shipped_code():
    files = subprocess.run(["git", "-C", str(REPO), "ls-files"], check=True,
                           capture_output=True, text=True).stdout.split()
    hits = []
    for rel in files:
        if not rel.startswith(_SHIPPED) or rel.endswith(".md") \
                or ".bak." in rel:
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _LAN.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "host addresses in shipped code:\n" + "\n".join(hits)
