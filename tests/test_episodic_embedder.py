"""episodic-memory embeds with the host's configured embedder.

claude_hooks/episodic_embedder.py writes <superpowers>/embedder.json from
the memory provider's embedder; episodic-memory/src/embedder-config.ts
reads it (its own tests: test/configured-embedder.test.ts).
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

from claude_hooks import episodic_embedder as ee

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def superpowers(tmp_path, monkeypatch):
    for var in ("PERSONAL_SUPERPOWERS_DIR", "XDG_CONFIG_HOME", "EPISODIC_MEMORY_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "superpowers"
    monkeypatch.setenv("EPISODIC_MEMORY_CONFIG_DIR", str(d))
    return d


def _cfg(name="llamafile", opts=None, provider="pgvector") -> dict:
    if opts is None:
        opts = {"url": "http://127.0.0.1:38092/embedding", "model": "qwen3-embedding:0.6b",
                "timeout": 180.0, "max_chars": 30000, "daemon_ensure": True}
    return {"providers": {provider: {"enabled": True, "embedder": name, "embedder_options": opts}}}


class TestSpec:
    def test_llamafile_names_the_model_not_the_hooks_cut(self):
        spec, why = ee.spec_for(_cfg())
        assert why == ""
        assert spec == {"kind": "llamafile", "url": "http://127.0.0.1:38092/embedding",
                        "model": "qwen3-embedding:0.6b", "timeout_ms": 180000,
                        "_managed_by": "claude-hooks", "_source": "providers.pgvector"}
        # The hooks' 30000-character cut is for memories; episodic keeps its own.
        assert "max_chars" not in spec

    def test_a_llamafile_without_a_model_has_no_space_to_name(self):
        spec, why = ee.spec_for(_cfg(opts={"url": "http://x:1/embedding"}))
        assert spec is None and "model" in why

    def test_ollama_batches_on_api_embed(self):
        spec, _ = ee.spec_for(_cfg("ollama", {"url": "http://h:11434/api/embeddings", "model": "m"}))
        assert spec["url"] == "http://h:11434/api/embed"
        spec, _ = ee.spec_for(_cfg("ollama", {}))
        assert spec["model"] == ee.OLLAMA_DEFAULT_MODEL

    def test_sqlite_vec_when_pgvector_is_off(self):
        cfg = _cfg(provider="sqlite_vec")
        cfg["providers"]["pgvector"] = {"enabled": False, "embedder": "ollama"}
        assert ee.spec_for(cfg)[0]["_source"] == "providers.sqlite_vec"

    def test_no_store_no_file(self):
        assert ee.spec_for({"providers": {}})[0] is None
        assert ee.spec_for(_cfg("null", {}))[0] is None

    def test_composite_keeps_a_fallback_only_of_the_same_model(self):
        same = _cfg("composite", {
            "primary": "ollama", "primary_options": {"url": "http://g:11434", "model": "q"},
            "fallback": "llamafile", "fallback_options": {"url": "http://c:1/embedding", "model": "q"}})
        spec, _ = ee.spec_for(same)
        assert spec["kind"] == "ollama" and spec["fallback"]["kind"] == "llamafile"
        other = _cfg("composite", {
            "primary": "ollama", "primary_options": {"model": "q"},
            "fallback": "llamafile", "fallback_options": {"url": "http://c:1/embedding", "model": "z"}})
        assert "fallback" not in ee.spec_for(other)[0]


class TestFile:
    def test_written_private_then_unchanged(self, superpowers):
        notes = []
        assert ee.sync_file(_cfg(), note=notes.append) == "written"
        path = superpowers / "embedder.json"
        assert json.loads(path.read_text())["model"] == "qwen3-embedding:0.6b"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert ee.sync_file(_cfg(), note=notes.append) == "unchanged"

    def test_dry_run_writes_nothing(self, superpowers):
        assert ee.sync_file(_cfg(), dry=True, note=lambda m: None) == "written"
        assert not (superpowers / "embedder.json").exists()

    def test_a_users_own_file_is_left_alone(self, superpowers):
        superpowers.mkdir(parents=True)
        own = {"kind": "ollama", "url": "http://mine:11434/api/embed", "model": "mine"}
        (superpowers / "embedder.json").write_text(json.dumps(own))
        assert ee.sync_file(_cfg(), note=lambda m: None) == "user-owned"
        assert json.loads((superpowers / "embedder.json").read_text()) == own

    def test_ours_is_removed_when_the_config_names_none(self, superpowers):
        ee.sync_file(_cfg(), note=lambda m: None)
        assert ee.sync_file({"providers": {}}, note=lambda m: None) == "removed"
        assert not (superpowers / "embedder.json").exists()

    def test_superpowers_dir_follows_the_package(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EPISODIC_MEMORY_CONFIG_DIR")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert ee.superpowers_dir() == tmp_path / "xdg" / "superpowers"
        monkeypatch.setenv("PERSONAL_SUPERPOWERS_DIR", str(tmp_path / "p"))
        assert ee.superpowers_dir() == tmp_path / "p"


class TestIndex:
    def test_index_space(self, superpowers):
        assert ee.index_space() is None
        db = superpowers / "conversation-index" / "db.sqlite"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE exchanges (id TEXT)")
        conn.commit()
        assert ee.index_space() == ee.LEGACY_SPACE
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO index_meta VALUES ('embedder', 'qwen3-embedding:0.6b')")
        conn.commit()
        conn.close()
        assert ee.index_space() == "qwen3-embedding:0.6b"

    def test_index_pending(self, superpowers):
        # Transcripts the index still owes (index-pending.ts); verify_deploy WARNs on them.
        assert ee.index_pending() is None
        db = superpowers / "conversation-index" / "db.sqlite"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE exchanges (id TEXT)")
        conn.commit()
        assert ee.index_pending() == 0  # an index from before the list
        conn.execute("CREATE TABLE index_pending (archive_path TEXT PRIMARY KEY, reason TEXT, since TEXT)")
        conn.executemany("INSERT INTO index_pending VALUES (?, 'catch-up', '2026-11-27')",
                         [("/a.jsonl",), ("/b.jsonl",)])
        conn.commit()
        conn.close()
        assert ee.index_pending() == 2
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        try:
            import episodic_doctor
        finally:
            sys.path.pop(0)
        assert episodic_doctor._pending(db) == 2

    def test_rebuild_spec_swaps_the_server_only(self):
        spec, _ = ee.spec_for(_cfg())
        via = ee.rebuild_spec(spec, "http://127.0.0.1:11434", "ollama")
        assert via == {"kind": "ollama", "url": "http://127.0.0.1:11434/api/embed",
                       "model": "qwen3-embedding:0.6b", "timeout_ms": 180000}
        assert ee.space_of(via) == ee.space_of(spec)

    def test_parity_is_the_lowest_cosine(self):
        vecs = {"a": [[1.0, 0.0], [0.0, 1.0]], "b": [[1.0, 0.0], [0.6, 0.8]]}
        with mock.patch.object(ee, "_embed_once", side_effect=lambda spec, texts: vecs[spec["k"]]):
            assert ee.parity({"k": "a"}, {"k": "b"}) == pytest.approx(0.8)
        with mock.patch.object(ee, "_embed_once", side_effect=lambda spec, texts: [[1.0]] * 2
                               if spec["k"] == "a" else [[1.0, 0.0]] * 2):
            assert ee.parity({"k": "a"}, {"k": "b"}) == 0.0


class TestEnsure:
    def test_wakes_only_a_daemon_managed_llamafile(self):
        from claude_hooks import daemon_client
        with mock.patch.object(daemon_client, "embedding_ensure") as ens:
            ee.ensure_embedder(_cfg())
            assert ens.call_count == 1
            ee.ensure_embedder(_cfg(opts={"url": "http://x/embedding", "model": "m"}))
            ee.ensure_embedder(_cfg("ollama", {"model": "m"}))
            assert ens.call_count == 1

    def test_never_raises(self):
        from claude_hooks import daemon_client
        with mock.patch.object(daemon_client, "embedding_ensure", side_effect=OSError("down")):
            ee.ensure_embedder(_cfg())


class TestServer:
    def test_server_wakes_the_embedder_at_most_every_30_s(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(REPO / "episodic_server"))
        try:
            import server as es
        finally:
            sys.path.pop(0)
        cfg_path = tmp_path / "claude-hooks.json"
        cfg_path.write_text(json.dumps(_cfg()))
        monkeypatch.setattr(es, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(es, "_ensured_at", 0.0)
        with mock.patch.object(ee, "ensure_embedder") as ens:
            es.ensure_embedder(now=1000.0)
            es.ensure_embedder(now=1010.0)
            es.ensure_embedder(now=1031.0)
        assert ens.call_count == 2
