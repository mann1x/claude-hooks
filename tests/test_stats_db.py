"""
Tests for ``claude_hooks/proxy/stats_db.py`` — S1 rollup.

Scope:
- schema creation idempotent
- first-run ingestion populates requests + rollups
- re-run is a no-op (cursor respected)
- append-and-rerun picks up only new lines
- rollup math: cache_hit_rate, divergence counter, warmup counts,
  status buckets, ratelimit timeseries
- invalid lines counted as parse errors without aborting
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from claude_hooks.proxy.stats_db import (
    SCHEMA_VERSION,
    connect,
    ingest_dir,
    ingest_file,
    rebuild_rollups,
)


def _mk_record(**kw):
    base = {
        "ts": "2026-04-14T10:00:00.000Z",
        "method": "POST",
        "path": "/v1/messages",
        "query": "",
        "status": 200,
        "duration_ms": 1234,
        "upstream_host": "https://api.anthropic.com",
        "req_bytes": 500,
        "resp_bytes": 2000,
        "model_requested": "claude-opus-4-6",
        "model_delivered": "claude-opus-4-6",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 50,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 900,
        },
        "stop_reason": "end_turn",
        "rate_limit": None,
        "is_warmup": False,
        "synthetic": False,
        "session_id": "sess-a",
    }
    base.update(kw)
    return base


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ============================================================ #
# Schema
# ============================================================ #
class TestSchema:
    def test_connect_creates_tables(self, tmp_path):
        db = tmp_path / "s.db"
        conn = connect(db)
        try:
            tables = {
                r[0] for r in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for needed in {
                "requests", "daily_rollup", "session_rollup",
                "model_rollup", "ratelimit_windows", "ingestion_state",
            }:
                assert needed in tables, f"missing table {needed}"
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            assert v == SCHEMA_VERSION
        finally:
            conn.close()

    def test_connect_is_idempotent(self, tmp_path):
        db = tmp_path / "s.db"
        connect(db).close()
        # Second connect must not raise or reset the version.
        conn = connect(db)
        try:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            assert v == SCHEMA_VERSION
        finally:
            conn.close()


# ============================================================ #
# Ingestion cursor
# ============================================================ #
class TestIngestCursor:
    def test_first_run_inserts_all(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        p = log_dir / "2026-04-14.jsonl"
        _write_jsonl(p, [_mk_record(), _mk_record(ts="2026-04-14T10:01:00Z")])

        db = tmp_path / "s.db"
        results = ingest_dir(db, log_dir)
        assert len(results) == 1
        assert results[0].lines_inserted == 2
        assert results[0].lines_skipped == 0

        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            assert n == 2
        finally:
            conn.close()

    def test_rerun_is_noop(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        p = log_dir / "2026-04-14.jsonl"
        _write_jsonl(p, [_mk_record(), _mk_record(ts="2026-04-14T10:01:00Z")])

        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        r2 = ingest_dir(db, log_dir)
        assert r2[0].lines_inserted == 0
        assert r2[0].lines_skipped == 2

        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        finally:
            conn.close()

    def test_append_and_rerun_picks_up_new(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        p = log_dir / "2026-04-14.jsonl"
        _write_jsonl(p, [_mk_record()])

        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)

        # Append two more lines and re-run.
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(_mk_record(ts="2026-04-14T10:01:00Z")) + "\n")
            f.write(json.dumps(_mk_record(ts="2026-04-14T10:02:00Z")) + "\n")

        r2 = ingest_dir(db, log_dir)
        assert r2[0].lines_inserted == 2
        assert r2[0].lines_skipped == 1

        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
        finally:
            conn.close()

    def test_invalid_lines_counted_as_errors(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        p = log_dir / "2026-04-14.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(_mk_record()) + "\n")
            f.write("not-json\n")
            f.write('{"just":"a list","arr":[]}\n')           # dict, missing ts
            f.write('[1,2,3]\n')                               # not a dict
            f.write(json.dumps(_mk_record(ts="2026-04-14T10:05:00Z")) + "\n")

        db = tmp_path / "s.db"
        results = ingest_dir(db, log_dir)
        r = results[0]
        assert r.lines_inserted == 2
        assert r.parse_errors == 3


# ============================================================ #
# Rollup math
# ============================================================ #
class TestRollupMath:
    def test_cache_hit_rate_computed(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(usage={"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 100,
                              "cache_read_input_tokens": 900}),
            _mk_record(ts="2026-04-14T11:00:00Z",
                       usage={"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 1000}),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT cache_hit_rate, total_cache_creation_tokens, "
                "total_cache_read_tokens FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()
            hit, cc, cr = row
            assert cc == 100 and cr == 1900
            # 1900 / 2000 = 0.95
            assert abs(hit - 0.95) < 1e-9
        finally:
            conn.close()

    def test_cache_hit_rate_none_when_no_cache(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            # Usage with zero cache tokens — denominator 0, hit rate undefined.
            _mk_record(usage={"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            hit = conn.execute(
                "SELECT cache_hit_rate FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()[0]
            assert hit is None
        finally:
            conn.close()

    def test_divergence_counter(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(),   # requested == delivered (opus-4-6)
            _mk_record(ts="2026-04-14T11:00:00Z",
                       model_requested="claude-opus-4-6",
                       model_delivered="claude-haiku-4-5-20251001"),
            _mk_record(ts="2026-04-14T12:00:00Z",
                       model_requested="claude-opus-4-6",
                       model_delivered=None),       # null delivered — don't count
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            div = conn.execute(
                "SELECT model_divergence_count FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()[0]
            assert div == 1
        finally:
            conn.close()

    def test_warmup_counts(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(is_warmup=True, warmup_blocked=True, status=200),
            _mk_record(ts="2026-04-14T11:00:00Z", is_warmup=True,
                       warmup_blocked=True, status=200),
            _mk_record(ts="2026-04-14T12:00:00Z"),
            _mk_record(ts="2026-04-14T13:00:00Z", is_warmup=True,
                       warmup_blocked=False, status=200),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            wm, wmb = conn.execute(
                "SELECT warmup_count, warmup_blocked_count FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()
            assert wm == 3
            assert wmb == 2
        finally:
            conn.close()

    def test_status_buckets(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(status=200),
            _mk_record(ts="2026-04-14T11:00:00Z", status=404),
            _mk_record(ts="2026-04-14T12:00:00Z", status=429),
            _mk_record(ts="2026-04-14T13:00:00Z", status=500),
            _mk_record(ts="2026-04-14T14:00:00Z", status=502),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT status_2xx, status_4xx, status_5xx, status_429 "
                "FROM daily_rollup WHERE date=?", ("2026-04-14",),
            ).fetchone()
            assert row == (1, 2, 2, 1)
        finally:
            conn.close()

    def test_session_rollup_groups_per_session(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(session_id="A"),
            _mk_record(ts="2026-04-14T11:00:00Z", session_id="A"),
            _mk_record(ts="2026-04-14T12:00:00Z", session_id="B"),
            _mk_record(ts="2026-04-14T13:00:00Z", session_id=None),  # dropped
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            rows = dict(conn.execute(
                "SELECT session_id, request_count FROM session_rollup "
                "WHERE date=?", ("2026-04-14",),
            ).fetchall())
            assert rows == {"A": 2, "B": 1}
        finally:
            conn.close()

    def test_model_rollup_uses_effective_model(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(model_delivered="claude-opus-4-6"),
            _mk_record(ts="2026-04-14T11:00:00Z",
                       model_delivered="claude-haiku-4-5-20251001"),
            _mk_record(ts="2026-04-14T12:00:00Z",
                       model_requested="claude-opus-4-6",
                       model_delivered=None),   # falls back to requested
            _mk_record(ts="2026-04-14T13:00:00Z",
                       model_requested=None, model_delivered=None),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            rows = dict(conn.execute(
                "SELECT model, request_count FROM model_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchall())
            assert rows == {
                "claude-opus-4-6": 2,
                "claude-haiku-4-5-20251001": 1,
                "<unknown>": 1,
            }
        finally:
            conn.close()


# ============================================================ #
# Ratelimit snapshots
# ============================================================ #
class TestRateLimitWindows:
    def test_snapshots_captured(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        headers = {
            "anthropic-ratelimit-unified-status": "allowed_warning",
            "anthropic-ratelimit-unified-5h-utilization": "0.87",
            "anthropic-ratelimit-unified-7d-utilization": "0.76",
            "anthropic-ratelimit-unified-5h-status": "allowed",
            "anthropic-ratelimit-unified-7d-status": "allowed_warning",
            "anthropic-ratelimit-unified-representative-claim": "seven_day",
            "anthropic-ratelimit-unified-5h-reset": "1776240000",
            "anthropic-ratelimit-unified-7d-reset": "1776412800",
        }
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(rate_limit=headers),
            _mk_record(ts="2026-04-14T11:00:00Z"),  # no rate_limit — skipped
            _mk_record(ts="2026-04-14T12:00:00Z", rate_limit=headers),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT ts, five_hour_utilization, seven_day_utilization, "
                "representative_claim FROM ratelimit_windows ORDER BY ts"
            ).fetchall()
            assert len(rows) == 2
            for _, five, seven, claim in rows:
                assert abs(five - 0.87) < 1e-9
                assert abs(seven - 0.76) < 1e-9
                assert claim == "seven_day"
        finally:
            conn.close()

    def test_ratelimit_snapshots_not_duplicated_on_rerun(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(rate_limit={
                "anthropic-ratelimit-unified-5h-utilization": "0.5",
                "anthropic-ratelimit-unified-7d-utilization": "0.6",
            }),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        ingest_dir(db, log_dir)    # second run
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM ratelimit_windows").fetchone()[0]
            assert n == 1
        finally:
            conn.close()


# ============================================================ #
# Rebuild-rollups safety
# ============================================================ #
class TestRebuildRollups:
    def test_rebuild_all_dates(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [_mk_record()])
        _write_jsonl(log_dir / "2026-04-15.jsonl", [
            _mk_record(ts="2026-04-15T10:00:00Z")
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = connect(db)
        try:
            dates = {r[0] for r in conn.execute("SELECT date FROM daily_rollup")}
            assert dates == {"2026-04-14", "2026-04-15"}
            # Manually mutate a count and verify rebuild restores it.
            conn.execute("UPDATE daily_rollup SET request_count=999 WHERE date=?",
                         ("2026-04-14",))
            rebuild_rollups(conn, dates=["2026-04-14"])
            rc = conn.execute(
                "SELECT request_count FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()[0]
            assert rc == 1
        finally:
            conn.close()
