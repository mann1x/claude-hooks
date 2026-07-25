# claude-hooks-daemon

Long-lived hook executor (Tier 3.8 latency reduction).

## What it does

Each hook invocation today spawns a fresh Python interpreter. Even on a
warm machine that's:

- ~50–200 ms interpreter startup
- ~20–50 ms `import claude_hooks.*`
- ~20 ms config + logging setup
- ~50 ms provider instantiation

For UserPromptSubmit and Stop — fired on every prompt — that's a
visible per-turn tax. The daemon owns all that state once and answers
each hook in milliseconds.

## How it works

`bin/claude-hook` first tries to send the event to the daemon over an
HMAC-authenticated TCP localhost socket. The daemon dispatches
through the same `claude_hooks.dispatcher.dispatch` the inline run
path uses, so behaviour is identical between the two modes. If the
daemon isn't running (not installed, crashed, paused for `systemctl
stop`, …), the client returns `None` and the shim falls back to
in-process dispatch silently. The daemon is **strictly optional** —
nothing breaks without it, you just lose the latency savings.

## Responsibilities

The daemon owns three things in a single process:

1. **Hook dispatch** (the original v1.0 responsibility). Routes
   inbound events through `claude_hooks.dispatcher.dispatch`.
2. **Llamafile lifecycle** (v1.4+). When
   `cfg["embedding"]["enabled"]` is true, the daemon instantiates
   a [`claude_hooks.embedding_manager.EmbeddingManager`](../claude_hooks/embedding_manager.py)
   that spawns the bundled llamafile binary on demand, idle-reaps
   it after 5 minutes (configurable), and exposes three new RPC
   ops to clients. See
   [`docs/llamafile-integration.md`](llamafile-integration.md)
   for the architecture; the wire-protocol entries are at the
   bottom of the next section.
3. **Graceful shutdown** of both — `request_shutdown()` reaps the
   llamafile (if alive), drains in-flight hooks, then exits.

The daemon does **not** own:
- Provider connections (qdrant, memory_kg, pgvector, sqlite_vec)
  — those are constructed per-dispatch and torn down at end of
  request.
- The `/consultants` engine — that runs as its own service (or
  the `consultants_forwarder` smart-start shim on port 38096).
- The API proxy (`claude-hooks-proxy`, port 38080) and stats
  dashboard (port 38081) — separate units.

## Wire protocol

One JSON line per request, one JSON line per response. The signed
input is `id|ts|event|sha256(payload_json)` so payloads can be large
without inflating the signature input.

```
REQUEST:  {"id":N, "ts":<epoch>, "event":"Stop",
           "payload":{...}, "sig":"<hex>"}
RESPONSE: {"id":N, "ok":true, "result":{...}}            on success
          {"id":N, "ok":false, "error":"...", "code":N}  on error
