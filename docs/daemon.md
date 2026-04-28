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
