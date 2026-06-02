# claude-hooks proxy (Phases P0 + P1) — setup

> **Forwarder uses httpx + HTTP/2 (since 2026-04-14).**
>
> Earlier versions of the forwarder opened a fresh HTTP/1.1 connection
> per request. Anthropic's edge 429's that connection profile — we
> observed 4 × 429 in 58 s on non-overlapping requests while the 5h
> / 7d unified budgets still showed `"allowed"`. Native Claude Code
> uses HTTP/2 multiplexing on a single connection; we now match that
> profile with a module-level `httpx.Client(http2=True)` + pool.
>
> Requires `httpx[http2]>=0.27` (installed automatically by
> `install.py` when `proxy.enabled: true`, or via
> `pip install 'httpx[http2]>=0.27'`).

Opt-in local HTTP proxy in front of `api.anthropic.com`. Hooks can't
see the raw HTTPS traffic; the proxy can. It logs one JSONL record per
upstream request and rides out upstream overload/throttle with a
spaced, deadline-bounded retry layer (see
[Retry / throttle resilience](#retry--throttle-resilience) below).
Response bodies always stream through **unmodified** — the proxy only
retries *before* any byte reaches Claude Code, so it never alters what
the client receives.

Design + phased roadmap: [PLAN-proxy-hook.md](./PLAN-proxy-hook.md).

## What you get in P0

- Every upstream request logged with: timestamp, method, path, status,
  duration, bytes in/out, model requested, model delivered, token
  `usage` block, rate-limit headers, `is_warmup` flag, synthetic-rate-
  limit flag.
- **Warmup detection** — `is_warmup: true` whenever the first user
  message is the literal string `"Warmup"`. Gives you a live counter
  instead of mining transcripts after the fact.
- **Synthetic rate-limit detection** — `synthetic: true` whenever the
  response carries `"model": "<synthetic>"`, i.e. Claude Code's
  client-side false rate limiter (bug B3 per ArkNill's #42796
  analysis).
- **Real rate-limit headers** captured verbatim under `rate_limit`
  (all `anthropic-ratelimit-*` / `x-ratelimit-*` / `retry-after`) —
  P1 will feed these into `scripts/weekly_token_usage.py` to
  auto-populate `--current-usage-pct`.

## Install via `install.py` (recommended)

`python3 install.py` orchestrates the entire setup. It first asks
the high-level question, then dispatches to the right per-OS
mechanism — no manual `systemctl` / `launchctl` / `schtasks` wiring
needed.

### Step 1 — pick the proxy mode

The installer prints:

```
==> claude-hooks API proxy
    Optional local HTTP proxy in front of api.anthropic.com.
    Adds: real weekly-limit %, Warmup token-drain block,
          rate-limit header capture, structured request logs.
    Can be installed locally on this host, or you can point
    this host at an existing proxy already running on the LAN.
    See docs/proxy.md.
  Use the API proxy? (current: no) [y/N]:
```

If you say **`n`**, nothing changes — `proxy.enabled` stays at
its current value, no service is installed, no env var is touched.

If you say **`y`**, you get a follow-up:

```
  Two options:
    [1] Install the proxy on THIS host.
        Service runs locally; Claude Code points at 127.0.0.1.
    [2] Use an existing proxy already on the network.
        Skip local install -- just set ANTHROPIC_BASE_URL on
        this host to the URL you supply.
  Choose [1/2] (default 1):
```

### Step 2a — local install (choice `1`)

The installer:

1. Sets `proxy.enabled = true` in `config/claude-hooks.json`.
2. Optionally writes `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`
   into `~/.claude/settings.json` under `env` (asks first; defaults
   to yes). Backs up the prior `settings.json` before overwriting.
   `listen_host: "0.0.0.0"` translates to `127.0.0.1` for the client
   side automatically (the proxy *binds* on all interfaces, but the
   *client* always uses loopback when same-host).
3. Checks that `httpx[http2]>=0.27` is importable in the conda env
   (or system python) and offers to pip-install it if not.
4. Installs the platform-native service + starts it:

   | OS | Mechanism | Service name |
   |----|-----------|--------------|
   | **Linux** (systemd) | Drops 4 units in `/etc/systemd/system/`, `daemon-reload`, `enable --now` | `claude-hooks-proxy.service` (+ `rollup.service` + `rollup.timer` + `dashboard.service`) |
   | **macOS** (launchd) | Writes `~/Library/LaunchAgents/com.claude-hooks.proxy.plist` (KeepAlive=true), `launchctl load -w` | `com.claude-hooks.proxy` |
   | **Windows** (Task Scheduler) | UAC-elevated `schtasks /Create /XML` for a logon-triggered task; ExecutionTimeLimit=PT0S; uses `pythonw.exe` + `run_proxy.py` to avoid a permanent cmd.exe console window | `claude-hooks-proxy` |

   Linux ships the full proxy + rollup + dashboard stack; macOS and
   Windows install just the proxy itself. The rollup/dashboard
   services are Linux conveniences and run fine standalone on the
   other OSes via `python -m claude_hooks.proxy.dashboard` etc. when
   you want them.

5. Template substitution replaces `__REPO_PATH__`, `__HOME__`, and
   (Windows) `__USER__` in the unit / plist / XML files before they
   land on disk.

### Step 2b — remote URL (choice `2`)

You're prompted for the URL of the existing proxy
(e.g. `http://192.168.178.2:38080`). The installer:

1. Sets `proxy.enabled = false` in `config/claude-hooks.json` so the
   per-OS service installer stays a no-op on this host.
2. Writes `ANTHROPIC_BASE_URL=<your-url>` into `~/.claude/settings.json`.
3. Trailing slashes are stripped; the URL must start with
   `http://` or `https://` (re-prompted otherwise).

This is the right path for multi-host setups where one machine
hosts the proxy and other machines just point at it.

### Idempotency, dry-run, non-interactive

- Re-running `install.py` is idempotent: existing service files /
  scheduled tasks are detected and left alone (re-installation is
  offered as an opt-in for the daemon; the proxy is leave-as-is by
  default to preserve any drop-in overrides).
- `--dry-run` prints the plan without writing anything.
- `--non-interactive` is a **silent no-op for the orchestrator** —
  config is preserved as-is so CI / automated installs don't flip
  someone's proxy mode unexpectedly. Per-OS service installers in
  non-interactive mode auto-accept their `[Y/n]` prompts when their
  config gate is already true.

## Manual install (escape hatch)

The orchestrator above is the path to take. This section is for when
you're managing the lifecycle yourself — packaging, debugging, or
running on an OS that doesn't fit one of the auto-detected patterns.

1. Edit `config/claude-hooks.json` (copy the `proxy` block from
   `config/claude-hooks.example.json` if missing):

   ```json
   "proxy": {
     "enabled": true,
     "listen_host": "127.0.0.1",
     "listen_port": 38080,
     "upstream": "https://api.anthropic.com",
     "timeout": 120.0,
     "log_requests": true,
     "log_dir": "~/.claude/claude-hooks-proxy",
     "log_retention_days": 14,
     "record_rate_limit_headers": true,
     "block_warmup": false
   }
   ```

2. Run the proxy in the foreground (`Ctrl-C` to stop):

   ```bash
   # POSIX shim (prefers conda env's python)
   bin/claude-hooks-proxy

   # Or directly
   python3 -m claude_hooks.proxy

   # Windows
   bin\claude-hooks-proxy.cmd
   # ...or via the windowless launcher (mirrors what the scheduled task uses)
   pythonw run_proxy.py
   ```

3. Point Claude Code at it via `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:38080"
     }
   }
   ```

   For multi-host (LAN-shared) setups, point each client's
   `ANTHROPIC_BASE_URL` at the host running the proxy
   (e.g. `http://192.168.178.2:38080`) and set
   `listen_host: "0.0.0.0"` on the host. The orchestrator's
   choice `2` ("use an existing proxy") sets exactly this URL on the
   client side without installing anything locally.

4. Open a new Claude Code session. Every request is now logged under
   `~/.claude/claude-hooks-proxy/YYYY-MM-DD.jsonl`.

## Operational notes

- **Default OFF.** Nothing spawns until `enabled: true` + the binary is
  running.
- **Local-only by default.** `listen_host: "127.0.0.1"` keeps it off
  the LAN. Don't change this unless you have a reason.
- **Streaming-safe.** SSE (extended thinking) responses stream
  chunk-by-chunk; the proxy only peeks the opening 4 KB for metadata,
  never buffers the whole body.
- **Auth headers pass through verbatim.** `x-api-key`, `authorization`,
  `anthropic-*` are never inspected or mutated.
- **Log rotation.** One file per UTC day; files older than
  `log_retention_days` are pruned hourly.

## Troubleshooting

- **Port in use.** Change `listen_port`. 38080 is the default and
  was free on solidPC + pandorum at time of writing.
- **TLS errors contacting `api.anthropic.com`.** Python's stdlib uses
  the system trust store; on Debian/Ubuntu make sure `ca-certificates`
  is installed.
- **Claude Code can't reach the proxy.** `curl http://127.0.0.1:38080/`
  should return a 502 with a JSON error body. If that works but CC
  doesn't, double-check the `ANTHROPIC_BASE_URL` env var is applied
  (open a fresh session after editing settings.json).
- **Turn it off.** Unset `ANTHROPIC_BASE_URL` in settings.json and
  stop the proxy process. Claude Code goes back to hitting the API
  directly on the next session.

## What's new in P1

- **SSE tail** captures the final `usage` block from the trailing
  `message_delta` event (the canonical billing numbers) instead of
  the estimate from `message_start`. Falls back to `message_start`
  when no `message_delta` arrives.
- **`stop_reason`** captured from `message_delta.delta.stop_reason`
  (`end_turn` / `tool_use` / `max_tokens` / …).
- **Rolling rate-limit state file** at
  `<log_dir>/ratelimit-state.json`. Atomic-replace write on every
  response that carries `anthropic-ratelimit-unified-*` headers.
- **`scripts/weekly_token_usage.py` auto-populates `--current-usage-pct`**
  from that state file. If the proxy is running and Claude Code has
  hit the API at least once this window, the `%Limit` column appears
  without any manual input. Footer notes when the value came from the
  proxy (with source timestamp) so you know it's live, not stale.
- **`--proxy-state PATH`** new CLI flag to point the script at a
  non-default state file (testing / multi-host setups).

## What's new in P3 (block_warmup)

- **`block_warmup: true`** short-circuits Warmup requests at the
  proxy. Upstream is never called. Returns a minimal
  Anthropic-compatible reply:
  - Non-streaming → JSON `{"type": "message", "stop_reason": "end_turn", "usage": {zeros}}`
  - Streaming → valid SSE (`message_start` → `content_block_*` →
    `message_delta` → `message_stop`)
- **Log line gains `warmup_blocked: true`** so you can count savings.
- **Header marker** `X-Claude-Hooks-Proxy: warmup-blocked` on every
  blocked reply for quick `curl` verification.

Enable in `config/claude-hooks.json`:

```json
"proxy": {
  "enabled": true,
  "block_warmup": true
}
```

Then you can drop `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` from
`~/.claude/settings.json` and get Ctrl+B + Bash `run_in_background`
back, because the proxy is killing Warmup on its own.

## What's new in P4 (statusline segment)

A compact segment script at `scripts/statusline_usage.py` reads the
proxy's `ratelimit-state.json` and prints one short line suitable
for embedding in a custom `statusLine`:

```bash
python3 scripts/statusline_usage.py            # emoji format (default)
python3 scripts/statusline_usage.py --format plain
python3 scripts/statusline_usage.py --format ascii
python3 scripts/statusline_usage.py --state-file /custom/path.json
```

Output:
- `5h 42%` — only 5h window known
- `5h 42% · 7d 18%` — both windows present
- `5h 65% ⚠` — ≥ 50% on the binding window
- `5h 85% 🔴` — ≥ 80%
- empty string on stale / missing / broken state (never crashes)

Exit code is always 0 — the script is safe to call from any
statusline runner.

### Wiring example (bash statusline)

```bash
usage_seg=$(python3 /path/to/claude-hooks/scripts/statusline_usage.py 2>/dev/null)
[ -n "$usage_seg" ] && usage_part=" | ${usage_seg}"
printf "%s%s" "$other_parts" "$usage_part"
```

## Plan status

All four proxy phases (P0 / P1 / P2-block / P4) shipped. See
`docs/PLAN-proxy-hook.md` for per-phase checklists. Stats roadmap
(S1–S5) in `docs/PLAN-stats-sqlite.md`; S1 (SQLite rollup), S2
(body-parser + agent rollup), S3 (thinking-depth metrics), and S4
(dashboard) are live.

## Stats rollup (S1)

`scripts/proxy_rollup.py` ingests the daily JSONL files into a
persistent SQLite database at `proxy.stats_db_path` (default
`~/.claude/claude-hooks-proxy/stats.db`). Tables:

- `requests` — one row per proxied request, with S2/S3 columns as
  nullable placeholders so future phases don't need a migration.
- `daily_rollup` — per-day counts (requests, Warmups, Warmup blocks,
  status buckets including 429, model-divergence count, token totals,
  cache hit rate, byte totals, duration totals).
- `session_rollup`, `model_rollup` — groupings of the same data for
  drill-down.
- `ratelimit_windows` — time series of `anthropic-ratelimit-unified-*`
  snapshots (5h + 7d utilization, representative claim, reset unix ts).
- `ingestion_state` — per-file cursor so re-runs are cheap and
  idempotent.

### Manual

```bash
python3 scripts/proxy_rollup.py               # ingest + rebuild rollups
python3 scripts/proxy_rollup.py --dry-run     # show pending lines, write nothing
python3 scripts/proxy_rollup.py --since 2026-04-14
python3 scripts/proxy_rollup.py --json        # machine output
```

### Dashboard (S4)

`claude_hooks/proxy/dashboard.py` serves a read-only single-page
view of the rollups on port `38081` (override via
`proxy_dashboard.listen_port`). Stdlib-only, no external assets —
the HTML is embedded in the module.

Routes:

| Path | Returns |
|---|---|
| `GET /` | HTML dashboard (auto-refresh 60 s) |
| `GET /api/summary.json` | today + last-7d totals + rate-limit burn projection |
| `GET /api/daily.json?days=14` | per-day rollup series |
| `GET /api/agents.json?date=YYYY-MM-DD` | per-agent breakdown (default: today UTC) |
| `GET /api/models.json?date=YYYY-MM-DD` | per-model breakdown |
| `GET /api/betas.json` | distinct `anthropic-beta` tokens observed, with first/last-seen ts |
| `GET /api/ratelimit.json` | latest `ratelimit-state.json` + 5h/7d burn projection |
| `GET /healthz` | `OK` (liveness probe) |

Manual:

```bash
python3 -m claude_hooks.proxy.dashboard          # foreground
bin/claude-hooks-dashboard                       # POSIX shim
curl -s http://127.0.0.1:38081/api/summary.json | jq .
```

systemd:

```bash
sudo cp systemd/claude-hooks-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-hooks-dashboard
```

Burn projection is pure math on the latest unified-ratelimit
snapshot: given the observed utilization, reset unix-ts, and the
fixed window length (5 h or 7 d), we compute burn rate per hour and
ETA to exhaustion. `will_exhaust_before_reset: true` is the canary
flag the HTML view highlights.

### systemd timer (recommended)

Install both unit files from `systemd/`:

```bash
sudo cp systemd/claude-hooks-rollup.service /etc/systemd/system/
sudo cp systemd/claude-hooks-rollup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-hooks-rollup.timer
systemctl list-timers claude-hooks-rollup.timer
```

Runs every 5 min after boot (with `Persistent=true` so a missed tick
on wake fires once). The rollup is idempotent; skipping or overrunning
a tick is harmless.

## Stop-phrase behaviour canaries (S5)

Opt-in pattern matcher that scans the assistant's `text_delta` output
for the stop-phrase categories from stellaraccident's #42796 analysis
(ownership dodging, permission seeking, premature stopping, known-
limitation labeling, session-length excuses, simplest-fix, reasoning
reversal, self-admitted errors).

Enable in config:

```json
"proxy": {
  "scan_stop_phrases": true,
  "stop_phrases_file": null            // null = repo default
}
```

The phrase catalog lives at `config/stop_phrases.yaml` — case-
insensitive regexes grouped by category. Add or tweak phrases there
without touching code. Off by default; the localhost dashboard is
the only consumer.

Runtime: one `StopPhraseScanner` per response, fed `text_delta`
text as bytes stream through the proxy. Counts land in the JSONL
line as `stop_phrase_counts: {category: n, ...}` (null on turns that
match nothing, keeping the log compact). Daily rollup gains per-
category totals; dashboard renders the "behavior canaries" card with
rate per 1K tool calls.

## Forwarder: httpx + HTTP/2

The proxy forwards via a module-level `httpx.Client(http2=True)`
with a keep-alive pool. Calls share one TCP+TLS connection that
multiplexes HTTP/2 streams, matching the connection profile
native Claude Code presents to `api.anthropic.com`.

### Why this matters

An earlier `http.client.HTTPSConnection` per-request implementation
tripped Anthropic's edge 429 gate on bursts — even when the unified
5h / 7d budgets reported `"allowed"`. Live evidence from
2026-04-14 (solidPC, 16:20–16:22 UTC):

| timestamp | status | dur | size | concurrent |
|---|---|---:|---:|---:|
| 16:20:55 | 200 | 23 s (stream) | 1856 KB | — |
| 16:20:59 | **429** | 1.3 s | 1858 KB | 1 |
| 16:21:00 | 200 | 2.0 s | 1860 KB | — |
| 16:21:01 | **429** | 1.2 s | 1860 KB | **0** |
| 16:21:04 | 200 | 1.4 s | 2011 KB | — |
| 16:21:05 | **429** | 1.6 s | 2011 KB | **0** |
| 16:21:57 | **429** | 1.6 s | 1950 KB | **0** |

429s with `concurrent=0` on requests *smaller* than adjacent 200s,
no `anthropic-ratelimit-unified-*` headers on the 429 responses →
the differentiator was connection profile, not rate/size/overlap.

The HTTP/2 pooled client lands us in the same "one well-behaved
client" bucket as native CC, so the edge gate doesn't trip.

### Dependency

`httpx[http2]>=0.27` is listed in `requirements.txt` and is
installed automatically by `install.py` when `proxy.enabled: true`.
For manual installs:

```bash
pip install 'httpx[http2]>=0.27'
```

Without it the proxy raises `ImportError` at startup with a
pointer to this section.

### Tuning

Pool defaults in `forwarder.py`:

- `max_keepalive_connections=10`, `max_connections=20`
- `keepalive_expiry=60.0` s idle before the pool retires a conn
  (`CLAUDE_HOOKS_PROXY_KEEPALIVE_SEC`). Short so stale connections
  retire ahead of upstream's silent idle-drop.
- `connect=10.0` s, `timeout=<proxy.timeout>` s for read/write
- `trust_env=False` — we ignore `HTTPS_PROXY` / `NO_PROXY` from the
  environment because the host may have those set pointing *at us*

The **retry** behaviour (backoff, deadline, circuit breaker) is tuned
separately — see the next section.

## Retry / throttle resilience

Anthropic's edge throttles connections under load (it bites hardest
once a second session is active). The proxy absorbs that turbulence so
Claude Code's own client never exhausts *its* retry budget — but it
does so **politely**, with proper spacing, rather than hammering an
already-overloaded backend.

The retry *policy* lives in `claude_hooks/proxy/retry.py` (a pure,
unit-tested module); `forwarder.py` owns only the HTTP loop +
`time.sleep`.

**What it does**

- **Jittered exponential backoff.** Each retry waits a full-jittered
  `uniform(0, min(base·2^attempt, max))` — spaced like a well-behaved
  client, not the old sub-second fixed step.
- **Honors `Retry-After`.** When upstream sends `Retry-After` (integer
  seconds *or* HTTP-date) the proxy waits exactly that long (clamped to
  a cap), instead of its own backoff.
- **Wall-clock deadline is the primary bound.** Retries continue —
  spaced — until a deadline (default 90 s, comfortably under the 120 s
  read `timeout`), then the **authentic** upstream error (e.g. a 529
  body + headers) is passed through verbatim. The attempt count is only
  a safety cap. This is what keeps the *client* from giving up: the
  proxy holds the request and keeps trying in the background.
- **No whole-pool nuke.** Earlier versions drained the entire shared
  HTTP/2 pool on a sticky failure. That closed sibling sessions' live
  connections (turning one session's overload into a collective storm)
  *and* recreated the per-request fresh-connection profile the pool
  exists to avoid — which itself tripped the edge **429** gate (see
  [Forwarder: httpx + HTTP/2](#forwarder-httpx--http2)). The rewrite
  removes it: httpx already evicts a connection that *raised*, so the
  next attempt on the shared client lands on a fresh connection without
  disturbing other sessions.
- **Cross-session circuit breaker.** The proxy is one process with one
  shared pool; every session's request flows through it. When a burst
  of overloads (529 + connection drops) crosses a threshold, the
  breaker opens and *every* session adds a coordinated delay before its
  next first-attempt — so a second session can't pile on. This is the
  direct counter to "the throttle engages as soon as a 2nd session
  runs".
- **429 passes straight through.** A 429 is the account's own quota,
  not an overload; the proxy never retries it (Claude Code + its
  `Retry-After` handle it). It's counted for visibility only.

**Observability**

- `GET http://127.0.0.1:38080/health` returns a live snapshot:
  cumulative flap counters (`upstream_529_total`,
  `upstream_conn_error_total`, `upstream_429_passthrough_total`,
  `retry_succeeded_total`, `retry_exhausted_total`, `breaker_open_total`)
  and the current breaker state (`open`, `open_remaining_s`,
  `recent_overloads`, `last_retry_after_s`).
- JSONL request lines gain `retry_count` / `backoff_total_ms` /
  `retry_outcome` (null on the common no-retry path); the same three
  land in the `requests` table (stats schema v6).

**Env knobs** (all optional; safe defaults):

| Env var | Default | Controls |
|---|---|---|
| `CLAUDE_HOOKS_PROXY_RETRY_DEADLINE_S` | `90` | wall-clock retry window |
| `CLAUDE_HOOKS_PROXY_RETRY_BASE_DELAY_S` | `1.0` | backoff base |
| `CLAUDE_HOOKS_PROXY_RETRY_MAX_DELAY_S` | `20.0` | backoff cap |
| `CLAUDE_HOOKS_PROXY_RETRY_MAX_ATTEMPTS` | `8` | attempt safety cap (`1` = pass-through; legacy `CLAUDE_HOOKS_PROXY_RETRIES` honored as fallback) |
| `CLAUDE_HOOKS_PROXY_RETRY_AFTER_CAP_S` | `30.0` | max honored `Retry-After` |
| `CLAUDE_HOOKS_PROXY_RETRY_JITTER` | `true` | full-jitter on/off |
| `CLAUDE_HOOKS_PROXY_HONOR_RETRY_AFTER` | `true` | honor the header |
| `CLAUDE_HOOKS_PROXY_RETRY_STATUS` | `500,502,503,504,520-527,529` | retryable status set (CSV) |
| `CLAUDE_HOOKS_PROXY_BREAKER_WINDOW_S` | `30` | overload sliding window |
| `CLAUDE_HOOKS_PROXY_BREAKER_THRESHOLD` | `4` | overloads to open |
| `CLAUDE_HOOKS_PROXY_BREAKER_OPEN_S` | `10` | open duration |
| `CLAUDE_HOOKS_PROXY_BREAKER_EXTRA_DELAY_S` | `2.0` | coordinated pre-attempt delay while open |

Retired (ignored, with a one-time warning): `…_RETRY_BACKOFF`,
`…_RETRY_BACKOFF_MAX`, `…_SLOW_5XX_RESET_SEC`, `…_5XX_RESET_AFTER`,
`…_PROTO_RESET_AFTER` — their semantics (sub-second fixed backoff +
whole-pool nuke) were exactly what amplified the throttle.
