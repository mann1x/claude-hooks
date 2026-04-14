# Plan: claude-hooks proxy mode (optional, opt-in)

**Status:** draft, not started.
**Motivation:** observability + selective mitigation of behaviours
the Claude Code hook surface can't reach.

## Context

Claude Code exposes ~8 hook events (`UserPromptSubmit`, `Stop`,
`SessionStart`, `SessionEnd`, `PreToolUse`, `PostToolUse`,
`SubagentStop`, `PreCompact`, `Notification`). These fire **around**
the model call but cannot see or modify the raw HTTPS request or
response. Several things we care about are invisible at that layer:

1. **Subagent Warmup** traffic — fires at session boot, BEFORE any
   `UserPromptSubmit` hook, and is only observable after the fact
   via transcripts. A proxy sees the actual `POST /v1/messages` at
   fire time and can deny / rewrite / log it.
2. **Per-request token usage** — the user's weekly-limit percentage
   is server-side only (no local file, no CLI query). A proxy can
   record rate-limit headers Anthropic returns
   (`anthropic-ratelimit-requests-remaining`,
   `anthropic-ratelimit-tokens-remaining`, etc.) and surface them.
3. **Thinking redaction** — the `redact-thinking-2026-02-12` beta
   rewrites assistant thinking blocks on the way back. A proxy can
   log the redaction volume and give the user a concrete picture
   of lost reasoning that hooks can't see.
4. **Silent retries and compacts** — Claude Code sometimes re-sends
   requests on 5xx or compact-triggered re-plans. Transcripts
   collapse these into one entry; a proxy catches every attempt.
5. **Model substitution** — the server sometimes downgrades `opus`
   to `haiku` under pressure. The transcript records the delivered
   model but a proxy can detect the substitution in real time.

