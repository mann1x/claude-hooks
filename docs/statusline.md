# Status line: usage limits and unread mail

`scripts/statusline_compose.py` is a complete `statusLine` command for
Claude Code:

```
claude-hooks | ctx: 25% used | Opus 5.5 | 5h 19% · 7d 90% 🔴 | 📬 2
```

It needs no proxy. Both live segments come from places that exist on
every host:

| segment | source |
|---|---|
| `5h 19% · 7d 90% 🔴` | the `rate_limits` block Claude Code passes the status line on stdin — the same numbers Anthropic's `anthropic-ratelimit-unified-*` headers carry, fresh on every refresh |
| `📬 2` | this session's unread mailbox count (`docs/mailbox.md`), shown only when there is some |

## Wiring

```json
"statusLine": {
  "type": "command",
  "command": "/path/to/python /path/to/claude-hooks/scripts/statusline_compose.py",
  "refreshInterval": 30
}
```

`refreshInterval` is what makes the mail badge useful: without it the
status line re-runs only on conversation events, so mail that arrives
while the session sits idle would wait for your next prompt. With it the
badge appears within about half a minute, and the status line costs no
tokens — asking the model "did I get mail?" costs a turn.

Options: `--format {emoji,ascii,plain}` (default emoji on Linux/macOS,
ascii on Windows consoles; `CLAUDE_HOOKS_STATUSLINE_FORMAT` overrides,
`CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI=1` keeps emoji on Windows Terminal),
`--no-mail`, `--mail-ttl SECONDS` (default 20).

`scripts/statusline_usage.py` prints just the usage segment, for an
existing status-line script: pipe it the same stdin.

## Usage segment

- `5h 42% · 7d 18%` — both windows; either can be absent
- `⚠` at ≥ 50 % and `🔴` at ≥ 80 % of the **binding** window, which is
  the fuller of the two (the payload does not carry Anthropic's
  `representative-claim`)
- `⏰` Anthropic shoulder hours (13–22 UTC), `🔥` weekday peak-of-peak
  (17–21 UTC); override with `CLAUDE_HOOKS_STATUSLINE_PEAK_HOURS_UTC` /
  `CLAUDE_HOOKS_STATUSLINE_PEAKPEAK_HOURS_UTC` as `HH-HH`
- empty when the payload has no `rate_limits` (API-key sessions, or
  before the first response)

Until v1.18 this segment read the proxy's `ratelimit-state.json` (or the
dashboard's `/api/ratelimit.json` from another host). That went blank
whenever a session's traffic did not pass through the proxy, and made
the status line depend on two services for data Claude Code already
had. The proxy still writes the state file for the dashboard,
`status.py`, `proxy_stats.py` and `weekly_token_usage.py`; the status
line no longer reads it, and `/api/ratelimit.json` is gone. The old
flags (`--state-file`, `--remote-url`, `--proxy-url`, `--show-blocked`,
…) are accepted and ignored, so an existing `statusLine` command keeps
working. The Warmup-blocked counter (`blk=N`) went with them — see the
dashboard for that.

## Mail segment

The count is the session's unread inbox — the same query the hooks'
announcements use: messages to its alias (on this host or unqualified)
or to its session id, not yet read. It is hidden when the mailbox is
disabled in config or a `.claude-hooks-disable` marker does not
`keep: mailbox`.

The status line re-runs on every assistant message, so the count is
cached per session for `--mail-ttl` seconds in
`~/.claude/claude-hooks-mailbox/statusline-<session>.json` (pruned after
a day). A failed lookup is cached too, so a store that is down costs one
attempt per interval. Reading the mail (`mailbox-read`) clears the badge
at the next refresh after the cache expires.
