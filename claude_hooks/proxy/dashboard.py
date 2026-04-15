"""
Read-only dashboard over ``stats.db``.

Stdlib-only ``ThreadingHTTPServer`` so it composes with the existing
proxy service architecture (systemd unit, hardening, same Python).
Separate server + port from the proxy so a failure in one doesn't
take down the other, and so scraping the dashboard can't slow
down request forwarding.

Routes:

  GET /                   HTML view (single page, auto-refresh)
  GET /api/summary.json   today + 7d aggregate, rate-limit state
  GET /api/daily.json     per-day rollup (?days=14 default)
  GET /api/agents.json    per-agent rollup (?date= default=today UTC)
  GET /api/models.json    per-model rollup (?date= default=today UTC)
  GET /api/betas.json     beta-feature tokens seen (sorted by recency)
  GET /api/ratelimit.json latest ratelimit-state.json + burn rate
  GET /healthz            "OK"

Never mutates the DB, never touches upstream. Safe to run unattended.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import signal
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from claude_hooks.config import expand_user_path, load_config

log = logging.getLogger("claude_hooks.proxy.dashboard")


# ---------------------------------------------------------------------- #
# Data queries
# ---------------------------------------------------------------------- #
def _utc_today() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``stats.db`` read-only — no schema migrations, no writes."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _query_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    today = _utc_today()
    # Today
    today_row = conn.execute(
        "SELECT * FROM daily_rollup WHERE date = ?", (today,),
    ).fetchone()
    # Last 7 days (inclusive of today).
    seven = conn.execute(
        """SELECT
             SUM(request_count) as requests,
             SUM(warmup_count) as warmups,
             SUM(warmup_blocked_count) as warmups_blocked,
             SUM(total_input_tokens) as input_tokens,
             SUM(total_output_tokens) as output_tokens,
             SUM(total_cache_creation_tokens) as cache_creation,
             SUM(total_cache_read_tokens) as cache_read,
             SUM(status_429) as status_429,
             SUM(status_5xx) as status_5xx,
             SUM(thinking_request_count) as thinking_requests,
             SUM(total_thinking_signature_bytes) as thinking_sig_bytes,
             SUM(total_thinking_visible_delta_count) as thinking_visible,
             SUM(total_thinking_redacted_delta_count) as thinking_redacted,
             SUM(total_tool_read_count) as tool_read,
             SUM(total_tool_edit_count) as tool_edit,
             SUM(total_tool_research_count) as tool_research,
             SUM(total_tool_mutation_count) as tool_mutation,
             SUM(total_tool_total_count) as tool_total
           FROM daily_rollup
           WHERE date >= date(?, '-6 days')""",
        (today,),
    ).fetchone()
    today_d = _row_to_dict(today_row)
    seven_d = _row_to_dict(seven)
    return {
        "today": _add_tool_ratios(today_d),
        "last_7d": _add_tool_ratios(seven_d, keys=("tool_read", "tool_edit",
                                                    "tool_research", "tool_mutation",
                                                    "tool_total")),
    }


def _add_tool_ratios(
    row: Optional[dict[str, Any]],
    *,
    keys: tuple = ("total_tool_read_count", "total_tool_edit_count",
                   "total_tool_research_count", "total_tool_mutation_count",
                   "total_tool_total_count"),
) -> Optional[dict[str, Any]]:
    """Attach stellaraccident-style ratios to a rollup dict.

    keys = (read, edit, research, mutation, total) — same semantics
    either way; just different column names in daily vs 7d rollups.
    """
    if row is None:
        return None
    r_read = row.get(keys[0]) or 0
    r_edit = row.get(keys[1]) or 0
    r_research = row.get(keys[2]) or 0
    r_mutation = row.get(keys[3]) or 0
    r_total = row.get(keys[4]) or 0
    row["read_edit_ratio"] = (r_read / r_edit) if r_edit > 0 else None
    row["research_mutation_ratio"] = (
        r_research / r_mutation if r_mutation > 0 else None
    )
    row["read_pct"] = (r_read / r_total) if r_total > 0 else None
    row["edit_pct"] = (r_edit / r_total) if r_total > 0 else None
    return row


def _query_daily(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    today = _utc_today()
    rows = conn.execute(
        "SELECT * FROM daily_rollup "
        "WHERE date >= date(?, ?) ORDER BY date DESC",
        (today, f"-{max(1, days) - 1} days"),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _query_agents(conn: sqlite3.Connection, date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_rollup WHERE date = ? "
        "ORDER BY request_count DESC", (date,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _query_models(conn: sqlite3.Connection, date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM model_rollup WHERE date = ? "
        "ORDER BY request_count DESC", (date,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _query_betas(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """All distinct beta-feature tokens observed, with first/last-seen
    timestamps. Good for catching feature rollouts (metric B8).
    """
    # Group by each token across all requests where beta_features is set.
    rows = conn.execute(
        "SELECT beta_features, MIN(ts) as first_seen, MAX(ts) as last_seen, "
        "COUNT(*) as n FROM requests "
        "WHERE beta_features IS NOT NULL GROUP BY beta_features "
        "ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    # Flatten the CSV strings into per-token stats.
    tally: dict[str, dict[str, Any]] = {}
    for r in rows:
        tokens = [t for t in (r["beta_features"] or "").split(",") if t]
        for t in tokens:
            e = tally.setdefault(
                t, {"token": t, "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"], "requests": 0},
            )
            if r["first_seen"] < e["first_seen"]:
                e["first_seen"] = r["first_seen"]
            if r["last_seen"] > e["last_seen"]:
                e["last_seen"] = r["last_seen"]
            e["requests"] += r["n"]
    return sorted(tally.values(), key=lambda e: e["last_seen"], reverse=True)


def _load_ratelimit_state(state_path: Path) -> Optional[dict[str, Any]]:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _compute_burn(state: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Rough burn projection: given the current utilization + reset
    timestamp, how long until the window fills?

    Returns a dict with per-window (5h, 7d) projection fields or
    ``None`` when data is missing. Only the ``anthropic-ratelimit-
    unified-*`` percentages and reset unix timestamps are needed.
    """
    if not isinstance(state, dict):
        return {"five_hour": None, "seven_day": None}
    now = _dt.datetime.utcnow()

    def project(util: Optional[float], reset_ts: Optional[int],
                window_len_s: float) -> Optional[dict]:
        if util is None or reset_ts is None:
            return None
        try:
            reset = _dt.datetime.utcfromtimestamp(int(reset_ts))
        except (OSError, ValueError, OverflowError):
            return None
        # Seconds elapsed in the CURRENT window (reset_ts is the next
        # rollover). ``window_len_s`` is either 5h or 7d — use the
        # known window length rather than trusting reset-now math.
        remaining_s = (reset - now).total_seconds()
        elapsed_s = max(1.0, window_len_s - remaining_s)
        # Utilization per second so far.
        burn_rate = util / elapsed_s   # fraction / s
        remaining_util = max(0.0, 1.0 - util)
        if burn_rate <= 0:
            eta_s: Optional[float] = None
        else:
            eta_s = remaining_util / burn_rate
        return {
            "utilization": util,
            "reset_utc": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reset_in_s": int(remaining_s),
            "elapsed_s": int(elapsed_s),
            "burn_per_hour": burn_rate * 3600,
            "eta_to_full_s": None if eta_s is None else int(eta_s),
            "will_exhaust_before_reset": (
                eta_s is not None and eta_s < remaining_s
            ),
        }

    five = project(
        state.get("five_hour_utilization"),
        _int_or_none(state.get("raw_headers", {}).get(
            "anthropic-ratelimit-unified-5h-reset")),
        window_len_s=5 * 3600,
    )
    seven = project(
        state.get("seven_day_utilization"),
        _int_or_none(state.get("raw_headers", {}).get(
            "anthropic-ratelimit-unified-7d-reset")),
        window_len_s=7 * 24 * 3600,
    )
    return {
        "five_hour": five,
        "seven_day": seven,
        "representative_claim": state.get("representative_claim"),
        "last_updated": state.get("last_updated"),
    }


