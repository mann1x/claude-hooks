# sqlite-vec MCP (v1.6+)

Same shape as [`pgvector-mcp`](pgvector-runbook.md): a system-wide
stdio MCP launcher (with optional HTTP daemon) that lets external
MCP clients — Cursor, Codex, OpenWebUI, Claude Desktop — recall +
store against the **same** sqlite-vec `.db` file the claude-hooks
hook pipeline reads in-process. No new schema, no new database; one
sqlite_vec store, two access paths.

## What the server exposes

Three tools in v1.6.0 (memory only — no KG, no FTS5 hybrid):

| Tool | Backs onto | Notes |
|------|------------|-------|
| `sqlite-vec-find` | `SqliteVecProvider.recall(query, k)` | Pure vector cosine distance. `k` default 5, max 50. |
| `sqlite-vec-store` | `SqliteVecProvider.store(content, metadata)` | SQLite serialises writers, so concurrent stores from multiple MCP clients will queue rather than collide. |
| `sqlite-vec-count` | `SqliteVecProvider.count()` | Row count in the configured primary table. |

The embedder used by the server is whatever
`cfg.providers.sqlite_vec.embedder` is set to — Ollama, llamafile,
OpenAI-compat. Same config the hook pipeline reads. If the embedder
is unavailable, the server returns a clean JSON-RPC tool error
instead of crashing; the loop keeps running for the next request.

Hybrid recall (FTS5) and a KG layer are out of scope for v1.6.0 —
they'd require new methods on `SqliteVecProvider` + new tables.
Filed as follow-ups; bring it up if you want them prioritised.

## Wire-up at install time

`install.py`'s sqlite_vec sub-dialog now ends with a launcher prompt:

```
--- sqlite_vec ---
  ... (db_path + embedder dialog) ...
  Install system-wide MCP launcher? [Y/n]:
```

On Y, the installer drops:

- **POSIX:** `~/.local/bin/sqlite-vec-mcp` — a tiny `sh` script that
  exports `PYTHONPATH=<repo>` and execs the configured Python with
  `-m claude_hooks.sqlite_vec_mcp`.
- **Windows:** `%LOCALAPPDATA%\claude-hooks\bin\sqlite-vec-mcp.cmd`
  — the same shape via `cmd.exe`.

It then registers the launcher in `~/.claude.json` at the root
`mcpServers` map:

```json
{
  "mcpServers": {
    "sqlite_vec": {
      "type": "stdio",
      "command": "/home/you/.local/bin/sqlite-vec-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

A semantic backup of the previous `~/.claude.json` is written next
to it (`.claude.json.bak-<ts>-sqlite-vec-mcp`) before the rewrite.

If the launcher is already present on a re-run, the dialog offers
the v1.5.4-style `[V]alidate only / [R]e-install / [S]kip? [V/r/s]`
choice. `V` (default) spawns the launcher, sends a single
`initialize` request, reads one response, and reports OK/FAIL — no
writes, no .db file touched.

## Pointing external MCP clients at it

Once the launcher is in `~/.claude.json`, Claude Code itself spawns
it automatically per session. For other clients:

**Cursor / Codex / Claude Desktop** — add an entry to their MCP
config pointing at the same absolute path:

```json
{
  "mcpServers": {
    "sqlite_vec": {
      "command": "/home/you/.local/bin/sqlite-vec-mcp"
    }
  }
}
```

**OpenWebUI** (and anything that wants HTTP) — bring up the optional
systemd unit:

```bash
sudo cp systemd/claude-hooks-sqlite-vec-mcp.service /etc/systemd/system/
sudo sed -i "s|__REPO_PATH__|$(pwd)|g" \
  /etc/systemd/system/claude-hooks-sqlite-vec-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now claude-hooks-sqlite-vec-mcp
```

The HTTP server listens on `0.0.0.0:32777/mcp`. Override host/port
via the `SQLITE_VEC_MCP_HTTP_HOST` / `SQLITE_VEC_MCP_HTTP_PORT`
environment variables (drop-in under
`/etc/systemd/system/claude-hooks-sqlite-vec-mcp.service.d/`).

Quick smoke:

```bash
curl -sX POST http://127.0.0.1:32777/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq
```

Returns the three-tool catalog.

## Port-and-name table

| Service | HTTP port | Launcher path (POSIX) | Launcher path (Windows) |
|---|---|---|---|
| pgvector-mcp | 32775 | `~/.local/bin/pgvector-mcp` | `%LOCALAPPDATA%\claude-hooks\bin\pgvector-mcp.cmd` |
| memory-kg MCP | 32776 | — (external server) | — |
| sqlite-vec-mcp (v1.6+) | **32777** | `~/.local/bin/sqlite-vec-mcp` | `%LOCALAPPDATA%\claude-hooks\bin\sqlite-vec-mcp.cmd` |

## Known limitations

- **One writer at a time.** SQLite serialises writers on a single
  `.db` file. Two MCP clients calling `sqlite-vec-store`
  simultaneously will queue rather than fail, but the latency
  shows. For high-write workloads run pgvector instead.
- **No FTS5 hybrid in v1.6.** Tool catalog ships
  `sqlite-vec-find` (pure vector) only. Hybrid lands when
  `SqliteVecProvider.recall_hybrid` does.
- **No KG.** sqlite_vec has no entity/observation/relation tables.
  Use pgvector if you need the KG side.
- **HTTP daemon is Linux/systemd only in v1.6.** Stdio launcher is
  cross-platform; the systemd unit is not yet wrapped as a Windows
  scheduled task. File an issue if you want it.

## See also

- [`docs/pgvector-runbook.md`](pgvector-runbook.md) — the pgvector
  counterpart; same shape, different store.
- [`docs/llamafile-integration.md`](llamafile-integration.md) —
  embedder backend used by the MCP server when configured.
