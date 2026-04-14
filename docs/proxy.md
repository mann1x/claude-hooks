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
see the raw HTTPS traffic; the proxy can. P0 is **observability
only** — pure pass-through, one JSONL record per upstream request.

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

## Enable

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

2. Start the proxy (foreground — `Ctrl-C` to stop):

   ```bash
   # POSIX shim (prefers conda env's python)
   bin/claude-hooks-proxy

   # Or directly
   python3 -m claude_hooks.proxy
   ```

   On Windows: `bin\claude-hooks-proxy.cmd`.

3. Point Claude Code at it via `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:38080"
     }
   }
   ```

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
`docs/PLAN-proxy-hook.md` for per-phase checklists.

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

Defaults in `forwarder.py`:

- `max_keepalive_connections=10`, `max_connections=20`
- `keepalive_expiry=300.0` s (5 min idle before the pool drops a conn)
- `connect=10.0` s, `timeout=<proxy.timeout>` s for read/write
- `trust_env=False` — we ignore `HTTPS_PROXY` / `NO_PROXY` from the
  environment because the host may have those set pointing *at us*

Tighten only if upstream changes its keep-alive window.