```

Replay-window (default 60 s) and HMAC bind to a per-host secret at
`~/.claude/claude-hooks-daemon-secret` (mode 0600, generated on first
start by `claude_hooks.daemon.ensure_secret`).

### Protocol-level ops (v1.4+)

Three RPC ops exist alongside the hook-event ops; they pass the
same HMAC envelope but the `event` field starts with `_` (the
dispatcher's underscore-prefix is how it tells "internal protocol
op" from "user-facing hook event"). All are best-effort:
daemon-down returns `None` to the client wrapper; manager-disabled
returns `{ok: true, result: {available: false}}`.

| `event` | Returns | Used by |
|---------|---------|---------|
| `_ping` | `{ok: true, result: {alive: true, version: ...}}` | `claude-hooks-daemon-ctl status` |
| `_shutdown` | `{ok: true, result: {stopping: true}}` | `claude-hooks-daemon-ctl kill` |
| `_embedding_ensure` | `{port: 38092, ready: true, mode: "gpu"\|"cpu", spawned: bool}` | `LlamafileEmbedder` on cold-start |
| `_embedding_status` | `{alive, pid, port, mode, idle_seconds, idle_timeout_seconds, gpu_offload_failed}` | dashboards / debug / smoke tests |
| `_embedding_shutdown` | `{stopped: true}` | `install.py --uninstall`, daemon graceful-stop |
| `_chat_model_ensure` (v1.5+) | `{label, port, ready, mode, spawned, evicted: [...]}` | `LlamafileChatClient` / `LlamafileAgentChatClient` cold-start |
| `_chat_model_status` (v1.5+) | `{models: [{label, alive, pid, port, idle_seconds, ...}]}` | `claude-hooks-models show / list-live`, ops dashboards |
| `_chat_model_shutdown` (v1.5+) | `{stopped: [...]}` (omit label = all) | `claude-hooks-models remove`, daemon graceful-stop |
| `_chat_model_gc` (v1.5+) | `{reaped: [...]}` | `claude-hooks-models gc` |

Typed wrappers in `claude_hooks/daemon_client.py`:
`embedding_ensure(timeout=60)`, `embedding_status(timeout=5)`,
`embedding_shutdown(timeout=15)`, plus v1.5's
`chat_model_ensure(label, timeout=120)`,
`chat_model_status(label=None, timeout=5)`,
`chat_model_shutdown(label=None, timeout=15)`,
`chat_model_gc(timeout=30)`. Each returns `None` on daemon
unreachable, `{available: false}` on `ok=false`, or the unwrapped
result dict on success.

### Chat-model lifecycle (v1.5+)

`ChatModelManager` is a multi-instance variant of v1.4's singleton
`EmbeddingManager`. State is a `dict[label, ProcessHandle]` with
threading-lock guarding mutation; LRU eviction kicks in when
`len(handles) >= max_concurrent_loaded` (configurable per-host).
The reaper thread polls every 60 s and per-label reaps when
`now - last_activity_at >= spec.idle_timeout_seconds` (default
600 s; streaming chat calls update `last_activity_at` per chunk
so long generations can't be reaped mid-call). Registry mtime is
watched on every ensure so `claude-hooks-models add` followed by
a recall picks up the new entry without restart. Per-label
GPU-spawn failure flips that label to CPU mode for the daemon's
lifetime (mirrors the v1.4 manager-global behaviour, scoped down
to one label).

## Install / autostart

`install.py` prompts to register an autostart entry for your platform:

| Platform | Mechanism                                            |
|----------|------------------------------------------------------|
| Linux    | `systemd/claude-hooks-daemon.service` → `/etc/systemd/system/`, `systemctl enable --now` |
| macOS    | `~/Library/LaunchAgents/com.claude-hooks.daemon.plist`, `launchctl load -w` |
| Windows  | `schtasks /Create /SC ONLOGON /TN claude-hooks-daemon …` via PowerShell `Start-Process -Verb RunAs -Wait` (one UAC prompt). Falls back to printing the manual command if UAC is declined or PowerShell isn't available. |

You can opt out at install time. The runtime behaviour is unchanged
either way — daemon-or-fallback.

## Manual control

The repo ships a unified `bin/claude-hooks-daemon-ctl` (`.cmd` on
Windows) so you don't have to remember each platform's tooling:

```bash
claude-hooks-daemon-ctl status     # alive? autostart entry? hint if neither
claude-hooks-daemon-ctl start      # idempotent (no-op if already up)
claude-hooks-daemon-ctl stop       # graceful HMAC _shutdown, falls back to platform End
claude-hooks-daemon-ctl restart    # stop + bounded ping wait + start
claude-hooks-daemon-ctl tail -n 80 # last 80 log lines (Windows: %USERPROFILE%\.claude\claude-hooks-daemon.log) or the journalctl/log command (Linux/macOS)
```

Exit codes: `status` returns `0` when responding, `1` when down but
the autostart entry exists, `2` when the daemon isn't installed at
all. Other verbs return `0` on apparent success and non-zero on any
inability to do the thing — useful for monitoring scripts.

Stop is graceful by default — the daemon receives an HMAC-signed
`_shutdown` over its own socket, replies, then schedules its own
shutdown so the response makes it back. If the graceful path doesn't
take within 5 seconds, the wrapper falls through to `schtasks /End`,
`systemctl --user stop`, or `launchctl kill SIGTERM`.

Direct platform tooling still works if you prefer it:

```bash
# Foreground (blocks — for debugging)
bin/claude-hooks-daemon

# Linux
systemctl status claude-hooks-daemon
systemctl restart claude-hooks-daemon

# macOS
launchctl list | grep claude-hooks
launchctl unload ~/Library/LaunchAgents/com.claude-hooks.daemon.plist

