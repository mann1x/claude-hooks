# claude-hooks proxy (Phases P0 + P1) — setup

> **⚠ KNOWN ISSUE — keep the proxy disabled until fixed (2026-04-14).**
>
> The forwarder (`claude_hooks/proxy/forwarder.py`) opens a **fresh
> HTTP/1.1 connection** to `api.anthropic.com` for every request, with
> no keep-alive and no HTTP/2 multiplexing. Native Claude Code uses a
> single persistent **HTTP/2** connection that streams all requests
> over it.
>
> Anthropic's edge appears to rate-limit the per-request-connection
> pattern with **real 429s** even when the 5h / 7d budgets show
> `"allowed"`. Observed live on 2026-04-14: 4 × 429 in 58 s on
> single non-overlapping ~2 MB requests, all clearing immediately
> after the proxy was bypassed. The proxy itself doesn't generate
> the 429s — it relays them — but the *connection profile it
> presents to Anthropic* is what trips the gate.
>
> **Until this is fixed** (connection pooling / HTTP/2 in the
> forwarder), the proxy is shipped with `proxy.enabled: false`. Do
> not flip it back on, and remove `ANTHROPIC_BASE_URL` from
> `~/.claude/settings.json` if you previously set it.
>
> Tracking: see the "Connection-pattern fix" section at the bottom
> of this doc for the planned remediation.

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

## Connection-pattern fix (blocker for re-enabling)

The warning at the top of this doc explains why the proxy is
currently shipped with `proxy.enabled: false`. The forwarder needs
to stop opening one HTTPS connection per request before we can
recommend it again.

### Diagnosis (2026-04-14)

Live traffic on solidPC, 16:20–16:22 UTC:

| timestamp | status | dur | size | concurrent |
|---|---|---:|---:|---:|
| 16:20:55 | 200 | 23 s (stream) | 1856 KB | — |
| 16:20:59 | **429** | 1.3 s | 1858 KB | 1 |
| 16:21:00 | 200 | 2.0 s | 1860 KB | — |
| 16:21:01 | **429** | 1.2 s | 1860 KB | **0** |
| 16:21:04 | 200 | 1.4 s | 2011 KB | — |
| 16:21:05 | **429** | 1.6 s | 2011 KB | **0** |
| 16:21:57 | **429** | 1.6 s | 1950 KB | **0** |

429s with `concurrent=0` (no overlap) and on requests *smaller* than
ones that succeeded → not request-rate, not request-size, not
overlap. The differentiator is **connection profile**.

`anthropic-ratelimit-unified-status` = `"allowed"` on every adjacent
200, so it's not the 5h / 7d budget either. The 429s carry no
unified-rate-limit headers at all — they come from a separate edge
gate.

### Root cause

`claude_hooks/proxy/forwarder.py` does, per request:

```python
conn = http.client.HTTPSConnection(host, port, ...)
conn.request(method, path, body=body, headers=...)
resp = conn.getresponse()
# ... stream body ...
conn.close()  # in _drain()'s finally
```

That's a fresh TCP handshake + TLS 1.3 handshake **per request**
to `api.anthropic.com`. Native Claude Code (Node `undici`) opens
**one HTTP/2 connection** and multiplexes all `/v1/messages`
streams over it — Anthropic sees one well-behaved client.

Our pattern (HTTP/1.1, no keep-alive, no pooling) trips an
edge-level connection-establishment-rate gate that's distinct
from the published unified-rate-limit budgets.

### Fix options (pick later)

Listed cheapest → biggest:

1. **Stdlib keep-alive + per-thread pool.** Hold an
   `HTTPSConnection` instance per worker thread; reuse it for
   subsequent requests on the same thread. Still HTTP/1.1, but
   one TCP+TLS per worker instead of per request. Estimated risk:
   medium — `http.client` is finicky about connection state and
   doesn't recover well from upstream-side closes.
2. **Optional `urllib3` dep, scoped to the proxy module.**
   `urllib3.PoolManager` gives connection pooling + retry
   handling for free. Still HTTP/1.1. Breaks the stdlib-only
   promise but only inside `claude_hooks/proxy/`.
3. **`httpx[http2]` (with `h2`).** Full HTTP/2 — matches what
   native CC does. Heaviest dep, most disruptive change, almost
   certainly fixes the 429 issue.

Option 2 is the likely landing spot. Option 1 is worth trying
first only if we want to defend the stdlib-only constraint.

### Until then

- Keep `proxy.enabled: false`.
- Don't set `ANTHROPIC_BASE_URL=http://...:38080` in
  `~/.claude/settings.json` on any host.
- The proxy code stays in the tree — it's not abandoned, just
  parked behind the connection-pattern bug.
