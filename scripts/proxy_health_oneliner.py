"""Daily one-line health summary over the proxy stats DB.

Designed for cron / systemd-timer / claude-hooks-scheduler — emits a
single line to stdout that's tailored for spotting the regression
modes the proxy data can actually catch:

  - Stop-phrase rate spikes per effort (route-specific quality drift)
  - Model divergences (silent model substitution upstream)
  - 429 / 5xx counts
  - Per-effort request mix (so a sudden xhigh-only day stands out)

Compares "today" (or ``--date YYYY-MM-DD``) against the running
average of the prior 7 days so a single bad day shows as ``↑`` next
to the metric. Output is one line so it goes nicely into Slack /
notify-send / a status feed.

Exit code: 0 always. The point is observability, not alerting — if
this errors, something else (cron warnings) will surface it. We do
print failure causes to stderr.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import sys
from pathlib import Path


SP_KEYS = (
    "sp_ownership_dodging",
    "sp_permission_seeking",
    "sp_premature_stopping",
    "sp_simplest_fix",
    "sp_reasoning_reversal",
    "sp_self_admitted_error",
)


def _today_utc() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _per_effort_sp(conn: sqlite3.Connection, date: str) -> dict[str, dict[str, float]]:
    """Return ``{effort: {n, ownD_per_1k, permS_per_1k, ...}}`` for date."""
    out: dict[str, dict[str, float]] = {}
    rows = conn.execute(
        f"""
        SELECT
          COALESCE(effort, '(unset)') AS effort,
          COUNT(*) AS n,
          {', '.join('SUM(' + k + ') AS ' + k for k in SP_KEYS)}
        FROM requests
        WHERE date = ? AND request_class = 'main'
        GROUP BY effort
        """,
        (date,),
    ).fetchall()
    for r in rows:
        eff = r[0]
        n = r[1] or 0
        d: dict[str, float] = {"n": n}
        for i, k in enumerate(SP_KEYS, start=2):
            v = r[i] or 0
            d[k + "_per_1k"] = (1000.0 * v / n) if n > 0 else 0.0
            d[k] = v
        out[eff] = d
    return out


def _baseline_sp(conn: sqlite3.Connection, date: str, days: int = 7) -> dict[str, dict[str, float]]:
    """Average per-1k rate per effort over the prior ``days`` days
    (exclusive of ``date``). Lets us flag today as up/down vs trend.
    """
    end = _dt.datetime.strptime(date, "%Y-%m-%d")
    start = (end - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    end_excl = end.strftime("%Y-%m-%d")
    out: dict[str, dict[str, float]] = {}
    rows = conn.execute(
        f"""
        SELECT
          COALESCE(effort, '(unset)') AS effort,
          SUM(CASE WHEN request_class='main' THEN 1 ELSE 0 END) AS n,
          {', '.join('SUM(' + k + ') AS ' + k for k in SP_KEYS)}
        FROM requests
        WHERE date >= ? AND date < ? AND request_class='main'
        GROUP BY effort
        """,
        (start, end_excl),
    ).fetchall()
    for r in rows:
        eff = r[0]
        n = r[1] or 0
        d: dict[str, float] = {"n": n}
        for i, k in enumerate(SP_KEYS, start=2):
            v = r[i] or 0
            d[k + "_per_1k"] = (1000.0 * v / n) if n > 0 else 0.0
        out[eff] = d
    return out


def _daily_basics(conn: sqlite3.Connection, date: str) -> dict[str, int]:
    """One-shot for header counters: total reqs, divergence, 4xx/5xx/429."""
    r = conn.execute(
        """
        SELECT request_count, model_divergence_count,
               status_4xx, status_5xx, status_429
        FROM daily_rollup WHERE date = ?
        """,
        (date,),
    ).fetchone()
    if not r:
        return {"reqs": 0, "div": 0, "s4xx": 0, "s5xx": 0, "s429": 0}
    return {"reqs": r[0] or 0, "div": r[1] or 0,
            "s4xx": r[2] or 0, "s5xx": r[3] or 0, "s429": r[4] or 0}


def _arrow(today: float, base: float) -> str:
    """Return a ↑ / ↓ / ~ glyph depending on how today compares to the
    7-day baseline. Threshold is ``2× baseline + 5/1k`` for ↑ — needs
    both a ratio change AND a meaningful absolute floor so a baseline
    of 0.1 doesn't fire ↑ on every random hit.
    """
    if today >= max(2 * base, base + 5):
        return "↑"
    if today <= base / 2 and base >= 5:
        return "↓"
    return ""


def _format_line(date: str, basics: dict, today_sp: dict, base_sp: dict) -> str:
    parts = [f"[{date}]", f"reqs={basics['reqs']}"]
    if basics["div"]:
        parts.append(f"DIVERG={basics['div']}!")
    if basics["s5xx"] or basics["s429"]:
        parts.append(f"5xx={basics['s5xx']} 429={basics['s429']}")
    # Per-effort: report ownership-dodging + permission-seeking per 1k.
    # These are the two with enough signal-to-noise to flag drift.
    efforts = sorted(set(today_sp) | set(base_sp))
    for eff in efforts:
        if eff in ("(unset)",):
            continue
        t = today_sp.get(eff, {})
        b = base_sp.get(eff, {})
        n = t.get("n", 0)
        if n < 30:  # too small to mean anything
            continue
        ownT = t.get("sp_ownership_dodging_per_1k", 0.0)
        ownB = b.get("sp_ownership_dodging_per_1k", 0.0)
        permT = t.get("sp_permission_seeking_per_1k", 0.0)
        permB = b.get("sp_permission_seeking_per_1k", 0.0)
        parts.append(
            f"{eff}(n={n}): ownD={ownT:.1f}{_arrow(ownT, ownB)}"
            f"/permS={permT:.1f}{_arrow(permT, permB)} per1k"
        )
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="~/.claude/claude-hooks-proxy/stats.db")
    p.add_argument("--date", default=_today_utc(),
                   help="UTC date YYYY-MM-DD; default = today UTC")
    p.add_argument("--baseline-days", type=int, default=7,
                   help="prior-N-days window for ↑/↓ comparison")
    args = p.parse_args(argv)

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"proxy_health: db not found at {db_path}", file=sys.stderr)
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as e:
        print(f"proxy_health: cannot open db: {e}", file=sys.stderr)
        return 0

    try:
        basics = _daily_basics(conn, args.date)
        today_sp = _per_effort_sp(conn, args.date)
        base_sp = _baseline_sp(conn, args.date, days=args.baseline_days)
    finally:
        conn.close()

    print(_format_line(args.date, basics, today_sp, base_sp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
