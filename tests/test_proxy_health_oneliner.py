"""Tests for ``scripts/proxy_health_oneliner.py``.

Seeds a synthetic stats.db with two effort buckets across two dates,
then runs ``main()`` and asserts the formatted line carries the
expected counters and the ↑ glyph when today's rate is meaningfully
higher than the prior baseline.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# Import the script as a module — it lives under scripts/ which isn't a
# package, so we hop sys.path to make it importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import proxy_health_oneliner as ph  # noqa: E402

# Reuse the proxy's stats_db schema so the seeded DB matches production.
from claude_hooks.proxy import stats_db  # noqa: E402


TODAY = "2026-05-01"
YESTERDAY = "2026-04-30"
DAY_BEFORE = "2026-04-29"


def _seed_request(conn: sqlite3.Connection, *, date: str, effort: str,
                  ownership: int = 0, permission: int = 0,
                  request_class: str = "main") -> None:
    """Insert a single ``requests`` row with the columns the oneliner reads.

    Many other columns are NOT NULL with defaults — the schema handles
    that. We only set the fields that drive the formatted line.
    """
    conn.execute(
        """
        INSERT INTO requests(
            ts, date, source_file, source_line, method, path, status,
            is_warmup, warmup_blocked, synthetic, request_class, effort,
            sp_ownership_dodging, sp_permission_seeking
        ) VALUES (?, ?, 's.jsonl', ?, 'POST', '/v1/messages', 200,
                  0, 0, 0, ?, ?, ?, ?)
        """,
        (f"{date}T00:00:00Z", date, conn.execute(
            "SELECT COALESCE(MAX(source_line), 0) + 1 FROM requests"
        ).fetchone()[0],
         request_class, effort, ownership, permission),
    )


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "stats.db"
    conn = stats_db.connect(db)
    try:
        # Baseline week — Apr 29 + Apr 30 — xhigh has clean ownership-dodging:
        # 5 hits / 5000 reqs = 1.0 per 1k. Medium has 0.
        for _ in range(2500):
            _seed_request(conn, date=DAY_BEFORE, effort="xhigh")
            _seed_request(conn, date=YESTERDAY, effort="xhigh")
        # Drop a small handful of hits across the baseline window.
        for _ in range(2):
            _seed_request(conn, date=DAY_BEFORE, effort="xhigh", ownership=1)
        for _ in range(3):
            _seed_request(conn, date=YESTERDAY, effort="xhigh", ownership=1)
        # Today — xhigh has spiked: 50 hits per 1k = ~50× baseline.
        for _ in range(500):
            _seed_request(conn, date=TODAY, effort="xhigh")
        for _ in range(25):  # 25 hits / 525 ≈ 47.6 per 1k
            _seed_request(conn, date=TODAY, effort="xhigh", ownership=1)
        # Today's medium is clean — 200 reqs, 0 hits.
        for _ in range(200):
            _seed_request(conn, date=TODAY, effort="medium")
        # Daily rollup row for today (the oneliner reads it for the basics).
        conn.execute(
            """
            INSERT INTO daily_rollup(
                date, request_count, model_divergence_count,
                status_4xx, status_5xx, status_429, updated_at
            ) VALUES (?, 725, 0, 0, 0, 0, ?)
            """,
            (TODAY, f"{TODAY}T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return db


class TestOneliner:
    def test_today_shows_uparrow_for_xhigh_regression(self, seeded_db):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ph.main(["--db", str(seeded_db), "--date", TODAY,
                          "--baseline-days", "7"])
        line = buf.getvalue().strip()
        assert rc == 0
        # Today's xhigh ownership-dodging rate (~47.6/1k) blows past the
        # baseline (~1/1k) — _arrow should attach an up-arrow.
        assert "xhigh" in line
        assert "↑" in line, f"expected up-arrow on regression, got: {line}"
        # And medium should be in the line too, without the arrow
        # (it's at 0 today, no baseline to compare against).
        assert "medium" in line

    def test_baseline_clean_no_arrow(self, seeded_db):
        """When today matches its baseline, the line should not flag ↑.

        We re-run the oneliner with ``--date`` set to a baseline day —
        the prior 7d window then contains the same ~1/1k rate, so today
        looks identical and no arrow appears.
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            ph.main(["--db", str(seeded_db), "--date", YESTERDAY,
                     "--baseline-days", "7"])
        line = buf.getvalue().strip()
        # YESTERDAY's rate matches DAY_BEFORE's — both around 1/1k. ↑
        # should NOT be present here. ↓ also shouldn't appear (baseline
        # too small in absolute terms — 1/1k is below the 5/1k floor).
        assert "↑" not in line
        assert "↓" not in line

    def test_handles_missing_db_quietly(self, tmp_path, capsys):
        """A missing DB must not crash cron — exit 0, log to stderr."""
        rc = ph.main(["--db", str(tmp_path / "absent.db")])
        assert rc == 0
        err = capsys.readouterr().err
        assert "db not found" in err

    def test_excludes_undersized_efforts(self, seeded_db):
        """Effort buckets with fewer than 30 main requests are noise —
        the oneliner should suppress them so the line stays readable.
        """
        # Add 5 reqs of a third effort today — under the 30-req floor.
        conn = sqlite3.connect(seeded_db)
        try:
            for _ in range(5):
                _seed_request(conn, date=TODAY, effort="high")
            conn.commit()
        finally:
            conn.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ph.main(["--db", str(seeded_db), "--date", TODAY])
        line = buf.getvalue().strip()
        assert "high(n=5)" not in line, f"undersized 'high' leaked: {line}"
