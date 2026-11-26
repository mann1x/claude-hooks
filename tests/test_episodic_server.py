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
import time
import urllib.error
import urllib.request
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
    _wait_for_probe()
    es._probe_cache.clear()


def _wait_for_probe(timeout: float = 5.0) -> None:
    """A background probe must finish inside the test that mocked the CLI,
    or it would run the real one."""
    assert es._probe_running.acquire(timeout=timeout), "probe thread still running"
    es._probe_running.release()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    a = tmp_path / "conversation-archive"
    a.mkdir()
    db = tmp_path / "conversation-index" / "db.sqlite"
    db.parent.mkdir()
    db.write_bytes(b"")
    monkeypatch.setattr(es, "DEFAULT_ARCHIVE", a)
    monkeypatch.setattr(es, "INDEX_DB", db)
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(es, "SYNC_LOG", logs / "episodic-memory.log")
    monkeypatch.setattr(es, "SYNC_LOCK", logs / "episodic-memory-sync.lock")
    return a


# ------------------------------------------------------------------ #
# health / probe
# ------------------------------------------------------------------ #
def test_health_is_degraded_when_the_cli_fails(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(1, err=ABI_ERR)):
        code, body = es.health(fresh=True)
    assert code == 503
    assert body["status"] == "degraded"
    assert body["archive_exists"] is True   # the old check, still true
    assert body["cli_ok"] is False
    assert "NODE_MODULE_VERSION 147" in body["stderr"]
    assert "episodic_doctor.py --rebuild" in body["hint"]


def test_health_is_ok_when_the_cli_runs(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(0, out="27674 conversations")):
        code, body = es.health(fresh=True)
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
        code, body = es.health(fresh=True)
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
    srv = es.EpisodicServer(("127.0.0.1", 0), es.EpisodicHandler)
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
        assert _get(http + "/health?fresh=1")[0] == 503
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


# ------------------------------------------------------------------ #
# install_vendored: what deploy and install.py run on the server host
# ------------------------------------------------------------------ #
@pytest.fixture
def vendored(tmp_path, monkeypatch):
    root = tmp_path / "episodic-memory"
    mod = root / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node"
    mod.parent.mkdir(parents=True)
    mod.write_bytes(b"")
    (root / "package.json").write_text('{"name": "episodic-memory"}')
    monkeypatch.setattr(doctor, "deploy_stamp", lambda root=None: "tree=abc abi=147")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(doctor, "pick_compiler", lambda: ("cc", "c++"))
    monkeypatch.setattr(doctor, "linked_root", lambda: root)
    return root


def _npm_calls(run):
    return [c.args[0][1] for c in run.call_args_list]


def test_install_skips_npm_when_the_stamp_matches_and_modules_load(vendored, monkeypatch):
    (vendored / "node_modules" / doctor.STAMP_NAME).write_text("tree=abc abi=147\n")
    monkeypatch.setattr(doctor, "load_error", lambda m: None)
    with mock.patch.object(doctor.subprocess, "run") as run:
        assert doctor.install_vendored(vendored, note=lambda m: None)
    assert run.call_count == 0


@pytest.mark.skipif(os.name == "nt", reason="the static C++ runtime is a GCC link "
                    "flag; install_vendored only picks a compiler off Windows")
