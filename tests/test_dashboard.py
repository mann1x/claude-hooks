"""
Tests for the proxy dashboard — S4.

Spin up the dashboard against a small synthetic stats.db + a
ratelimit-state.json fixture. Verify each endpoint returns the
expected shape and the HTML renders.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from claude_hooks.proxy import dashboard, stats_db


def _find_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def seeded_db(tmp_path):
    """Seed a stats.db with one daily_rollup, one agent_rollup, one
    model_rollup row, plus a ratelimit-state.json fixture.
    """
    db = tmp_path / "stats.db"
    # Force schema creation.
    conn = stats_db.connect(db)
    try:
        conn.execute("""
            INSERT INTO daily_rollup(
                date, request_count, warmup_count, warmup_blocked_count,
                synthetic_count, status_2xx, status_4xx, status_5xx, status_429,
                model_divergence_count, total_input_tokens, total_output_tokens,
                total_cache_creation_tokens, total_cache_read_tokens,
                cache_hit_rate, total_req_bytes, total_resp_bytes,
                total_duration_ms, updated_at,
                thinking_request_count, total_thinking_delta_count,
                total_thinking_signature_bytes, total_thinking_output_tokens
            ) VALUES (
                '2026-04-15', 100, 10, 10, 0, 90, 5, 5, 0,
                0, 1000, 5000, 50000, 900000,
                0.947, 10000, 50000, 120000, '2026-04-15T00:00:00Z',
                20, 80, 12000, 0
            )
        """)
        conn.execute("""
            INSERT INTO agent_rollup(
                date, agent_name, agent_type, request_count,
                warmup_blocked_count, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens
            ) VALUES
              ('2026-04-15', 'main', 'main', 50, 0, 500, 2500, 30000, 500000),
              ('2026-04-15', 'code reviewer', 'subagent', 40, 0, 400, 2000, 15000, 300000),
              ('2026-04-15', 'warmup', 'warmup', 10, 10, 0, 0, 0, 0)
        """)
        conn.execute("""
            INSERT INTO model_rollup(
                date, model, request_count, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens
            ) VALUES
              ('2026-04-15', 'claude-opus-4-6', 85, 800, 4000, 40000, 700000),
              ('2026-04-15', 'claude-haiku-4-5-20251001', 15, 200, 1000, 10000, 200000)
        """)
        # Two distinct beta-feature sets across 2 rows so _query_betas
        # has something to flatten.
        conn.execute("""
            INSERT INTO requests(
                ts, date, source_file, source_line, method, path, status,
                is_warmup, warmup_blocked, synthetic, beta_features
            ) VALUES
              ('2026-04-15T10:00:00Z', '2026-04-15', 's.jsonl', 1, 'POST', '/v1/messages', 200, 0, 0, 0,
               'context-management-2025-06-27,oauth-2025-04-20'),
              ('2026-04-15T11:00:00Z', '2026-04-15', 's.jsonl', 2, 'POST', '/v1/messages', 200, 0, 0, 0,
               'context-management-2025-06-27,effort-2025-11-24')
        """)
    finally:
        conn.close()

    # Ratelimit state.
    rl = tmp_path / "ratelimit-state.json"
    rl.write_text(json.dumps({
        "last_updated": "2026-04-15T06:00:00Z",
        "five_hour_utilization": 0.5,
        "seven_day_utilization": 0.8,
        "representative_claim": "seven_day",
        "raw_headers": {
            "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600),
            "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) + 86400),
        },
    }))
    return db, rl


@pytest.fixture
def running_dashboard(seeded_db):
    db, rl = seeded_db
    cfg = {
        "proxy": {
            "stats_db_path": str(db),
            "log_dir": str(rl.parent),
        },
        "proxy_dashboard": {
            "listen_host": "127.0.0.1",
            "listen_port": _find_port(),
        },
    }
    server = dashboard.build_server(cfg)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Give the bind a moment.
    time.sleep(0.05)
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status, r.headers.get_content_type(), r.read()


# ============================================================ #
class TestEndpoints:
    def test_healthz(self, running_dashboard):
        status, ct, body = _get(running_dashboard + "/healthz")
        assert status == 200
        assert ct.startswith("text/plain")
        assert body.strip() == b"OK"

    def test_root_serves_html(self, running_dashboard):
        status, ct, body = _get(running_dashboard + "/")
        assert status == 200
        assert ct.startswith("text/html")
        # Cheap sanity checks — must be a real page.
        assert b"claude-hooks proxy dashboard" in body
        assert b"/api/summary.json" in body

    def test_summary_today_populated(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/summary.json")
        assert status == 200
        j = json.loads(body)
        # Today shape
        assert j["today"]["date"] == "2026-04-15"
        assert j["today"]["request_count"] == 100
        assert abs(j["today"]["cache_hit_rate"] - 0.947) < 1e-6
        # last_7d aggregated
        assert j["last_7d"]["requests"] == 100
        # ratelimit + burn also included
        assert j["ratelimit"]["five_hour_utilization"] == 0.5
        assert j["burn"]["five_hour"] is not None

    def test_agents_rollup(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/agents.json?date=2026-04-15")
        assert status == 200
        rows = json.loads(body)
        names = {r["agent_name"] for r in rows}
        assert names == {"main", "code reviewer", "warmup"}
        # Sorted by request_count desc.
        assert rows[0]["agent_name"] == "main"
        assert rows[0]["request_count"] == 50

    def test_models_rollup(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/models.json?date=2026-04-15")
        assert status == 200
        rows = json.loads(body)
        assert rows[0]["model"] == "claude-opus-4-6"
        assert rows[0]["request_count"] == 85

    def test_daily_last14(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/daily.json?days=14")
        assert status == 200
        rows = json.loads(body)
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-04-15"

    def test_betas_flattened(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/betas.json")
        assert status == 200
        rows = json.loads(body)
        tokens = {r["token"] for r in rows}
        assert tokens == {
            "context-management-2025-06-27",
            "oauth-2025-04-20",
            "effort-2025-11-24",
        }
        # context-management-* appears in both rows → count 2.
        ctx = next(r for r in rows if r["token"] == "context-management-2025-06-27")
        assert ctx["requests"] == 2

    def test_ratelimit_endpoint(self, running_dashboard):
        status, _, body = _get(running_dashboard + "/api/ratelimit.json")
        assert status == 200
        j = json.loads(body)
        assert j["state"]["five_hour_utilization"] == 0.5
        # Burn has both windows computed.
        assert j["burn"]["five_hour"]["utilization"] == 0.5
        assert j["burn"]["seven_day"]["utilization"] == 0.8
        # ETA fields populated (non-negative).
        assert j["burn"]["five_hour"]["reset_in_s"] >= 0

    def test_not_found_returns_404(self, running_dashboard):
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(running_dashboard + "/no-such-thing")
        assert exc.value.code == 404

    def test_missing_db_returns_503(self, tmp_path):
        """Dashboard must degrade gracefully when stats.db is absent —
        never 500 to the client.
        """
        cfg = {
            "proxy": {
                "stats_db_path": str(tmp_path / "nope.db"),
                "log_dir": str(tmp_path),
            },
            "proxy_dashboard": {
                "listen_host": "127.0.0.1",
                "listen_port": _find_port(),
            },
        }
        server = dashboard.build_server(cfg)
        host, port = server.server_address
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            import urllib.error
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(f"http://{host}:{port}/api/summary.json")
            assert exc.value.code == 503
            # Healthz still works.
            status, _, body = _get(f"http://{host}:{port}/healthz")
            assert status == 200
        finally:
            server.shutdown(); server.server_close()


# ============================================================ #
class TestBurnMath:
    def test_project_skipped_when_no_reset(self):
        burn = dashboard._compute_burn({
            "five_hour_utilization": 0.5,
            "raw_headers": {},
        })
        assert burn["five_hour"] is None

    def test_project_produces_positive_eta_when_under_budget(self):
        """Given low utilization + distant reset, ETA should be far
        in the future (not negative).
        """
        burn = dashboard._compute_burn({
            "five_hour_utilization": 0.1,
            "seven_day_utilization": 0.2,
            "representative_claim": "seven_day",
            "raw_headers": {
                "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 4 * 3600),
                "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) + 5 * 86400),
            },
        })
        assert burn["five_hour"]["eta_to_full_s"] > 0
        assert burn["seven_day"]["eta_to_full_s"] > 0

    def test_project_flags_imminent_exhaustion(self):
        """High utilization with lots of window left → will_exhaust_before_reset."""
        # 90% utilization, window is 5h, reset in 4h (= 1h elapsed).
        # Burn so far: 0.9/3600 = 0.00025/s.
        # Remaining budget 0.1 @ that rate = 400s. Reset in 4h = 14400s.
        # So 400 < 14400 → flag should fire.
        burn = dashboard._compute_burn({
            "five_hour_utilization": 0.9,
            "raw_headers": {
                "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 4 * 3600),
            },
        })
        assert burn["five_hour"]["will_exhaust_before_reset"] is True
