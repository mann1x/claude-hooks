# Episodic memory server

`episodic_server/` is a small HTTP front-end for [obra/episodic-memory](https://github.com/obra/episodic-memory)
— Jesse Vincent's tool that indexes Claude Code transcripts and lets
you semantic-search past conversations. The server lets one host run
the indexer and many clients push transcripts to it on `SessionEnd`,
so search across all your machines lands in the same archive.

```
┌────────────┐  POST /ingest      ┌──────────────────┐
│ client A   │  X-Project: ...    │ episodic-server  │
│ (laptop)   │  X-Source-Host:    │  ↓               │
└────────────┘  X-Session-Id:     │  archive/        │
                ───────────────►  │   <host>-<proj>/ │
┌────────────┐                    │     <sid>.jsonl  │
│ client B   │  POST /ingest      │  ↓               │
│ (desktop)  │  ───────────────►  │  episodic-memory │
└────────────┘                    │  sync (re-index) │
                                  └──────────────────┘
```

claude-hooks ships:

- `episodic_server/server.py` — stdlib HTTP server (no deps)
- `episodic_server/Dockerfile` + `docker-compose.yaml` — container build
- `episodic_server/episodic-server.service` — systemd template
- `claude_hooks/hooks/session_end.py` — client-side push on session end
- `install.py --episodic-server` / `--episodic-client URL` — installer flags

## When you want this

- You bounce between hosts (laptop / desktop / pod) and want one
  search across *all* of them.
- You want a transcript archive that survives even when individual
  hosts get wiped.
- You want `episodic-memory search "..."` to surface a turn from a
  different machine without manual rsync.

If you only ever use one host, just install `episodic-memory` directly
and skip this server — the local CLI does the same indexing, and
`session_end` set to `mode: server` will trigger sync on every session
end automatically.

## Install — server mode

```bash
# 1. Install episodic-memory (the indexer)
git clone https://github.com/obra/episodic-memory
cd episodic-memory && npm install && npm link

# 2. Run the installer with --episodic-server
cd /path/to/claude-hooks
python3 install.py --episodic-server
```

The installer:
- Confirms `episodic-memory` is on PATH
- Sets `episodic.mode = server` in `config/claude-hooks.json`
- Renders `episodic_server/episodic-server.service` (substituting
  `__REPO_PATH__`, `__HOST__`, `__PORT__`) into
  `/etc/systemd/system/episodic-server.service`
- Reloads systemd, enables, and starts the unit

Verify:

```bash
systemctl status episodic-server
curl -s 'http://localhost:11435/health?fresh=1' | jq
# {
#   "archive": "/root/.config/superpowers/conversation-archive",
#   "archive_exists": true,
#   "index_db": "/root/.config/superpowers/conversation-index/db.sqlite",
#   "index_age_hours": 0.4,
#   "cli_ok": true,
#   "checked_at": 1790000000.0,
#   "status": "ok"
# }
```

`scripts/verify_deploy.py` reads the same endpoint on every deploy: a
dead CLI fails the deploy on the server host and warns on a client.

### Docker alternative

If you'd rather containerize it:

```bash
cd episodic_server
docker compose up -d
```

The compose file mounts `~/.config/superpowers/conversation-archive`
as `/archive` and `~/.claude/projects` read-only as
`/claude-projects`. Uses host networking so port `11435` is reachable
from the LAN.

## Install — client mode

On each Claude Code host that should push transcripts:

```bash
python3 install.py --episodic-client http://<server-host>:11435
```

This sets:

```jsonc
"episodic": {
  "mode": "client",
  "server_url": "http://192.168.178.2:11435",
  "timeout": 10.0
}
```

After install, `SessionEnd` reads the session transcript, posts it to
`/ingest`, and the server triggers a background `episodic-memory
sync`. No further client-side work — `episodic-memory` doesn't need
to be installed on clients.

## API

All endpoints return JSON.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Runs `episodic-memory stats` (cached 5 min; `?fresh=1` forces it). 200 `ok`, or **503 `degraded`** with the CLI's stderr and a `hint`. Also reports `index_age_hours` |
| `GET` | `/stats` | Runs `episodic-memory stats` and returns stdout |
| `GET` | `/search?q=<query>&limit=<N>` | Search across all indexed conversations. `limit` defaults to 10 |
| `POST` | `/ingest` | Save a transcript JSONL and trigger re-index |
| `POST` | `/sync` | Force a synchronous `episodic-memory sync` (timeout 120 s) |

When the CLI exits non-zero, `/stats`, `/search` and `/sync` answer
**502** with `returncode`, the tail of `stderr`, and a `hint` for known
causes. Until 2026-09-26 they answered 200 with an empty `stdout` and
dropped stderr, and `/health` only checked that the archive directory
existed, so a CLI that threw on every call looked healthy for 12 days.
The first `/search` in a process loads the embedding model (~35 s on
solidpc); give any probe of it at least 60 s.

### `POST /ingest`

Request body: raw NDJSON transcript (the file Claude Code keeps at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`).

Required headers:

| Header | Purpose |
|---|---|
| `Content-Length` | Required — empty body returns 400 |
| `X-Project` | Project name / cwd. Sanitized to `[A-Za-z0-9_.-]+` |
| `X-Session-Id` | Session ID. Defaults to `remote-<unix-ts>` if missing |
| `X-Source-Host` | Hostname of the pushing client. Used as a directory prefix to avoid collisions when two hosts have the same project name |

The server writes the transcript to:

```
<archive>/<X-Source-Host>-<X-Project>/<X-Session-Id>.jsonl
```

…then spawns `episodic-memory sync --background` to re-index. Returns
the saved path, byte count, and sanitized project name.

### `GET /search`

Wraps `episodic-memory search <query>`, parses the text output into
structured results, returns the top `limit`. Each result has `raw`
(original line), `quote` (the matched snippet), `match_pct` (when
present), and `location` (file/line info).

## Environment knobs

Read by `server.py`:

| Var | Default | Purpose |
|---|---|---|
| `EPISODIC_ARCHIVE` | `~/.config/superpowers/conversation-archive` | Where transcripts are written and `episodic-memory` reads from |
| `EPISODIC_INDEX_DB` | `<archive>/../conversation-index/db.sqlite` | The index whose age `/health` reports |
| `EPISODIC_BIN` | `episodic-memory` | Path/name of the indexer binary. Override if not on PATH |

CLI args:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `0.0.0.0` | Bind interface |
| `--port` | `11435` | Listen port |

## Client config (`config/claude-hooks.json`)

```jsonc
"episodic": {
  "mode": "client",                              // off | server | client
  "server_url": "http://192.168.178.2:11435",    // client only
  "server_host": "0.0.0.0",                      // server only
  "server_port": 11435,                          // server only
  "binary": "episodic-memory",                   // server only
  "timeout": 10.0                                // client push timeout
}
```

`mode: off` (the default) → SessionEnd does nothing for episodic.
`mode: server` → SessionEnd triggers `episodic-memory sync
--background` locally, no HTTP push.
`mode: client` → SessionEnd reads the transcript and POSTs it to
`server_url/ingest`.

## SessionEnd push wiring

When `mode: client`, the `session_end` hook
([`claude_hooks/hooks/session_end.py`](../claude_hooks/hooks/session_end.py)):

1. Reads `event.transcript_path` from the SessionEnd payload.
2. Skips push if the transcript is < 100 bytes (no real content).
3. Sends the file body as `application/x-ndjson` to `/ingest` with
   `X-Project: <event.cwd>`, `X-Session-Id: <event.session_id>`,
   `X-Source-Host: <socket.gethostname()>`.
4. On any URL error / timeout / OS error → logs a warning, returns
   nothing. The hook always exits 0; failed pushes never block
   session shutdown.

This is fire-and-forget — the client doesn't wait for indexing to
finish. The server runs sync in the background; `GET /search` reflects
new transcripts within seconds on small archives.

## systemd unit notes

`episodic-server.service` ships with hardening:

- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `ReadWritePaths=/root/.config/superpowers /root/.claude /var/log`
- `PrivateTmp=true`
- `ExecStartPre` checks `which episodic-memory` — won't start if the
  binary is missing
- `Restart=on-failure` with `RestartSec=30`
- `StartLimitBurst=5` over 300 s — won't loop forever on
  permanent failures (e.g. archive path inaccessible)

`install.py` writes `ReadWritePaths` for the installing user's home, and
adds the **resolved** target of `~/.config/superpowers` and `~/.claude`
when either is a symlink. `ProtectSystem=strict` builds the mount
namespace from the literal paths, so granting only the symlink leaves a
relocated archive read-only. On a host whose unit is already installed,
`install.py` checks the unit plus its drop-ins and prints the drop-in to
add when a path is missing:

```ini
# /etc/systemd/system/episodic-server.service.d/override.conf
[Service]
ReadWritePaths=/srv/<spool>/superpowers
```

## Native modules after a Node upgrade

better-sqlite3 binds V8 directly, not N-API, so its build works with
exactly one Node ABI. After a Node major upgrade every CLI call fails
with `NODE_MODULE_VERSION … ERR_DLOPEN_FAILED`, and `/health` answers
503 with a hint. The fix:

```bash
scripts/episodic_doctor.py            # which host-built modules load
scripts/episodic_doctor.py --rebuild  # rebuild the ones that don't
```

Why not just `npm rebuild`: Node ≥ 26's headers need C++20
(`<source_location>`, GCC ≥ 11), and Debian 11 has GCC 10 with nothing
newer in apt. The doctor picks `$CXX`, then the system `g++`, then a
conda-forge `*-conda-linux-gnu-g++` from any conda env, and always links
with `-static-libstdc++ -static-libgcc`. Without those two flags a
module built by conda's GCC links a libstdc++ newer than the system's:
it builds cleanly and then fails to load. There may be no prebuilt
binary for a new ABI (better-sqlite3 12.8.0 has none for Node 26), so
waiting for one is not a fix either. After a rebuild the index may be
stale: `episodic-memory sync`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `which episodic-memory` exits non-zero | Indexer not installed on the server host | `git clone https://github.com/obra/episodic-memory && cd episodic-memory && npm install && npm link` |
| Client pushes succeed but search returns nothing | Sync hasn't run yet, or sync failed silently | `curl -X POST http://<server>:11435/sync` then check `/stats` |
| Two hosts overwriting each other's transcripts | `X-Source-Host` not being set (older client) | Update claude-hooks on the client; check `socket.gethostname()` returns a unique name |
| `transcript too small (X bytes), skipping` in client log | Session genuinely had no content (< 100 bytes) — by design | No fix needed; the threshold filters empty / aborted sessions |
| Archive growing unbounded | episodic-memory has no built-in pruning | Manual: `find <archive> -name '*.jsonl' -mtime +180 -delete && episodic-memory sync` |

## Disable

Server side:
```bash
sudo systemctl disable --now episodic-server
sudo rm /etc/systemd/system/episodic-server.service
sudo systemctl daemon-reload
```

Client side: set `episodic.mode: "off"` in
`config/claude-hooks.json`. SessionEnd becomes a no-op for episodic;
no other hook is affected.
