# Deployment playbook

A single-file walkthrough of the full claude-hooks stack from clone
to proxy-on-LAN. Source of truth for anything scattered between
[README.md](../README.md), [proxy.md](proxy.md), and
[env-vars.md](env-vars.md) — those are reference docs, this is the
"do these things in this order" playbook.

---

## 1. Pick your setup

Three ways to run claude-hooks:

| Mode | Memory backends | Proxy | Good for |
|------|-----------------|-------|---------|
| **Minimal** | Qdrant + Memory KG MCP, OR pgvector single-backend (replaces both) | off | single host, standard recall + storage |
| **Single-host proxy** | + local proxy on `127.0.0.1` | on | want Warmup blocked, live weekly-% in statusline |
| **LAN-shared proxy** | proxy on `<server>:38080` | on | multi-host — one proxy, many Claude Code clients |

**pgvector mode (recommended for new installs):** one Postgres
database replaces Qdrant + Memory KG. `install.py` brings up the
schema, drops a system-wide `pgvector-mcp` stdio server, and
registers it so other MCP-aware clients (Cursor, Codex, OpenWebUI)
can share the same memory + KG. See
[pgvector-runbook.md](pgvector-runbook.md) for the full setup.

The rest of this doc walks the LAN-shared setup (the most complex).
Scale down as needed.

## 2. Prereqs (server side)

- Linux with Python 3.9+ (stdlib only for the core — no `pip install`
  required until you want the dev deps)
- A Qdrant MCP server (e.g. `ghcr.io/sparfenyuk/mcp-proxy` wrapping
  `mcp-server-qdrant`) reachable on HTTP
- A Memory-KG MCP server reachable on HTTP
- Optionally: Ollama for HyDE / reflect / consolidate
- Optionally: `ccusage` (`npx -y ccusage@latest`) for USD
  cross-reference

## 3. Install on the server

```bash
git clone https://github.com/mann1x/claude-hooks.git
cd claude-hooks
python3 install.py
```

The installer:
1. Offers to create a conda env (`claude-hooks`) — useful for dev
   deps and pinned Python. System `python3` works fine too.
2. Scans `~/.claude.json` for Qdrant / Memory-KG MCP servers and
   confirms matches with you.
3. Writes `config/claude-hooks.json`.
4. Merges hook entries into `~/.claude/settings.json` (idempotent;
   entries are tagged `_managedBy: claude-hooks`).
5. **pgvector setup (optional)** — asks once whether to enable
   pgvector. On yes, prompts for the DSN, probes Postgres + the
   `vector` extension, offers to `ollama pull` the embedder model
   (`qwen3-embedding:0.6b` by default) if missing, initializes the
   qwen3 + KG schema (`memories_qwen3`, `kg_observations_qwen3`,
   shared `kg_entities` + `kg_relations`) when not present, drops a
   system-wide launcher at `~/.local/bin/pgvector-mcp` (POSIX) or
   `%LOCALAPPDATA%\claude-hooks\bin\pgvector-mcp.cmd` (Windows), and
   registers it in `~/.claude.json`'s `mcpServers` so any MCP-aware
   client — Claude Code, Cursor, Codex, OpenWebUI — can recall +
   store + query the KG via `mcp__pgvector__*` tools. See
   [pgvector-runbook.md §4 "MCP server"](pgvector-runbook.md).
6. Optionally prompts for env-var recommendations (`CLAUDE_CODE_
   DISABLE_BACKGROUND_TASKS`, the bcherny stack). **Default = No**
   for everything — the proxy is the better fix for Warmup drain,
   and the bcherny stack caused more harm than good in our field
   tests (see [env-vars.md](env-vars.md) for verdicts).

### Verify

Open a new Claude Code session. You should see:

> _Started with claude-hooks recall enabled (2 provider(s): Qdrant, Memory KG)._

Check `~/.claude/claude-hooks.log` — you should see the hooks firing
on every turn.

## 4. Enable the proxy