def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------- #
# HTTP handler
# ---------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    db_path: Path = Path("~/.claude/claude-hooks-proxy/stats.db")
    ratelimit_path: Path = Path("~/.claude/claude-hooks-proxy/ratelimit-state.json")

    def log_message(self, fmt: str, *a: Any) -> None:  # noqa: A002
        log.debug("%s - - %s", self.address_string(), fmt % a)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send_html()
            elif parsed.path == "/favicon.ico" or parsed.path == "/favicon.svg":
                self._send_favicon()
            elif parsed.path == "/healthz":
                self._send_text("OK\n")
            elif parsed.path == "/api/summary.json":
                self._send_json(lambda conn: _query_summary(conn),
                                with_ratelimit=True)
            elif parsed.path == "/api/daily.json":
                days = int(qs.get("days", ["14"])[0])
                self._send_json(lambda conn: _query_daily(conn, days))
            elif parsed.path == "/api/agents.json":
                date = qs.get("date", [_utc_today()])[0]
                self._send_json(lambda conn: _query_agents(conn, date))
            elif parsed.path == "/api/models.json":
                date = qs.get("date", [_utc_today()])[0]
                self._send_json(lambda conn: _query_models(conn, date))
            elif parsed.path == "/api/betas.json":
                self._send_json(lambda conn: _query_betas(conn))
            elif parsed.path == "/api/ratelimit.json":
                state = _load_ratelimit_state(self.ratelimit_path)
                self._send_plain_json({
                    "state": state,
                    "burn": _compute_burn(state),
                })
            else:
                self._send_text("not found\n", status=404)
        except sqlite3.OperationalError as e:
            # DB not there yet / locked briefly — dashboard must never
            # 500; return a readable error.
            self._send_plain_json({"error": str(e)}, status=503)

    # -------- helpers -------- #
    def _send_json(self, query, *, with_ratelimit: bool = False) -> None:
        conn = _connect_ro(self.db_path)
        try:
            payload: Any = query(conn)
        finally:
            conn.close()
        if with_ratelimit:
            state = _load_ratelimit_state(self.ratelimit_path)
            payload = {
                **(payload if isinstance(payload, dict) else {"data": payload}),
                "ratelimit": state,
                "burn": _compute_burn(state),
            }
        self._send_plain_json(payload)

    def _send_plain_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, default=str, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_text(self, text: str, *, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_favicon(self) -> None:
        """Serve an inline SVG with the text ``CC`` so the browser tab
        is findable next to other localhost dashboards.
        """
        svg = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            b'<rect width="64" height="64" rx="12" fill="#58a6ff"/>'
            b'<text x="50%" y="50%" dy=".1em" text-anchor="middle" '
            b'dominant-baseline="middle" font-family="SF Mono, Menlo, Consolas, '
            b'monospace" font-size="28" font-weight="700" fill="#0d1117">CC</text>'
            b'</svg>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(svg)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self) -> None:
        body = _render_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------- #
# HTML view
# ---------------------------------------------------------------------- #
_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-hooks proxy dashboard</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e;
    --card: #161b22; --border: #30363d; --accent: #58a6ff;
    --ok: #3fb950; --warn: #d29922; --bad: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px 20px 48px;
    font: 13px/1.4 -apple-system, "SF Mono", Menlo, Consolas, monospace;
    background: var(--bg); color: var(--fg);
  }
  h1 { font-size: 15px; margin: 0 0 4px; font-weight: 600; }
  h2 { font-size: 13px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .subtitle { color: var(--muted); font-size: 11px; margin-bottom: 20px; }
  .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: auto; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  /* Right-align numeric columns at both header and cell level so
     labels line up over their data. */
  th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .legend {
    margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border);
    color: var(--muted); font-size: 10.5px; line-height: 1.6;
  }
  .legend code { background: var(--bg); padding: 0 4px; margin-right: 2px; color: var(--fg); }
  .pct { display: inline-block; padding: 1px 6px; border-radius: 3px; background: var(--border); color: var(--fg); font-size: 11px; }
  .pct-ok { background: #18381f; color: var(--ok); }
  .pct-warn { background: #3d2d07; color: var(--warn); }
  .pct-bad { background: #431b1d; color: var(--bad); }
  .big { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
  .bar { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; margin-top: 4px; }
  .bar-fill { height: 100%; background: var(--accent); transition: width 0.3s; }
  .bar-fill.warn { background: var(--warn); }
  .bar-fill.bad { background: var(--bad); }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  code { background: var(--border); padding: 1px 5px; border-radius: 3px; font-size: 11px; }
  .tag { display: inline-block; background: var(--border); color: var(--fg); padding: 1px 7px; border-radius: 10px; font-size: 10px; margin: 1px 2px 1px 0; }
  .err { color: var(--bad); }
  footer { margin-top: 24px; color: var(--muted); font-size: 11px; text-align: center; }
</style>
</head>
<body>
<h1>claude-hooks proxy dashboard</h1>
<div class="subtitle">auto-refresh 60s · stats.db read-only · no auth, localhost only</div>

<div class="grid">
  <div class="card"><h2>today</h2><div id="today-card">loading…</div></div>
  <div class="card"><h2>rate limits</h2><div id="ratelimit-card">loading…</div></div>
  <div class="card"><h2>last 7 days</h2><div id="seven-card">loading…</div></div>
</div>

<div class="grid" style="margin-top:12px">
  <div class="card"><h2>thinking (today)</h2><div id="thinking-card">loading…</div></div>
  <div class="card"><h2>tool-use canaries (today)</h2><div id="tools-card">loading…</div></div>
</div>

<div class="card" style="margin-top:12px"><h2>per day (last 14)</h2><div id="daily-table">…</div></div>

<div class="grid" style="margin-top:12px">
  <div class="card"><h2>agents today</h2><div id="agents-table">…</div></div>
  <div class="card"><h2>models today</h2><div id="models-table">…</div></div>
</div>

<div class="card" style="margin-top:12px"><h2>beta features seen</h2><div id="betas-block">…</div></div>

<footer>claude-hooks proxy dashboard · <span id="last-load"></span></footer>

<script>
function fmt(n) {
  if (n == null) return "—";
  if (typeof n !== "number") return String(n);
  if (n >= 1e9) return (n/1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(1) + "k";
  return n.toLocaleString();
}
function pct(f) {
  if (f == null) return "—";
  const p = Math.round(f * 100);
  let cls = "pct-ok";
  if (p >= 80) cls = "pct-bad";
  else if (p >= 50) cls = "pct-warn";
  return `<span class="pct ${cls}">${p}%</span>`;
}
function bar(f) {
  if (f == null) return "";
  const p = Math.min(100, Math.max(0, f * 100));
  let cls = "";
  if (p >= 80) cls = "bad";
  else if (p >= 50) cls = "warn";
  return `<div class="bar"><div class="bar-fill ${cls}" style="width:${p.toFixed(1)}%"></div></div>`;
}
function dur(s) {
  if (s == null) return "—";
  if (s < 0) return "passed";
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s/60) + "m";
  if (s < 86400) return (s/3600).toFixed(1) + "h";
  return (s/86400).toFixed(1) + "d";
}
async function j(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(url + " → " + r.status);
  return r.json();
}
function renderToday(d) {
  if (!d) return "<div class='muted'>no data yet</div>";
  const hit = d.cache_hit_rate;
  return `
    <div class="big">${fmt(d.request_count)} reqs</div>
    <dl class="kv">
      <dt>warmups blocked</dt><dd>${fmt(d.warmup_blocked_count)} / ${fmt(d.warmup_count)}</dd>
      <dt>in / out tokens</dt><dd>${fmt(d.total_input_tokens)} / ${fmt(d.total_output_tokens)}</dd>
      <dt>cache hit rate</dt><dd>${hit == null ? "—" : (hit*100).toFixed(1) + "%"}</dd>
      <dt>cache tokens (r/w)</dt><dd>${fmt(d.total_cache_read_tokens)} / ${fmt(d.total_cache_creation_tokens)}</dd>
      <dt>model divergences</dt><dd>${fmt(d.model_divergence_count)}</dd>
      <dt>status 2xx/4xx/5xx/429</dt><dd>${d.status_2xx}/${d.status_4xx}/${d.status_5xx}/${d.status_429}</dd>
    </dl>
  `;
}
function renderThinkingCard(d) {
  if (!d) return "<div class='muted'>no data yet</div>";
  const thrc = d.thinking_request_count || 0;
  const thvis = d.total_thinking_visible_delta_count || 0;
  const thred = d.total_thinking_redacted_delta_count || 0;
  const thDelta = thvis + thred;
  const visPct = thDelta > 0 ? thvis / thDelta : null;
  const avgSig = thrc > 0 ? (d.total_thinking_signature_bytes || 0) / thrc : null;
  return `
    <dl class="kv">
      <dt>thinking reqs</dt><dd>${fmt(thrc)} / ${fmt(d.request_count)}</dd>
      <dt>Σ signature bytes</dt><dd>${fmt(d.total_thinking_signature_bytes)}</dd>
      <dt>avg sig / req</dt><dd>${avgSig == null ? "—" : fmt(Math.round(avgSig))}</dd>
      <dt>visible deltas</dt><dd>${fmt(thvis)}</dd>
      <dt>redacted deltas</dt><dd>${fmt(thred)}</dd>
      <dt>visible %</dt><dd>${visPct == null ? "—" : (visPct*100).toFixed(1) + "%"}</dd>
    </dl>
    <div class="legend">
      Depth proxy — <code>signature bytes</code> correlate 0.971 with
      thinking content length (stellaraccident, #42796). Under
      <code>redact-thinking-2026-02-12</code> visible deltas stay at 0;
      depth only measurable via signature.
    </div>
  `;
}
function renderToolsCard(d) {
  if (!d) return "<div class='muted'>no data yet</div>";
  const total = d.total_tool_total_count || 0;
  if (total === 0) return "<div class='muted'>no tool-use data yet</div>" + TOOL_LEGEND;
  const re = d.read_edit_ratio;
  const rm = d.research_mutation_ratio;
  const readPct = d.read_pct;
  const editPct = d.edit_pct;
  return `
    <dl class="kv">
      <dt>total tool calls</dt><dd>${fmt(total)}</dd>
      <dt>Read : Edit</dt><dd>${re == null ? "—" : re.toFixed(2)}</dd>
      <dt>Research : Mutation</dt><dd>${rm == null ? "—" : rm.toFixed(2)}</dd>
      <dt>Read %</dt><dd>${readPct == null ? "—" : (readPct*100).toFixed(1) + "%"}</dd>
      <dt>Edit %</dt><dd>${editPct == null ? "—" : (editPct*100).toFixed(1) + "%"}</dd>
      <dt>Read / Edit</dt><dd>${fmt(d.total_tool_read_count)} / ${fmt(d.total_tool_edit_count)}</dd>
      <dt>Research / Mutation</dt><dd>${fmt(d.total_tool_research_count)} / ${fmt(d.total_tool_mutation_count)}</dd>
      <dt>Bash / Task</dt><dd>${fmt(d.total_tool_bash_count)} / ${fmt(d.total_tool_task_count)}</dd>
    </dl>${TOOL_LEGEND}
  `;
}
const TOOL_LEGEND = `
  <div class="legend">
    stellaraccident / #42796 canary: Read:Edit dropped 6.6 → 2.0 during the
    Mar regression. Read % &lt; 30 + Research:Mutation &lt; 3 signals
    "edit-first" behavior. Research = Read+Grep+Glob+WebFetch+WebSearch;
    Mutation = Edit+Write+MultiEdit+NotebookEdit.
  </div>
`;
function renderSeven(s) {
  if (!s) return "<div class='muted'>no data</div>";
  return `
    <dl class="kv">
      <dt>requests</dt><dd>${fmt(s.requests)}</dd>
      <dt>warmups (blocked / total)</dt><dd>${fmt(s.warmups_blocked)} / ${fmt(s.warmups)}</dd>
      <dt>input tokens</dt><dd>${fmt(s.input_tokens)}</dd>
      <dt>output tokens</dt><dd>${fmt(s.output_tokens)}</dd>
      <dt>cache read</dt><dd>${fmt(s.cache_read)}</dd>
      <dt>cache creation</dt><dd>${fmt(s.cache_creation)}</dd>
      <dt>429s</dt><dd>${fmt(s.status_429)}</dd>
      <dt>5xx</dt><dd>${fmt(s.status_5xx)}</dd>
      <dt>thinking reqs</dt><dd>${fmt(s.thinking_requests)}</dd>
    </dl>
  `;
}
function renderRateLimit(rl, burn) {
  if (!rl) return "<div class='muted'>no rate-limit snapshot yet</div>";
  const f5 = burn?.five_hour, f7 = burn?.seven_day;
  const row = (label, b) => b ? `
    <dt>${label}</dt><dd>${pct(b.utilization)}${bar(b.utilization)}</dd>
    <dt class="muted">${label} resets</dt><dd class="muted">${dur(b.reset_in_s)}</dd>
    <dt class="muted">${label} ETA full</dt><dd class="muted">${b.eta_to_full_s == null ? "—" : dur(b.eta_to_full_s)}${b.will_exhaust_before_reset ? " <span class='err'>⚠</span>" : ""}</dd>
  ` : `<dt>${label}</dt><dd class="muted">—</dd>`;
  return `
    <dl class="kv">
      ${row("5h", f5)}
      ${row("7d", f7)}
      <dt class="muted">binding</dt><dd class="muted">${burn?.representative_claim ?? "—"}</dd>
      <dt class="muted">last update</dt><dd class="muted">${rl.last_updated ?? "—"}</dd>
    </dl>
  `;
}
const LEGEND_DAILY = `
  <div class="legend">
    <code>REQS</code> total upstream requests ·
    <code>WMB</code> Warmup requests blocked (upstream never called) ·
    <code>2XX / 5XX / 429</code> response status buckets ·
    <code>HIT</code> cache-read / (cache-read + cache-creation) ratio ·
    <code>IN / OUT</code> input vs output tokens ·
    <code>CR</code> cache-read tokens ·
    <code>THK</code> requests that emitted a thinking content block.
  </div>
`;
const LEGEND_AGENTS = `
  <div class="legend">
    <code>AGENT</code> parsed from the request's persona block ·
    <code>TYPE</code> <code>main</code> / <code>subagent</code> / <code>warmup</code> ·
    <code>REQS</code> count · <code>WMB</code> Warmups blocked ·
    <code>IN / OUT</code> tokens · <code>CR</code> cache-read tokens.
  </div>
`;
const LEGEND_MODELS = `
  <div class="legend">
    <code>MODEL</code> delivered (falls back to requested on errors) ·
    <code>REQS / IN / OUT / CR</code> as above.
  </div>
`;
function renderDailyTable(rows) {
  if (!rows || !rows.length) return "<div class='muted'>no data</div>" + LEGEND_DAILY;
  const keys = [
    ["date", "date", false],
    ["request_count", "reqs", true],
    ["warmup_blocked_count", "wmb", true],
    ["status_2xx", "2xx", true],
    ["status_5xx", "5xx", true],
    ["status_429", "429", true],
    ["cache_hit_rate", "hit", true],
    ["total_input_tokens", "in", true],
    ["total_output_tokens", "out", true],
    ["total_cache_read_tokens", "cr", true],
    ["thinking_request_count", "thk", true],
  ];
  const th = keys.map(([_, l, n]) => `<th${n ? ' class="num"' : ""}>${l}</th>`).join("");
  const trs = rows.map(r => {
    const cells = keys.map(([k, _, n]) => {
      const v = r[k];
      if (k === "date") return `<td>${v}</td>`;
      if (k === "cache_hit_rate") return `<td class="num">${v == null ? "—" : (v*100).toFixed(1) + "%"}</td>`;
      return `<td class="num">${fmt(v)}</td>`;
    }).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>` + LEGEND_DAILY;
}
function renderAgentsTable(rows) {
  if (!rows || !rows.length) return "<div class='muted'>no S2 data yet today</div>" + LEGEND_AGENTS;
  const trs = rows.map(r => `
    <tr>
      <td>${r.agent_name || "—"}</td>
      <td>${r.agent_type || "—"}</td>
      <td class="num">${fmt(r.request_count)}</td>
      <td class="num">${fmt(r.warmup_blocked_count)}</td>
      <td class="num">${fmt(r.input_tokens)}</td>
      <td class="num">${fmt(r.output_tokens)}</td>
      <td class="num">${fmt(r.cache_read_tokens)}</td>
    </tr>`).join("");
  const th = `<tr><th>agent</th><th>type</th><th class="num">reqs</th><th class="num">wmb</th><th class="num">in</th><th class="num">out</th><th class="num">cr</th></tr>`;
  return `<table><thead>${th}</thead><tbody>${trs}</tbody></table>` + LEGEND_AGENTS;
}
function renderModelsTable(rows) {
  if (!rows || !rows.length) return "<div class='muted'>no data</div>" + LEGEND_MODELS;
  const trs = rows.map(r => `
    <tr>
      <td>${r.model}</td>
      <td class="num">${fmt(r.request_count)}</td>
      <td class="num">${fmt(r.input_tokens)}</td>
      <td class="num">${fmt(r.output_tokens)}</td>
      <td class="num">${fmt(r.cache_read_tokens)}</td>
    </tr>`).join("");
  const th = `<tr><th>model</th><th class="num">reqs</th><th class="num">in</th><th class="num">out</th><th class="num">cr</th></tr>`;
  return `<table><thead>${th}</thead><tbody>${trs}</tbody></table>` + LEGEND_MODELS;
}
function renderBetas(rows) {
  if (!rows || !rows.length) return "<div class='muted'>no beta features seen yet</div>";
  const tags = rows.map(r =>
    `<span class="tag" title="first ${r.first_seen}\nlast ${r.last_seen}\nreqs ${r.requests}">${r.token}</span>`
  ).join("");
  return `<div>${tags}</div>`;
}
async function load() {
  try {
    const [summary, daily, agents, models, betas] = await Promise.all([
      j("/api/summary.json"),
      j("/api/daily.json?days=14"),
      j("/api/agents.json"),
      j("/api/models.json"),
      j("/api/betas.json"),
    ]);
    document.getElementById("today-card").innerHTML = renderToday(summary.today);
    document.getElementById("seven-card").innerHTML = renderSeven(summary.last_7d);
    document.getElementById("ratelimit-card").innerHTML = renderRateLimit(summary.ratelimit, summary.burn);
    document.getElementById("thinking-card").innerHTML = renderThinkingCard(summary.today);
    document.getElementById("tools-card").innerHTML = renderToolsCard(summary.today);
    document.getElementById("daily-table").innerHTML = renderDailyTable(daily);
    document.getElementById("agents-table").innerHTML = renderAgentsTable(agents);
    document.getElementById("models-table").innerHTML = renderModelsTable(models);
    document.getElementById("betas-block").innerHTML = renderBetas(betas);
    document.getElementById("last-load").textContent = "loaded " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("last-load").innerHTML = `<span class="err">${e.message}</span>`;
  }
}
load();
setInterval(load, 60_000);
</script>
</body></html>
"""


def _render_html() -> str:
    return _HTML


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #
def build_server(cfg: Optional[dict] = None) -> ThreadingHTTPServer:
    merged = cfg if cfg is not None else load_config()
    pcfg = (merged.get("proxy") or {})
    dcfg = (merged.get("proxy_dashboard") or {})
    host = dcfg.get("listen_host") or pcfg.get("listen_host", "127.0.0.1")
    port = int(dcfg.get("listen_port", 38081))

    log_dir = Path(expand_user_path(pcfg.get(
        "log_dir", "~/.claude/claude-hooks-proxy")))
    db_path = Path(expand_user_path(pcfg.get(
        "stats_db_path") or str(log_dir / "stats.db")))
    state_path = log_dir / "ratelimit-state.json"

    class _HandlerBound(_Handler):
        pass
    _HandlerBound.db_path = db_path
    _HandlerBound.ratelimit_path = state_path

    server = ThreadingHTTPServer((host, port), _HandlerBound)
    server.daemon_threads = True
    return server


def run(cfg: Optional[dict] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server = build_server(cfg)
    except OSError as e:
        print(f"claude-hooks-dashboard: bind failed: {e}", file=sys.stderr)
        return 1
    host, port = server.server_address
    print(
        f"claude-hooks-dashboard on http://{host}:{port}  "
        f"(db={_Handler.db_path})",
        file=sys.stderr,
    )

    # Same pattern as the proxy: serve in a background thread, handle
    # signals on the main thread without deadlock risk.
    stop_flag = threading.Event()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def _stop(_sig: int, _frame) -> None:
        print("\nshutting down…", file=sys.stderr)
        stop_flag.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stop_flag.is_set():
            stop_flag.wait(timeout=1.0)
    finally:
        try: server.shutdown()
        except Exception: pass
        try: server.server_close()
        except Exception: pass
    return 0


if __name__ == "__main__":
    sys.exit(run())