# Windows
schtasks /Run /TN "claude-hooks-daemon"
schtasks /End /TN "claude-hooks-daemon"
```

## Disable per-invocation

Set `CLAUDE_HOOKS_DAEMON_DISABLE=1` in the shim's environment to skip
the daemon and force inline dispatch. Useful for debugging — nothing
about the daemon path should affect inline behaviour, but bypassing it
isolates issues quickly.

## Security

- Binds to `127.0.0.1` only. The server constructor refuses any
  non-loopback host, so a `host: "0.0.0.0"` config typo can't expose
  the daemon to the network.
- HMAC-SHA256 over `id|ts|event|sha256(payload_json)`.
- Replay window (default 60 s) bounds the forgery surface on a leaked
  secret to a 60 s window per signed message.
- Secret file refused at startup if mode bits include `0o077`
  (POSIX). Windows ACLs aren't checked — keep your home directory
  permissions sensible.

## Logging

- **Linux**: stdout/stderr captured by systemd → `journalctl -u
  claude-hooks-daemon`.
- **macOS**: launchd writes to the unified log; `log stream` to follow.
- **Windows**: `pythonw.exe` has no console and Task Scheduler doesn't
  capture stderr, so `run_daemon.py` redirects both streams into
  `%USERPROFILE%\.claude\claude-hooks-daemon.log` (line-buffered append).
  The launcher rotates the file to `claude-hooks-daemon.log.1` on
  startup if it has crossed 5 MiB — keeps one prior generation,
  no scheduled rotation, no log spam.

The daemon's logger is `claude_hooks.daemon` at INFO level; raise to
DEBUG by editing `daemon.py:367` if you need per-connection signing
trace.

## Latency tiers and `detach_store`

The daemon (Tier 3.8) is the biggest single latency lever, but it's
not the only one. Two orthogonal mechanisms shave time off the Stop
hook even when the daemon is off:

| Tier | Mechanism | Savings | Default |
|---|---|---|---|
| 1.2 | HyDE expansion cache (`hyde_cache.py`) | 0.5–4 s on `UserPromptSubmit` cache hits | on (when HyDE is on) |
| 1.3 | Detached store (`store_async.py`) | 200–500 ms per noteworthy turn on `Stop` | **on** |
| 3.8 | Long-lived daemon (`daemon.py`) | 100–300 ms per hook invocation | opt-in via installer |

Tier 1.3 — detached store — is the focus of this section. It is
controlled by `hooks.stop.detach_store` in
`config/claude-hooks.json`, and has been **on by default since
2026-07-25**.

> **Why it stopped being opt-in.** With a *local* embedder the inline
> path is not merely slower, it is incorrect. `store()` issues two
> embeds (dedup recall, then content) and embedding latency is
> superlinear in payload size — measured CPU-only on
> qwen3-embedding-0.6b: 5 KB ~9 s, 16 KB ~48 s, 30 KB ~135 s. A 5 KB
> memory therefore needs ~19 s of embedding, which exceeded the 20 s
> `Stop` cap older installs wrote into `settings.json`. Claude Code
> SIGTERMed the hook mid-store: the memory was silently lost, and the
> session reported the embedder as down. Raising the cap alone does not
> fix it — at `max_chars: 30000` the two embeds can cost ~270 s, more
> than any sane hook budget. Detaching removes the bound entirely.
>
> The trade-off is real and unchanged: a detached store cannot report
> failures in the Stop `systemMessage`, because the parent has already
> returned. Failures go to `~/.claude/claude-hooks.log` instead. Set
> `detach_store: false` if you would rather see store errors inline and
> your memories are small enough to fit the hook budget.

### Why the Stop hook is slow

When the assistant turn is "noteworthy" (it called `Bash`, `Edit`,
`Write`, …), Stop summarises the turn and writes it to every provider
whose `store_mode` is `auto`. The cost per provider:

- `provider.recall(summary[:500], k=3)` for dedup (skip if a
  near-duplicate already exists) — one MCP `tools/call` round-trip.
- `provider.store(summary, metadata)` — another round-trip.

Both are network calls to the MCP server. Two providers ⇒ four
calls. Even on a healthy localhost setup that's ~200–500 ms, and
Claude Code is blocked on it.

### What `detach_store` does

When `hooks.stop.detach_store: true`, Stop forks a detached
`python -m claude_hooks.store_async` subprocess
(`subprocess.Popen` with `start_new_session=True`, stdio piped to
`DEVNULL`), pipes the payload (config + summary + metadata + provider
allow-list) to its stdin, and returns immediately. Claude Code
unblocks ~200–500 ms sooner. The child runs the same dedup-and-store
fan-out the inline path would have run, logging to the same
`~/.claude/claude-hooks.log` file.

Failures in the child are logged but **never surfaced** to Claude
Code — by the time the child finishes, the parent has already
returned. That's the price of the detach: errors are quieter.

### When to enable it

Turn it on when:

- Stop latency is visible (multi-provider config, slow MCP server,
  remote provider).
- You don't need the systemMessage to reflect the actual store
  result — the message says `[claude-hooks] storing to <providers>
  (async)` and is correct as long as the spawn succeeded.

Leave it off when:

- You're running tests that pass `FakeProvider` instances directly
  to `stop.handle()` — they can't survive an interpreter boundary,
  so the child rebuilds providers from config and the test's fakes
  vanish. Tests asserting on store side-effects must run inline.
- You want a hard guarantee the store completed before the next
  turn (e.g. interactive debugging where you'd recall what you just
  stored). Race condition: the next `UserPromptSubmit` could race
  the in-flight detached store.

### Interaction with the daemon

Both can be on at once and they compose:

- The daemon owns the hook-dispatch path → `Stop` runs in the
  long-lived daemon process, no interpreter startup tax.
- Inside the daemon's Stop handler, `detach_store: true` still forks
  a fresh `python -m claude_hooks.store_async` for the actual store.
  The daemon can return its response to the client immediately and
  the slow MCP work happens in the child.

In practice once the daemon is in place the savings from Tier 1.3
shrink (the daemon already eliminated interpreter startup), but
they're still real because the daemon's Stop handler would otherwise
sit in the network-call serial path. Net stack: the daemon answers
the Stop hook in milliseconds, the spawned child does the actual
store independently.

### Failure modes

- Subprocess spawn fails (rare — out of FDs, broken interpreter,
  stdin closed before write) → `store_async.spawn()` returns
  `False`, Stop logs at DEBUG and falls back to **inline** dedup +
  store. The user sees the inline systemMessage.
- Child dies after spawn — payload is half-stored, no signal back
  to the parent. The next turn will re-run dedup against whatever
  did land, so duplicates are still avoided.
- Payload not JSON-serialisable (custom objects in metadata) →
  spawn returns `False` early, falls back to inline.