Inspiration: [@ArkNill in
#42796](https://github.com/anthropics/claude-code/issues/42796)
mentioned running a transparent proxy in front of the Anthropic API
to measure per-request behaviour.

## Design constraints

1. **Default OFF.** Never auto-intercept by default. User must
   explicitly opt in via `config/claude-hooks.json`.
2. **Local-only by default.** Listen on `127.0.0.1:<port>` — no
   external exposure.
3. **Pass-through semantics.** The proxy must not alter request
   content unless explicitly configured. Default mode is pure
   observability.
4. **Reversible.** A single env-var flip or config toggle must
   reliably revert to direct API access with zero state carryover.
5. **Stdlib only for the core.** We accept that TLS interception
   requires a CA cert — that's fine. The HTTP + TLS mechanics need
   stdlib + one well-known CA wrapper (see "TLS options" below).
6. **Cross-platform.** Linux + Windows. The wiring trick is
   `ANTHROPIC_BASE_URL` + trust-store manipulation — both OSes
   support this.

## TLS options (the hard part)

Claude Code talks HTTPS to `api.anthropic.com`. To see the raw
body we must terminate TLS. Two candidates:

### A) `ANTHROPIC_BASE_URL` redirect to a local HTTP proxy

Claude Code respects `ANTHROPIC_BASE_URL`. Point it at
`http://127.0.0.1:<port>` and run a plain-HTTP proxy there. The
proxy re-terminates to `https://api.anthropic.com` for the
upstream call.

- **Pro:** no cert trust issues, no system-wide TLS interception.
- **Pro:** documented env var; no patches required.
- **Con:** traffic between CC and the proxy is unencrypted
  (local-only, so acceptable).
- **Con:** might not work for auth flows that pin to
  `api.anthropic.com` by name (must test).

### B) MITM with a locally trusted CA (mitmproxy-style)

Ship a mini CA, install it into the OS trust store, forward
`api.anthropic.com` through the proxy.

- **Pro:** fully transparent — no CC-side config change.
- **Con:** modifying the system trust store is invasive, hard to
  do cleanly on Windows, and a security smell for a "hooks"
  project.
- **Verdict:** Not pursuing in v1. Option A is enough.

## Hook surface

The proxy emits **synthetic hook events** that the dispatcher can
fan out to providers or handlers, mirroring the existing hook
shape:

| Event                  | When                                                                                  | Body                                                         |
|------------------------|----------------------------------------------------------------------------------------|--------------------------------------------------------------|
| `ProxyRequest`         | Before forwarding upstream                                                             | `{method, url, headers_sanitised, body, is_warmup}`          |
| `ProxyResponse`        | After upstream responds, before returning to CC                                        | `{status, rate_limit_headers, usage_block, model_delivered}` |
| `ProxyRetry`           | When CC issues a second attempt for the same logical turn                              | `{attempt, previous_status, reason}`                         |
| `ProxyModelSubst`      | When the response model ≠ the requested model                                          | `{requested, delivered, reason_hint}`                        |
| `ProxyWarmupBlocked`   | Only if user enabled block-warmup — fires when the proxy short-circuits a Warmup call  | `{agent_id, session_id}`                                     |

These go through the same `dispatcher.build_providers()` path, so
handlers can `recall()` / `store()` just like from the user
hooks. Bonus: makes the proxy usable as a *recall trigger* on
rate-limit-header changes.

## Config schema additions

`config/claude-hooks.json` gains:

```json
{
  "proxy": {
    "enabled": false,
    "listen_host": "127.0.0.1",
    "listen_port": 38080,
    "upstream": "https://api.anthropic.com",
    "timeout": 120.0,
    "log_requests": true,
    "log_dir": "~/.claude/claude-hooks-proxy",
    "log_retention_days": 14,
    "block_warmup": false,
    "record_rate_limit_headers": true,
    "emit_events": [
      "ProxyResponse",
      "ProxyModelSubst",
      "ProxyWarmupBlocked"
    ]
  }
}
```

And the user's shell / settings.json needs:

```json
{
  "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:38080" }
}
```

— which `install.py` can offer to wire up (opt-in).

## Phased delivery

### Phase P0 — scaffold (est. 1 d)
- `claude_hooks/proxy/` package with a stdlib `http.server.ThreadingHTTPServer`
  forwarder.
- Pass-through only, no rewriting. Logs request metadata to JSONL
  under `log_dir`.
- Systemd unit / Windows service installer (opt-in).

### Phase P1 — observability (est. 2 d)
- Parse response streams (SSE) to extract model + usage blocks
  without buffering the whole body.
- Record rate-limit headers into a rolling file that the
  `weekly_token_usage.py` script can read — finally gives us the
  "weekly %" number that is currently UI-only.
- Emit `ProxyResponse` + `ProxyModelSubst` events to the
  dispatcher.

### Phase P2 — selective intervention (est. 2 d)
- `block_warmup: true` short-circuits requests whose body content
  matches `"Warmup"` — returns a minimal stub response so CC
  doesn't error.
- `ProxyWarmupBlocked` event.
- Tests using a fake upstream HTTP server.

### Phase P3 — integrations (est. 1 d)
- `scripts/weekly_token_usage.py` reads the proxy's rate-limit
  log and augments the table with the **real** weekly %.
- `statusline-command.sh` gains a `proxy` mode that shows current
  weekly % from the same log.

### Phase P4 — docs & install UX (est. 0.5 d)
- `docs/proxy.md` — setup, troubleshooting, security stance.
- `install.py` prompt to wire `ANTHROPIC_BASE_URL` (opt-in).
- README section.

Total: **≈ 6.5 days** of focused work.

## Risks

1. **Claude Code may not honour `ANTHROPIC_BASE_URL` everywhere.**
   Smoke-test first before building out.
2. **Stream framing (SSE) parsing must be robust** — mis-parse and
   CC will see truncated completions. Gate the observability
   feature behind a flag and fall back to pure pass-through on
   parse failure.
3. **Rate-limit headers may be deprecated or throttled** at the
   Anthropic side; the spec is undocumented for subscription
   users. If absent, Phase P1 value drops but P0/P2 still work.
4. **Concurrency / connection reuse.** HTTP/2 and keep-alive are
   likely — a naive forwarder will serialise. Use
   `urllib3.PoolManager` (already a transitive dep of
   `requests`-style libs) or wrap stdlib `http.client`.
5. **Security.** Running a local proxy that terminates API
   traffic means the proxy process sees every prompt in clear.
   Log rotation + restrictive file permissions matter. Document
   the threat model explicitly in `docs/proxy.md`.

## Non-goals for v1

- System-trust MITM (see TLS option B).
- Request modification / prompt-injection rewriting. Observability
  and selective blocking of Warmup only.
- Multi-user / network-exposed proxy. Local dev host only.

## Decisions to confirm with user before starting

1. `ANTHROPIC_BASE_URL` redirect vs MITM — any preference beyond
   "redirect is simpler"?
2. Default port — `38080` OK or clash with something on pandorum?
3. Should Phase P1 rate-limit logging replace
   `--current-usage-pct` in `weekly_token_usage.py` (auto-populate
   from proxy log) or just augment it?
4. Block-warmup as part of P2, or move to a separate phase so the
   observability parts land first?