`install.py` orchestrates the proxy setup on Linux/macOS/Windows
(see [`docs/proxy.md`](proxy.md#install-via-installpy-recommended)
for the full prompt walkthrough). Two flows depending on the role
this host should play.

### Server-side (the host running the proxy)

If you want to share the proxy with other machines on the LAN, edit
`config/claude-hooks.json` first so `listen_host` is the LAN
interface (and any other tweaks you want), then run the installer:

```json
"proxy": {
  "enabled": true,
  "listen_host": "192.168.178.2",
  "listen_port": 38080,
  "upstream": "https://api.anthropic.com",
  "timeout": 120.0,
  "log_requests": true,
  "log_dir": "~/.claude/claude-hooks-proxy",
  "log_retention_days": 14,
  "record_rate_limit_headers": true,
  "block_warmup": true
}
```

Then `python3 install.py`:

- Pick **`y`** when asked "Use the API proxy?".
- Pick **`1`** when asked local-vs-remote (you ARE the local host).
- Optionally accept the `ANTHROPIC_BASE_URL=http://127.0.0.1:38080`
  prompt — `0.0.0.0` / LAN IP listen hosts translate to `127.0.0.1`
  on the client side automatically.

The installer drops the right per-OS service:

| OS | What gets installed |
|----|---------------------|
| Linux | `claude-hooks-proxy.service` + `rollup.service` + `rollup.timer` + `dashboard.service` in `/etc/systemd/system/`, `daemon-reload` + `enable --now` |
| macOS | `~/Library/LaunchAgents/com.claude-hooks.proxy.plist` (KeepAlive=true), `launchctl load -w` |
| Windows | UAC-elevated logon-triggered scheduled task `claude-hooks-proxy` (pythonw + `run_proxy.py` to avoid a permanent cmd window) |

Single-host setup: leave `listen_host: "127.0.0.1"`. LAN-shared
setup: use the LAN IP and make sure your firewall lets the port
through for the subnet you want to expose.

Verify (Linux):

```bash
systemctl is-active claude-hooks-proxy              # active
journalctl -u claude-hooks-proxy -n 5 --no-pager
curl -s -o /dev/null -w "%{http_code}\n" \
    http://192.168.178.2:38080/                     # 404 = proxy alive
```

Verify (macOS):
```bash
launchctl print gui/$(id -u)/com.claude-hooks.proxy | head
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:38080/
```

Verify (Windows):
```cmd
schtasks /Query /TN claude-hooks-proxy /V /FO LIST
curl http://127.0.0.1:38080/ -s -o NUL -w "%{http_code}\n"
```

## 5. Wire each client

For every other Claude Code host that should route through the
shared proxy, run `python3 install.py` on that host and pick
**choice `2`** ("use an existing proxy already on the network")
when asked. The installer prompts for the URL
(e.g. `http://192.168.178.2:38080`) and writes
`ANTHROPIC_BASE_URL` into `~/.claude/settings.json` for you. No
local proxy service is installed on these clients.

If you'd rather wire it by hand, edit `~/.claude/settings.json` and
add under `env`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://192.168.178.2:38080"
  }
}
```

Restart Claude Code. On the very next turn the proxy's JSONL log
gains a row, and (if rate-limit headers are present) the state
file at `~/.claude/claude-hooks-proxy/ratelimit-state.json` appears
on the **server**.

## 6. Optional: statusline segment

Append to your existing `statusLine` command script so the weekly %
shows inline. Minimal shell wiring:

```bash
REPO="${CLAUDE_HOOKS_REPO:-/srv/.../claude-hooks}"
usage_seg=$(python3 "$REPO/scripts/statusline_usage.py" 2>/dev/null)
[ -n "$usage_seg" ] && usage_part=" | $usage_seg"
```

The script auto-picks emoji on Linux/macOS and ASCII on Windows
(cmd.exe / legacy PowerShell render emoji as tofu boxes). Override
with `CLAUDE_HOOKS_STATUSLINE_FORMAT={emoji,ascii,plain}`. Windows
Terminal users with a Cascadia-Code-like font can keep emoji even
when the script is invoked with a hardcoded `--format emoji` by
exporting `CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI=1`.

`statusline_usage.py` exits 0 on every error path, so it's safe to
add to any statusline runner without guarding.

## 7. Optional: env-var tweaks

See [env-vars.md](env-vars.md) for the full curated list. Our
recommendation after months of field testing:

- ✅ Do set: nothing specific, until you hit a problem.
- ❌ Don't set: `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` +
  `MAX_THINKING_TOKENS` (bcherny stack) unless you've verified it
  helps *your* workflow. On ours it increased trivial mistakes.
- With the proxy's `block_warmup: true`, you can also drop
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` that you might have set
  for earlier Warmup mitigation — the proxy handles it now, and
  you get Ctrl+B + `run_in_background` back.

## 8. Monitoring

```bash
# One-screen dashboard (recommended default)
python3 scripts/status.py

# Short: is the proxy still healthy?
systemctl is-active claude-hooks-proxy

# Last 7 days of proxy traffic (per-day + per-model)
python3 scripts/proxy_stats.py --days 7 --by-model

# Custom-weekly view (Fri 10:00 CEST reset)
python3 scripts/weekly_token_usage.py --show-sidechain

# All of them — pipe into jq for dashboarding
python3 scripts/status.py --json | jq .
python3 scripts/proxy_stats.py --days 7 --json | jq .
python3 scripts/weekly_token_usage.py --json | jq .
```

## 9. Uninstall

```bash
# Hooks
python3 install.py --uninstall

# Proxy
sudo systemctl disable --now claude-hooks-proxy
sudo rm /etc/systemd/system/claude-hooks-proxy.service
sudo systemctl daemon-reload

# Client-side env var
# Remove ANTHROPIC_BASE_URL from ~/.claude/settings.json on every host
```

Log data survives uninstall. Wipe with
`rm -rf ~/.claude/claude-hooks-proxy/` if you want it gone.

---

## Troubleshooting quick-ref

| Symptom | First thing to check |
|--------|----------------------|
| Hooks aren't firing | `tail -20 ~/.claude/claude-hooks.log` — the dispatcher logs every event |
| Proxy won't start | `journalctl -u claude-hooks-proxy -n 30 --no-pager` — common cause: port in use |
| Proxy hangs on `systemctl stop` | Should be bounded to 10 s via `TimeoutStopSec`; if not, unit is stale — reinstall it |
| Statusline shows nothing | Proxy hasn't seen a request yet this window; make one Claude Code call and re-check |
| `weekly_token_usage.py` shows 0 for today | Transcripts from this session haven't been flushed; trigger a new turn and re-run |
| Warmup count unexpectedly high | `block_warmup: false` in config, or proxy not in the CC request path (check `ANTHROPIC_BASE_URL`) |
