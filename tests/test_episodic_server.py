"""episodic-server must report a dead CLI as dead.

On solidpc, from 2026-09-14 to 09-26, every ``episodic-memory`` call threw
(better-sqlite3 built for Node 22, Node on 26) while ``/health`` said
``ok`` and ``/stats`` answered 200 with an empty body. These pin the
contract that replaced that: a non-zero CLI exit is a 502 carrying
stderr, and ``/health`` runs the CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "episodic_server"))
sys.path.insert(0, str(REPO / "scripts"))

import server as es  # noqa: E402
import episodic_doctor as doctor  # noqa: E402
import install  # noqa: E402

ABI_ERR = ("Error: The module 'better_sqlite3.node' was compiled against a "
           "different Node.js version using NODE_MODULE_VERSION 127. This "
           "version of Node.js requires NODE_MODULE_VERSION 147.\n"
           "  code: 'ERR_DLOPEN_FAILED'")


def _cp(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["episodic-memory"], rc, out, err)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    es._probe_cache.clear()
    yield
    es._probe_cache.clear()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    a = tmp_path / "conversation-archive"
    a.mkdir()
    db = tmp_path / "conversation-index" / "db.sqlite"
    db.parent.mkdir()
    db.write_bytes(b"")
    monkeypatch.setattr(es, "DEFAULT_ARCHIVE", a)
    monkeypatch.setattr(es, "INDEX_DB", db)
    return a


# ------------------------------------------------------------------ #
# health / probe
# ------------------------------------------------------------------ #
def test_health_is_degraded_when_the_cli_fails(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(1, err=ABI_ERR)):
        code, body = es.health()
    assert code == 503
    assert body["status"] == "degraded"
    assert body["archive_exists"] is True   # the old check, still true
    assert body["cli_ok"] is False
    assert "NODE_MODULE_VERSION 147" in body["stderr"]
    assert "episodic_doctor.py --rebuild" in body["hint"]


def test_health_is_ok_when_the_cli_runs(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(0, out="27674 conversations")):
        code, body = es.health()
    assert (code, body["status"], body["cli_ok"]) == (200, "ok", True)
    assert body["index_age_hours"] is not None


def test_probe_is_cached_until_it_expires_or_fresh_is_asked(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(0)) as run:
        es.probe(now=1000.0)
        es.probe(now=1000.0 + es.HEALTH_PROBE_TTL_S - 1)
        assert run.call_count == 1
        es.probe(now=1000.0 + es.HEALTH_PROBE_TTL_S + 1)
        assert run.call_count == 2
        es.probe(fresh=True, now=1000.0 + es.HEALTH_PROBE_TTL_S + 2)
        assert run.call_count == 3


def test_a_missing_binary_is_degraded_not_a_crash(archive):
    with mock.patch.object(es, "_run_cli", side_effect=FileNotFoundError("episodic-memory")):
        code, body = es.health()
    assert code == 503 and "cannot run" in body["error"]


def test_stderr_is_tailed():
    body = es.cli_failure(_cp(1, err="x" * 10_000 + "END"))
    assert len(body["stderr"]) == es.STDERR_TAIL_CHARS
    assert body["stderr"].endswith("END")
    assert "hint" not in body


# ------------------------------------------------------------------ #
# HTTP: a failed CLI is never a 200
# ------------------------------------------------------------------ #
@pytest.fixture
def http(archive):
    srv = HTTPServer(("127.0.0.1", 0), es.EpisodicHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("path", ["/stats", "/search?q=bcache"])
def test_a_failed_cli_is_a_502_with_stderr(http, path):
    with mock.patch.object(es, "_run_cli", return_value=_cp(1, err=ABI_ERR)):
        code, body = _get(http + path)
    assert code == 502
    assert body["returncode"] == 1
    assert "ERR_DLOPEN_FAILED" in body["stderr"]


def test_search_still_parses_a_good_answer(http):
    out = ('1. [claude-hooks, 2026-09-20] - 87% match\n'
           '   "bcache superblock"\n   Lines 10-20 in x.jsonl\n')
    with mock.patch.object(es, "_run_cli", return_value=_cp(0, out=out)):
        code, body = _get(http + "/search?q=bcache")
    assert code == 200 and body["count"] == 1
    assert body["results"][0]["match_pct"] == 87


def test_health_endpoint_status_codes(http):
    with mock.patch.object(es, "_run_cli", return_value=_cp(1, err=ABI_ERR)):
        assert _get(http + "/health")[0] == 503
    with mock.patch.object(es, "_run_cli", return_value=_cp(0)):
        # cached failure until fresh=1
        assert _get(http + "/health")[0] == 503
        assert _get(http + "/health?fresh=1")[0] == 200


# ------------------------------------------------------------------ #
# install.py: a relocated archive stays writable
# ------------------------------------------------------------------ #
needs_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="symlinks need a privilege on Windows; the unit is Linux-only")


@needs_symlinks
def test_rw_paths_include_the_resolved_target_of_a_symlinked_archive(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".config").mkdir()
    spool = tmp_path / "spool" / "superpowers"
    spool.mkdir(parents=True)
    (home / ".config" / "superpowers").symlink_to(spool)
    paths = install._episodic_rw_paths(home)
    assert str(home / ".config" / "superpowers") in paths
    assert str(spool.resolve()) in paths
    assert str(home / ".claude") in paths
    assert len(paths) == len(set(paths))


@needs_symlinks
def test_render_writes_every_rw_path_into_the_unit(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    spool = tmp_path / "spool"
    spool.mkdir()
    (home / ".config").mkdir()
    (home / ".config" / "superpowers").symlink_to(spool)
    template = (REPO / "episodic_server" / "episodic-server.service").read_text()
    unit = install._render_episodic_unit(template, repo=Path("/r"), host="h",
                                         port=7, home=home)
    rw = [ln for ln in unit.splitlines() if ln.startswith("ReadWritePaths=")]
    assert len(rw) == 1
    assert str(spool.resolve()) in rw[0] and "/var/log" in rw[0]
    assert "/root/" not in unit.replace(str(home), "")
    assert "--host h --port 7" in unit and "__" not in unit


def test_missing_paths_counts_drop_ins(tmp_path):
    unit = tmp_path / "episodic-server.service"
    unit.write_text("[Service]\nReadWritePaths=/a /b /var/log\n")
    assert install._episodic_unit_missing_paths(unit, ["/a", "/c"]) == ["/c"]
    d = tmp_path / "episodic-server.service.d"
    d.mkdir()
    (d / "override.conf").write_text("[Service]\nReadWritePaths=-/c\n")
    assert install._episodic_unit_missing_paths(unit, ["/a", "/c"]) == []


# ------------------------------------------------------------------ #
# episodic_doctor
# ------------------------------------------------------------------ #
def test_doctor_checks_only_host_built_modules(tmp_path):
    nm = tmp_path / "node_modules"
    keep = nm / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node"
    skip = [
        nm / "better-sqlite3" / "build" / "Release" / "obj.target" / "better_sqlite3.node",
        nm / "better-sqlite3" / "build" / "Release" / "test_extension.node",
        nm / "onnxruntime-node" / "bin" / "napi-v3" / "darwin" / "arm64" / "x.node",
    ]
    for p in [keep, *skip]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    assert doctor.native_modules(tmp_path) == [keep]


def test_doctor_rebuild_env_keeps_the_static_runtime():
    env = doctor.rebuild_env("cc", "c++", base={"LDFLAGS": "-L/x"})
    assert env["CC"] == "cc" and env["CXX"] == "c++"
    assert env["LDFLAGS"] == "-L/x " + doctor.STATIC_RUNTIME
    again = doctor.rebuild_env("cc", "c++", base=env)
    assert again["LDFLAGS"].count("-static-libstdc++") == 1


def test_doctor_prefers_a_compiler_that_can_do_cxx20(monkeypatch):
    monkeypatch.delenv("CXX", raising=False)
    monkeypatch.setattr(doctor, "conda_cxx_candidates",
                        lambda: ["/c/bin/x86_64-conda-linux-gnu-g++"])
    majors = {"g++": 10, "/c/bin/x86_64-conda-linux-gnu-g++": 11}
    monkeypatch.setattr(doctor, "gcc_major", lambda cxx: majors.get(cxx))
    assert doctor.pick_compiler() == ("/c/bin/x86_64-conda-linux-gnu-gcc",
                                      "/c/bin/x86_64-conda-linux-gnu-g++")
    majors["/c/bin/x86_64-conda-linux-gnu-g++"] = 9
    assert doctor.pick_compiler() is None
