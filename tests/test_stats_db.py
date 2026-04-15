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

    def test_model_rollup_excludes_non_messages_paths(self, tmp_path):
        """Telemetry / health-probe requests don't carry a ``model``
        field and would otherwise pile up as ``<unknown>``. model_rollup
        must only cover ``/v1/messages`` traffic; daily totals still
        include everything.
        """
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-14.jsonl", [
            _mk_record(model_delivered="claude-opus-4-6"),
            _mk_record(
                ts="2026-04-14T11:00:00Z",
                path="/api/event_logging/batch",
                model_requested=None, model_delivered=None,
            ),
            _mk_record(
                ts="2026-04-14T12:00:00Z",
                method="HEAD", path="/",
                model_requested=None, model_delivered=None,
                status=404,
            ),
            _mk_record(
                ts="2026-04-14T13:00:00Z",
                path="/api/event_logging/batch",
                model_requested=None, model_delivered=None,
            ),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        conn = sqlite3.connect(db)
        try:
            # Only the /v1/messages row shows up in model_rollup.
            rows = dict(conn.execute(
                "SELECT model, request_count FROM model_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchall())
            assert rows == {"claude-opus-4-6": 1}
            # But the daily total still counts every upstream hit.
            total = conn.execute(
                "SELECT request_count FROM daily_rollup WHERE date=?",
                ("2026-04-14",),
            ).fetchone()[0]
            assert total == 4
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


# ============================================================ #
# S2 — agent_rollup + S2 column population
# ============================================================ #
class TestS2Ingest:
    def _mk_s2_record(self, **kw):
        base = _mk_record(**kw)
        base.setdefault("agent_type", "main")
        base.setdefault("agent_name", "main")
        base.setdefault("request_class", "main")
        base.setdefault("is_sidechain", False)
        base.setdefault("cc_version", "2.1.107.616")
        base.setdefault("effort", "medium")
        base.setdefault("thinking_type", "adaptive")
        base.setdefault("account_uuid", "acc-uuid")
        base.setdefault("num_tools", 127)
        base.setdefault("num_messages", 10)
        base.setdefault("beta_features", ["context-management-2025-06-27"])
        return base

    def test_s2_columns_written(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-15.jsonl", [self._mk_s2_record()])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        import sqlite3
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT cc_version, effort, thinking_type, account_uuid, "
                "num_tools, num_messages, beta_features, agent_type, "
                "agent_name, is_sidechain FROM requests LIMIT 1"
            ).fetchone()
            assert row == (
                "2.1.107.616", "medium", "adaptive", "acc-uuid",
                127, 10, "context-management-2025-06-27", "main",
                "main", 0,
            )
        finally:
            conn.close()

    def test_agent_rollup_groups_by_agent_name(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        recs = [
            self._mk_s2_record(agent_name="main", agent_type="main"),
            self._mk_s2_record(ts="2026-04-15T11:00:00Z",
                               agent_name="code reviewer",
                               agent_type="subagent",
                               is_sidechain=True),
            self._mk_s2_record(ts="2026-04-15T12:00:00Z",
                               agent_name="code reviewer",
                               agent_type="subagent",
                               is_sidechain=True),
            self._mk_s2_record(ts="2026-04-15T13:00:00Z",
                               agent_name="warmup",
                               agent_type="warmup",
                               is_warmup=True, warmup_blocked=True),
        ]
        for r in recs:
            r["ts"] = r["ts"].replace("2026-04-14", "2026-04-15")
        # Normalise date for all records.
        recs[0]["ts"] = "2026-04-15T10:00:00Z"
        _write_jsonl(log_dir / "2026-04-15.jsonl", recs)
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        import sqlite3
        conn = sqlite3.connect(db)
        try:
            rows = dict(conn.execute(
                "SELECT agent_name, request_count FROM agent_rollup "
                "WHERE date='2026-04-15'"
            ).fetchall())
            assert rows == {"main": 1, "code reviewer": 2, "warmup": 1}
            # Warmup row should also have warmup_blocked_count = 1
            wmb = conn.execute(
                "SELECT warmup_blocked_count FROM agent_rollup "
                "WHERE date='2026-04-15' AND agent_name='warmup'"
            ).fetchone()[0]
            assert wmb == 1
        finally:
            conn.close()

    def test_beta_features_serialised_as_csv(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        rec = self._mk_s2_record(
            beta_features=["feat-a", "feat-b", "feat-c"],
        )
        _write_jsonl(log_dir / "2026-04-15.jsonl", [rec])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        import sqlite3
        conn = sqlite3.connect(db)
        try:
            v = conn.execute(
                "SELECT beta_features FROM requests LIMIT 1"
            ).fetchone()[0]
            assert v == "feat-a,feat-b,feat-c"
        finally:
            conn.close()


class TestS2Migration:
    def test_v1_database_migrates_forward(self, tmp_path):
        """A database from schema v1 gains S2 columns on next connect,
        preserving existing rows.
        """
        import sqlite3
        from claude_hooks.proxy import stats_db

        db = tmp_path / "s.db"
        # Build v1 explicitly — apply v1 DDL, set user_version=1.
        conn = sqlite3.connect(db, isolation_level=None)
        for stmt in stats_db._schema_ddl():
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 1")
        # Drop a v1-shape row into requests.
        conn.execute("""
            INSERT INTO requests(
                ts, date, source_file, source_line, session_id, method,
                path, status, duration_ms, req_bytes, resp_bytes,
                model_requested, model_delivered, model_effective,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                is_warmup, warmup_blocked, synthetic
            ) VALUES (
                '2026-04-14T10:00:00Z', '2026-04-14', 'old.jsonl', 1,
                'sess', 'POST', '/v1/messages', 200, 1234, 500, 2000,
                'claude-opus-4-6', 'claude-opus-4-6', 'claude-opus-4-6',
                10, 50, 100, 900,
                0, 0, 0
            )
        """)
        conn.close()

        # Reconnect via the real API — migration should kick in.
        conn2 = stats_db.connect(db)
        try:
            v = conn2.execute("PRAGMA user_version").fetchone()[0]
            assert v == stats_db.SCHEMA_VERSION
            # S2 columns exist and default to NULL for the old row.
            row = conn2.execute(
                "SELECT cc_version, effort, agent_type FROM requests "
                "WHERE source_line = 1"
            ).fetchone()
            assert row == (None, None, None)
            # agent_rollup table exists.
            conn2.execute("SELECT COUNT(*) FROM agent_rollup").fetchone()
        finally:
            conn2.close()


# ============================================================ #
# S3 — thinking metrics on daily_rollup
# ============================================================ #
class TestS3Ingest:
    def test_daily_rollup_sums_thinking_columns(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-15.jsonl", [
            _mk_record(ts="2026-04-15T10:00:00Z",
                       thinking_delta_count=12,
                       thinking_signature_bytes=256),
            _mk_record(ts="2026-04-15T11:00:00Z",
                       thinking_delta_count=5,
                       thinking_signature_bytes=120,
                       thinking_output_tokens=80),
            _mk_record(ts="2026-04-15T12:00:00Z"),   # no thinking
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        import sqlite3
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT thinking_request_count, total_thinking_delta_count, "
                "total_thinking_signature_bytes, total_thinking_output_tokens "
                "FROM daily_rollup WHERE date=?", ("2026-04-15",),
            ).fetchone()
            assert row == (2, 17, 376, 80)
        finally:
            conn.close()


class TestS3Migration:
    def test_v2_database_migrates_to_v3(self, tmp_path):
        """A v2 DB gains S3 daily_rollup columns on next connect."""
        import sqlite3
        from claude_hooks.proxy import stats_db

        db = tmp_path / "s.db"
        # Build v2 explicitly: run v1 DDL + v2 migration, pin version.
        conn = sqlite3.connect(db, isolation_level=None)
        for stmt in stats_db._schema_ddl():
            conn.execute(stmt)
        stats_db._migrate_v2(conn)
        conn.execute("PRAGMA user_version = 2")
        conn.close()

        # Reconnect — v3 migration should run.
        conn2 = stats_db.connect(db)
        try:
            v = conn2.execute("PRAGMA user_version").fetchone()[0]
            assert v == stats_db.SCHEMA_VERSION
            cols = {
                r[1] for r in conn2.execute(
                    "PRAGMA table_info(daily_rollup)").fetchall()
            }
            for added in ("thinking_request_count",
                          "total_thinking_delta_count",
                          "total_thinking_signature_bytes",
                          "total_thinking_output_tokens"):
                assert added in cols, f"missing {added}"
        finally:
            conn2.close()


# ============================================================ #
# S4 — tool-use categorisation + thinking visible/redacted aggregates
# ============================================================ #
class TestS4Categorisation:
    def test_categorise_tools_buckets(self):
        from claude_hooks.proxy.stats_db import _categorise_tools
        counts = {"Read": 6, "Edit": 2, "Grep": 3, "Bash": 1, "Task": 1,
                  "Write": 1, "WebFetch": 1, "SomeMCP__foo": 4}
        cats = _categorise_tools(counts)
        assert cats["read"] == 6
        assert cats["edit"] == 2
        assert cats["research"] == 6 + 3 + 1       # Read + Grep + WebFetch
        assert cats["mutation"] == 2 + 1           # Edit + Write
        assert cats["bash"] == 1
        assert cats["task"] == 1
        assert cats["total"] == sum(counts.values())

    def test_categorise_tools_empty(self):
        from claude_hooks.proxy.stats_db import _categorise_tools
        cats = _categorise_tools(None)
        for v in cats.values():
            assert v == 0


class TestS4Rollup:
    def _rec(self, ts, tools):
        return _mk_record(
            ts=ts,
            tool_use_counts=tools,
            thinking_visible_delta_count=1,
            thinking_redacted_delta_count=2,
            thinking_delta_count=3,
            thinking_signature_bytes=200,
        )

    def test_daily_rollup_aggregates_tool_categories(self, tmp_path):
        log_dir = tmp_path / "logs"; log_dir.mkdir()
        _write_jsonl(log_dir / "2026-04-15.jsonl", [
            self._rec("2026-04-15T10:00:00Z", {"Read": 5, "Edit": 1}),
            self._rec("2026-04-15T11:00:00Z", {"Read": 3, "Grep": 2, "Bash": 1}),
            self._rec("2026-04-15T12:00:00Z", {"Write": 1, "Task": 1}),
        ])
        db = tmp_path / "s.db"
        ingest_dir(db, log_dir)
        import sqlite3
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT total_tool_read_count, total_tool_edit_count, "
                "total_tool_research_count, total_tool_mutation_count, "
                "total_tool_bash_count, total_tool_task_count, "
                "total_tool_total_count, "
                "total_thinking_visible_delta_count, "
                "total_thinking_redacted_delta_count "
                "FROM daily_rollup WHERE date=?", ("2026-04-15",),
            ).fetchone()
            (read, edit, research, mutation, bash, task, total,
             visible, redacted) = row
            assert read == 8
            assert edit == 1
            assert research == 8 + 2    # Read*8 + Grep*2
            assert mutation == 1 + 1    # Edit + Write
            assert bash == 1
            assert task == 1
            assert total == 5 + 1 + 3 + 2 + 1 + 1 + 1   # 14
            assert visible == 3
            assert redacted == 6
        finally:
            conn.close()


class TestS4Migration:
    def test_v3_database_migrates_to_v4(self, tmp_path):
        """v3 DB gains S4 columns on next connect — existing data stays."""
        import sqlite3
        from claude_hooks.proxy import stats_db

        db = tmp_path / "s.db"
        conn = sqlite3.connect(db, isolation_level=None)
        for stmt in stats_db._schema_ddl():
            conn.execute(stmt)
        stats_db._migrate_v2(conn)
        stats_db._migrate_v3(conn)
        conn.execute("PRAGMA user_version = 3")
        conn.close()

        conn2 = stats_db.connect(db)
        try:
            v = conn2.execute("PRAGMA user_version").fetchone()[0]
            assert v == stats_db.SCHEMA_VERSION
            req_cols = {
                r[1] for r in conn2.execute(
                    "PRAGMA table_info(requests)").fetchall()
            }
            for needed in ("thinking_visible_delta_count",
                           "thinking_redacted_delta_count",
                           "tool_use_counts",
                           "tool_read_count", "tool_edit_count",
                           "tool_research_count", "tool_mutation_count",
                           "tool_bash_count", "tool_task_count",
                           "tool_total_count"):
                assert needed in req_cols
            daily_cols = {
                r[1] for r in conn2.execute(
                    "PRAGMA table_info(daily_rollup)").fetchall()
            }
            for needed in ("total_thinking_visible_delta_count",
                           "total_thinking_redacted_delta_count",
                           "total_tool_read_count",
                           "total_tool_edit_count",
                           "total_tool_research_count",
                           "total_tool_mutation_count",
                           "total_tool_total_count"):
                assert needed in daily_cols
        finally:
            conn2.close()
