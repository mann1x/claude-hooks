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

- `episodic-memory/` — the indexer itself, vendored as a git subtree of
  obra/episodic-memory with our patches on top (see
  [Vendored episodic-memory](#vendored-episodic-memory))
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
cd /path/to/claude-hooks
python3 install.py --episodic-server
```

Needs Node.js and npm. The installer:
- Builds the vendored `episodic-memory/` (`npm install` with a C++20
  toolchain, native-module load check) and `npm link`s it, so the
  `episodic-memory` on PATH is this copy
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
  "timeout": 10.0,                               // client push timeout
  "compress_after_days": 7                       // server: zstd archived transcripts idle this long (0 = never)
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

## Vendored episodic-memory

`episodic-memory/` is a git subtree of
[obra/episodic-memory](https://github.com/obra/episodic-memory),
imported at upstream `7e06519` (v1.6.0+2) on 2026-09-26. Before that
the server ran an out-of-tree checkout that nothing in this repo built,
verified or updated: it sat at 1.0.15, 79 commits behind, when its
native module broke.

```bash
# pull upstream (keep our commits on top; resolve conflicts as usual)
git remote add episodic-upstream https://github.com/obra/episodic-memory.git  # once
git subtree pull --prefix=episodic-memory episodic-upstream main
# extract our changes for an upstream PR
git subtree split --prefix=episodic-memory -b episodic-split
```

Upstream's conventions apply inside the directory (its own
`CLAUDE.md`): edit `src/`, `npm run build`, commit `src/` and `dist/`
together; `npx vitest run` is its suite (as root, one file-lock test
that relies on `chmod` fails by design).

`scripts/deploy.py` builds it on the server host: `npm install` with
the toolchain below when the vendored tree or the Node ABI changed,
a load check, `npm link`. `verify_deploy.py` fails when the CLI on
PATH is not the vendored copy.

Our patches (all upstreamable):

| change | why |
|---|---|
| `EPISODIC_MEMORY_TOOL_INPUT_CHARS` (default 0) caps stored `tool_calls.tool_input` / `tool_result` | nothing reads them back; on solidpc they were 6.5 GB of an 8.0 GB index |
| `episodic-memory compact [--dry-run] [--no-backup]` | applies the cap to existing rows, rebuilds with `VACUUM INTO` + rename, backs the untouched db up to `<db>.pre-compact` first. solidpc: 8.02 → 2.04 GB in 76 s |
| compressed archive: `<name>.jsonl.zst`, read transparently | the archive was 15 GB of JSON lines that compress ~6.7× at zstd level 3 |
| `episodic-memory compress-archive [--after-days N] [--level L] [--dry-run]`, and sync compresses on its own when `EPISODIC_MEMORY_COMPRESS_AFTER_DAYS` is set | see below |
| the indexer re-copies an archived transcript only when its source is newer | it re-copied every previously indexed file on every run, which would also silently undo compression |

### Compressed archive

A transcript's name is always `<name>.jsonl` — in the database, in
`-summary.txt` paths, in sync's copy logic — and only opening it
resolves to `<name>.jsonl.zst` when that is what is on disk
(`src/transcript-io.ts`). Parser, search, `show`, the MCP `read` tool,
verify and `findJsonlFiles` all go through it, so search, show and
indexing work unchanged. Decompression is Node's built-in zstd (Node
≥ 22.15). For a human: `zstdcat x.jsonl.zst | jq`, `zstdgrep`.

- **What gets compressed:** archived transcripts not modified for
  `episodic.compress_after_days` (default 7). claude-hooks passes it to
  every sync it starts (SessionEnd on the server, `/ingest`, `/sync`) as
  `EPISODIC_MEMORY_COMPRESS_AFTER_DAYS`, and sync compresses at the end
  of its run, under its lock. Each file is decompressed and hashed
  against the original before the original is removed, and keeps the
  original's mtime, so sync still sees the copy as current.
- **A resumed session:** its source becomes newer, sync copies a fresh
  plain `.jsonl` and drops the stale `.zst`. A re-pushed `/ingest` does
  the same.
- **Live transcripts** in `~/.claude/projects` are never touched:
  Claude Code appends to them and reads them to resume a session.
- **The first run** over an existing archive is large; do it by hand
  rather than inside a SessionEnd-triggered sync:
  `episodic-memory compress-archive --dry-run`, then without.

Upstream re-embeds at most `EPISODIC_MEMORY_MIGRATION_BATCH` (500)
stale exchanges per sync after an encoder change. Until all are done,
queries from the new encoder are compared against old-encoder vectors,
so for a large index run one sync with the batch raised:
`EPISODIC_MEMORY_MIGRATION_BATCH=60000 episodic-memory sync`.

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
| `which episodic-memory` exits non-zero | Vendored indexer not built/linked on the server host | `python3 scripts/deploy.py` (or `install.py --episodic-server`) |
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