def test_install_rebuilds_with_the_static_runtime_when_the_stamp_is_stale(vendored, monkeypatch):
    monkeypatch.setattr(doctor, "load_error", lambda m: None)
    with mock.patch.object(doctor.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        assert doctor.install_vendored(vendored, note=lambda m: None)
    assert _npm_calls(run) == ["install"]
    env = run.call_args_list[0].kwargs["env"]
    assert doctor.STATIC_RUNTIME in env["LDFLAGS"] and env["CXX"] == "c++"
    assert (vendored / "node_modules" / doctor.STAMP_NAME).read_text().strip() == "tree=abc abi=147"


@pytest.mark.skipif(os.name != "nt", reason="Windows builds with the toolchain npm finds")
def test_install_on_windows_leaves_the_toolchain_to_npm(vendored, monkeypatch):
    monkeypatch.setattr(doctor, "load_error", lambda m: None)
    with mock.patch.object(doctor.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        assert doctor.install_vendored(vendored, note=lambda m: None)
    assert _npm_calls(run) == ["install"]
    env = run.call_args_list[0].kwargs["env"]
    assert doctor.STATIC_RUNTIME not in env.get("LDFLAGS", "")


def test_an_install_that_does_not_load_fails_and_writes_no_stamp(vendored, monkeypatch):
    monkeypatch.setattr(doctor, "load_error", lambda m: "NODE_MODULE_VERSION 127")
    notes = []
    with mock.patch.object(doctor.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, "", "")):
        assert not doctor.install_vendored(vendored, note=notes.append)
    assert not (vendored / "node_modules" / doctor.STAMP_NAME).exists()
    assert any("does not load" in n for n in notes)


def test_a_cli_linked_elsewhere_is_relinked_and_checked(vendored, monkeypatch, tmp_path):
    (vendored / "node_modules" / doctor.STAMP_NAME).write_text("tree=abc abi=147\n")
    monkeypatch.setattr(doctor, "load_error", lambda m: None)
    where = iter([tmp_path / "old-checkout", vendored])
    monkeypatch.setattr(doctor, "linked_root", lambda: next(where))
    with mock.patch.object(doctor.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        assert doctor.install_vendored(vendored, note=lambda m: None)
    assert _npm_calls(run) == ["link"]


def test_a_link_that_does_not_take_fails(vendored, monkeypatch, tmp_path):
    (vendored / "node_modules" / doctor.STAMP_NAME).write_text("tree=abc abi=147\n")
    monkeypatch.setattr(doctor, "load_error", lambda m: None)
    monkeypatch.setattr(doctor, "linked_root", lambda: tmp_path / "old-checkout")
    with mock.patch.object(doctor.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, "", "")):
        assert not doctor.install_vendored(vendored, note=lambda m: None)


# ------------------------------------------------------------------ #
# Archive compression: the threshold reaches every sync we start
# ------------------------------------------------------------------ #
def test_server_sync_env_reads_the_threshold_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-hooks.json"
    monkeypatch.setattr(es, "CONFIG_PATH", cfg)
    cfg.write_text(json.dumps({"episodic": {"compress_after_days": 3}}))
    assert es.sync_env()["EPISODIC_MEMORY_COMPRESS_AFTER_DAYS"] == "3"
    cfg.write_text(json.dumps({"episodic": {}}))
    assert es.sync_env()["EPISODIC_MEMORY_COMPRESS_AFTER_DAYS"] == "7"
    monkeypatch.setenv("EPISODIC_MEMORY_COMPRESS_AFTER_DAYS", "9")
    cfg.write_text(json.dumps({"episodic": {"compress_after_days": 0}}))
    assert "EPISODIC_MEMORY_COMPRESS_AFTER_DAYS" not in es.sync_env()


def test_ingest_drops_a_stale_compressed_copy(http, archive):
    old = archive / "h-proj" / "sid.jsonl.zst"
    old.parent.mkdir()
    old.write_bytes(b"stale")
    req = urllib.request.Request(
        http + "/ingest", data=b'{"type":"user"}\n' * 10, method="POST",
        headers={"X-Project": "proj", "X-Session-Id": "sid", "X-Source-Host": "h"})
    with mock.patch.object(es.subprocess, "Popen") as popen:
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
    assert (archive / "h-proj" / "sid.jsonl").exists()
    assert not old.exists()
    assert "EPISODIC_MEMORY_COMPRESS_AFTER_DAYS" in popen.call_args.kwargs["env"]


def test_session_end_sync_env():
    from claude_hooks.hooks.session_end import sync_env
    assert sync_env({"compress_after_days": 5})["EPISODIC_MEMORY_COMPRESS_AFTER_DAYS"] == "5"
    assert sync_env({})["EPISODIC_MEMORY_COMPRESS_AFTER_DAYS"] == "7"
    assert "EPISODIC_MEMORY_COMPRESS_AFTER_DAYS" not in sync_env({"compress_after_days": 0})
    assert "EPISODIC_MEMORY_COMPRESS_AFTER_DAYS" not in sync_env({"compress_after_days": "x"})


# ------------------------------------------------------------------ #
# The probe must load the embedding model (2026-10-31 .. 11-26: the
# model cache was gone, every search and every sync's indexing failed,
# and a ``stats`` probe kept answering ok).
# ------------------------------------------------------------------ #
def test_the_probe_is_a_vector_search_not_stats(archive):
    with mock.patch.object(es, "_run_cli", return_value=_cp(0)) as run:
        es.probe(fresh=True)
    args = run.call_args.args[0]
    assert args[0] == "search" and "--vector" in args
    assert "stats" not in args
    assert run.call_args.args[1] == es.SEARCH_TIMEOUT_S


def test_a_search_that_cannot_load_the_model_is_degraded(archive):
    err = ("Error searching: Error: ENOENT: no such file or directory, mkdir "
           "'/x/node_modules/@huggingface/transformers/.cache'")
    with mock.patch.object(es, "_run_cli", return_value=_cp(1, err=err)):
        code, body = es.health(fresh=True)
    assert (code, body["status"], body["cli_ok"]) == (503, "degraded", False)
    assert "transformers/.cache" in body["stderr"]


# ------------------------------------------------------------------ #
# POST /sync?background=1: the hook daemon's sandbox cannot write the
# sync log, the archive or the index, so SessionEnd asks the server.
# ------------------------------------------------------------------ #
def _post(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_a_background_sync_is_started_and_answered_202(http):
    with mock.patch.object(es.subprocess, "Popen") as popen, \
            mock.patch.object(es, "_run_cli") as run:
        code, body = _post(http + "/sync?background=1")
    assert (code, body["status"]) == (202, "started")
    assert popen.call_args.args[0][1:] == ["sync", "--background"]
    assert "stderr" not in popen.call_args.kwargs   # stays on the journal
    run.assert_not_called()


def test_a_plain_sync_still_runs_in_the_request(http):
    with mock.patch.object(es.subprocess, "Popen") as popen, \
            mock.patch.object(es, "_run_cli", return_value=_cp(0, out="done")):
        code, body = _post(http + "/sync")
    assert (code, body["status"]) == (200, "synced")
    popen.assert_not_called()


def test_a_background_sync_that_cannot_start_is_a_500(http):
    with mock.patch.object(es.subprocess, "Popen", side_effect=FileNotFoundError("nope")):
        code, body = _post(http + "/sync?background=1")
    assert code == 500 and "cannot run" in body["error"]


# ------------------------------------------------------------------ #
# /health answers at once (owner, 2026-11-26, via automaticum #2402): it
# waited behind a ~4 min search, because the server served one request at
# a time and a fresh-less probe ran in the request.
# ------------------------------------------------------------------ #
def test_health_answers_while_a_search_runs(http):
    release = threading.Event()
    started = threading.Event()

    def slow(args, timeout, env=None):
        if args[0] == "search" and "--vector" not in args:
            started.set()
            release.wait(10)
        return _cp(0)

    with mock.patch.object(es, "_run_cli", side_effect=slow):
        t = threading.Thread(target=_get, args=(http + "/search?q=slow",), daemon=True)
        t.start()
        assert started.wait(5)
        t0 = time.monotonic()
        code, body = _get(http + "/health")
        took = time.monotonic() - t0
        release.set()
        t.join(10)
        _wait_for_probe()
    assert took < 2.0
    assert body["searches"]["running"] == 1
    assert body["activity"] == "searching"


def test_plain_health_never_probes_in_the_request(archive):
    gate = threading.Event()

    def slow_probe(args, timeout, env=None):
        gate.wait(10)
        return _cp(0)

    with mock.patch.object(es, "_run_cli", side_effect=slow_probe):
        t0 = time.monotonic()
        code, body = es.health()
        assert time.monotonic() - t0 < 1.0
        assert (code, body["status"], body["cli_ok"]) == (503, "probing", None)
        assert body["probe_running"] is True
        # A second call does not start a second probe.
        assert es.refresh_probe_in_background(time.time()) is False
        gate.set()
        _wait_for_probe()
        code, body = es.health()
    assert (code, body["status"]) == (200, "ok")


def test_indexing_is_read_from_the_sync_lock(archive):
    now = time.time()
    assert es.indexing_state(now) == {"running": False}
    es.SYNC_LOCK.write_text("4242")
    mutex = es.SYNC_LOCK.parent / (es.SYNC_LOCK.name + ".lock")
    mutex.mkdir()
    st = es.indexing_state(now)
    assert st["running"] is True and st["pid"] == 4242 and "since" in st
    old = now - es.SYNC_LOCK_STALE_S - 5
    os.utime(mutex, (old, old))
    assert es.indexing_state(now) == {"running": False, "stale_lock": True}


SYNC_LOG_SAMPLE = """\
2026-11-25T10:00:00.000Z [info] Starting background sync from pid 1
Syncing conversations...

✅ Sync complete!
  Copied: 4
  Skipped: 10
  Indexed: 4
  Summarized: 2
2026-11-26T09:25:51.617Z [info] Starting background sync from pid 2
Syncing conversations...

✅ Sync complete!
  Copied: 3
  Skipped: 1833
  Indexed: 0
  Summarized: 10

⚠️  Errors: 1
  (embeddings): Semantic indexing skipped — embedding backend unavailable: ENOENT
2026-11-26T10:00:00.000Z [info] Starting background sync from pid 3
Syncing conversations...
"""


def test_last_sync_is_the_last_completed_one():
    got = es.last_sync(SYNC_LOG_SAMPLE)
    assert got["started_at"] == "2026-11-26T09:25:51.617Z"
    assert (got["copied"], got["indexed"], got["summarized"]) == (3, 0, 10)
    assert got["indexing_skipped"] is True
    assert es.last_sync("nothing here") is None


def test_a_sync_that_skipped_indexing_is_degraded(archive):
    es.SYNC_LOG.write_text(SYNC_LOG_SAMPLE)
    with mock.patch.object(es, "_run_cli", return_value=_cp(0)):
        code, body = es.health(fresh=True)
    assert (code, body["status"], body["cli_ok"]) == (503, "degraded", True)
    assert "skipped semantic indexing" in body["error"]


def test_searches_wait_for_a_slot_and_give_up(monkeypatch):
    monkeypatch.setattr(es, "_search_slots", threading.BoundedSemaphore(1))
    with es.search_slot(1) as first:
        assert first is True
        with es.search_slot(0.05) as second:
            assert second is False
    assert es.searches_state()["running"] == 0
    assert es.searches_state()["waiting"] == 0
