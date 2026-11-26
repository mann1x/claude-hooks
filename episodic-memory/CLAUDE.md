# Project Instructions: episodic-memory

A Claude Code plugin that gives semantic search across past Claude Code conversations. This file holds the conventions an AI assistant needs to work on this codebase safely.

## Quick orientation

- **TypeScript source** in `src/`, compiled to `dist/` by `tsc && esbuild`. Both are committed.
- **CLI entry points** in `cli/` (Node.js wrappers — no bash).
- **MCP server** is `dist/mcp-server.js`, launched by `cli/mcp-server-wrapper.js` from the plugin manifest.
- **Tests** live in `test/`, run via `vitest`.
- **Generated files** live in `dist/` and `src/version.ts`. The latter is gitignored; never edit it.

## Build and test

```
npm test          # full suite, runs prebuild step that generates src/version.ts
npm run build     # tsc + esbuild bundle
npm run generate-version   # writes src/version.ts from package.json
```

The `prebuild` and `pretest` hooks both regenerate `src/version.ts`. After source changes that touch the MCP server or any imported module, run `npm run build` so `dist/` reflects source. Commit `dist/` alongside `src/`.

## Version management

Three files hold the plugin version, all kept in lockstep:

- `package.json`
- `.claude-plugin/plugin.json`
- `.claude-plugin/marketplace.json` (plugins[0].version)

Plus a fourth thing outside this repo:

- `../superpowers-marketplace/.claude-plugin/marketplace.json` (the user-visible install registry)

To bump in this repo:

```
./scripts/bump-version.sh X.Y.Z         # updates all three local files
./scripts/bump-version.sh --check       # report current versions
./scripts/bump-version.sh --audit       # scan for stale references
```

The script reads `.version-bump.json` for the file list and audit excludes. `MCP server identity` (the `version` field in `new Server({...})` in `src/mcp-server.ts`) is derived from `src/version.ts`, which the prebuild script generates from `package.json` — keep that pipeline in place; never hardcode the version.

## Release engineering

Follow this every time:

1. **Test:** `npm test` (full suite must pass)
2. **Build:** `npm run build` (commits `dist/` need to be fresh)
3. **Bump:** `./scripts/bump-version.sh X.Y.Z` and verify clean audit
4. **Changelog:** add an entry to `CHANGELOG.md`. Write for end users, not engineers — concrete numbers, plain English, active voice. Lead with the user-visible benefit.
5. **Commit and tag:**
   ```
   git commit -m "Release vX.Y.Z: <one-line>"
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin main && git push origin vX.Y.Z
   ```
6. **GitHub release:**
   ```
   awk '/^## \[X\.Y\.Z\]/,/^## \[/' CHANGELOG.md | sed '$d' | tail -n +2 > /tmp/notes.md
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md
   ```
7. **Bump the marketplace registry** (the load-bearing step that's easy to forget):
   ```
   cd ../superpowers-marketplace
   git pull
   # edit .claude-plugin/marketplace.json, set the episodic-memory entry's
   # "version" to X.Y.Z
   git commit -m "Update episodic-memory to vX.Y.Z" && git push
   ```
   Without this, `/plugin install episodic-memory@superpowers-marketplace` continues serving the previous version. **Users install from the marketplace, not from this repo's tags.**
8. **Smoke test from the published release:** clone the new tag into a tmp dir, `npm install && npm run build && npm test`, then run a synthetic sync + search end-to-end. The full suite covers most paths but doesn't exercise first-install model download or MCP boot.

## Things to be careful with

### The summarizer recursion guard (#87)

`summarizer.ts` calls the Claude Agent SDK's `query()`, which spawns a Claude subprocess that fires `SessionStart` hooks. Our own `SessionStart` hook runs `sync --background`, which calls the summarizer. That loop fans out hundreds of processes within seconds.

The fix:
- `getApiEnv()` always sets `EPISODIC_MEMORY_SUMMARIZER_GUARD=1` in the env it returns to the SDK
- `sync-cli.ts` checks `shouldSkipReentrantSync()` at startup and exits silently when the guard is set

**Anything new that spawns a Claude subprocess via the SDK must inherit this guard.** And nothing should run `sync --background` without checking the guard first. Test with `test/sync-cli-reentrancy.test.ts` style integration if you change the spawn path.

### Embedding migration (1.2.0+)

The `exchanges.embedding_version` column tracks which encoder produced each row's vector. New code stamps `EMBEDDING_VERSION` (in `src/embedding-migration.ts`); old rows from earlier installs default to 0. The sync flow re-embeds stale rows in batches behind a lock at `~/.config/superpowers/conversation-index/.embedding-migration.lock`.

If you change anything in the embedding pipeline (model, dtype, prefix, pooling, normalization, truncation), **bump `EMBEDDING_VERSION`**. That triggers automatic re-embedding for everyone on upgrade. Don't change pipeline behavior silently — search results would degrade against indexed vectors from the old pipeline.

The model cache is `getModelCacheDir()` in `src/paths.ts` (`EPISODIC_MEMORY_MODEL_CACHE`, else `<superpowers>/models`); `initEmbeddings()` sets `transformers.env.cacheDir` to it. Never leave it at transformers.js's default `.cache` inside its package: `npm install` deletes it, and a sandboxed server cannot write there to fetch it back. Spec: `test/model-cache-dir.test.ts`.

A change of *model* is not a version bump: it is a new vector space. `index_meta.embedder` names the space the index holds (an index without the key holds bge-small), `vec_exchanges` is declared at that model's width, and `ensureVectorSpace()` in `src/vector-space.ts` switches the index (recreate the table at the new width, mark every exchange stale) before the first vector from another space is written. Search never compares across spaces: when the index holds another model's vectors, or the embedder is unreachable, default search keeps its BM25 half and `--vector` refuses. Never write vectors outside `writeExchangeVectors()` (it also removes an exchange's old windows), and never assume 384 dimensions. Spec: `test/configured-embedder.test.ts`.

### Configured embedder

`src/embedder-config.ts` resolves the embedder: `EPISODIC_MEMORY_EMBEDDER` (JSON or `local`), else `<superpowers>/embedder.json`, else the bundled bge-small. HTTP backends: `llamafile` (`/embedding`), `ollama` (`/api/embed`), `openai` (`/v1/embeddings`), optional `fallback` of the same model. `model` names the space, not the server, so a rebuild may run on a faster server of the same model. An unreadable config is an error (semantic search off), never a fallback to bge-small, which would switch the space and drop every vector. Query / document prefixes default per model (`modelPrefixes()`: qwen3-embedding gets its instruction). In claude-hooks, `claude_hooks/episodic_embedder.py` writes the file from the memory provider's embedder.

### Exchange text and windows

`src/exchange-text.ts` decides what is embedded: no user side for harness turns (task notifications, continued-session summaries, Stop hook feedback), no system reminders, no tool names, the question capped at 600 characters, and long answers cut into overlapping windows (`composeExchangeChunks()`, ≤ `MAX_CHUNKS`), one vector each, ids `<exchange id>` then `<exchange id>#<k>`. Search maps a window back to its exchange and keeps each exchange once. An exchange with nothing worth a vector gets none and is still found by BM25.

### Keyword fusion (BM25) search

Default search (`both` mode) is reciprocal rank fusion (`fuseByRank()`) of the vector-similarity list and a BM25 keyword list from the `exchanges_fts` FTS5 index. `ensureFtsIndex()` in `src/db.ts` builds and keeps that index in step via triggers (needs `recursive_triggers = ON`); `src/compact.ts` rebuilds it after `VACUUM INTO`. `--text` keeps the exact whole-query substring match. Spec: `test/keyword-fusion.test.ts`.

### Stored tool inputs and `compact`

`tool_calls.tool_input` / `tool_result` are not read back by search, show, or the embedding migration — only `tool_name` is. `src/tool-input.ts` caps what `insertExchange` keeps to `EPISODIC_MEMORY_TOOL_INPUT_CHARS` (default `0` = store `NULL`). `src/compact.ts` / `src/compact-cli.ts` apply the cap to existing rows and rebuild the file via `VACUUM INTO` + atomic rename under the sync lock; it refuses the swap while another connection holds the database open. Don't add readers of those columns without raising the default. Spec: `test/compact.test.ts`.

### Compressed archive (`.jsonl.zst`)

Archived transcripts may be stored as `<name>.jsonl.zst`. The canonical name stays `<name>.jsonl` everywhere (DB `archive_path`, `-summary.txt` derivation, sync's copy logic); only opening a file resolves to the `.zst` sibling. **Never open a transcript with `fs.createReadStream` / `readFileSync` directly** — use `src/transcript-io.ts` (`openTranscriptStream`, `readTranscript`, `statTranscript`, `transcriptExists`). Anything that writes a fresh archive copy must call `removeCompressedCopy`. `src/compress-archive.ts` does the compression (sync runs it when `EPISODIC_MEMORY_COMPRESS_AFTER_DAYS` is set). Spec: `test/transcript-compression.test.ts`.

### `dist/` is committed

Hand-edits to `dist/` get clobbered by `npm run build`. Always edit `src/`, then build, then commit both together. CI doesn't rebuild for you.

### Test isolation

Tests use `mkdtempSync`, set `TEST_DB_PATH`/`TEST_PROJECTS_DIR`/`EPISODIC_MEMORY_CONFIG_DIR` per-test, and clean up in `afterEach`. Don't reach for the real `~/.config/superpowers/`. The `test-utils.ts` helpers cover the common patterns. `test/setup-isolated-config.ts` points `EPISODIC_MEMORY_MODEL_CACHE` at a shared `node_modules/.cache/episodic-test-models`, so embedding tests don't re-download the model per isolated config dir.

## File layout

```
src/
  embeddings.ts          # encoder pipeline (bundled ONNX or a configured server); query and exchange embedders
  embedder-config.ts     # which embedder: EPISODIC_MEMORY_EMBEDDER / <superpowers>/embedder.json / bundled
  vector-space.ts        # the index's vector space (model + width), switch + re-embed, window vectors
  exchange-text.ts       # what an exchange is embedded as: cleaned text, overlapping windows
  embedding-migration.ts # version constant + lock + batch migration
  search.ts              # vector + BM25 keyword fusion (RRF); multi-concept aggregation
  indexer.ts             # incremental index from sources to vec_exchanges
  sync.ts / sync-cli.ts  # source→archive copy + index, with reentrancy guard
  summarizer.ts          # Claude Agent SDK calls; persistSession: false guard
  db.ts                  # schema + migrations (incl. cascade, embedding_version, exchanges_fts)
  tool-input.ts          # EPISODIC_MEMORY_TOOL_INPUT_CHARS cap for tool_input/tool_result
  compact.ts / compact-cli.ts  # trim stored tool inputs + VACUUM INTO rebuild
  transcript-io.ts       # open/stat/read transcripts, .jsonl or .jsonl.zst
  compress-archive.ts / compress-archive-cli.ts  # zstd idle archived transcripts
  paths.ts               # config/index/archive/model-cache directory resolution
  parser.ts              # JSONL transcript → exchanges
  mcp-server.ts          # MCP tool surface (search, read)
  version.ts             # GENERATED — do not edit

cli/
  episodic-memory.js     # umbrella CLI dispatcher
  mcp-server-wrapper.js  # ensures deps are installed before launching the server
  *.js                   # subcommand entry points

scripts/
  bump-version.sh        # version bumper with drift audit
  generate-version.js    # writes src/version.ts from package.json
```

## When in doubt

Read the relevant test file. Tests in this repo are the executable spec — particularly `test/embedding-migration.test.ts`, `test/sync-cli-reentrancy.test.ts`, `test/tool-calls-cascade.test.ts`, and `test/compact.test.ts`. They cover the load-bearing invariants (lock contention, recursion-guard, schema migrations, index compaction) and exercise the real subsystems rather than mocking them.

<!-- caliber:managed:pre-commit -->
## Before Committing

**IMPORTANT:** Before every git commit, you MUST ensure Caliber syncs agent configs with the latest code changes.

First, check if the pre-commit hook is already installed:
```bash
grep -q "caliber" .git/hooks/pre-commit 2>/dev/null && echo "hook-active" || echo "no-hook"
```

- If **hook-active**: the hook handles sync automatically — just commit normally. Tell the user: "Caliber will sync your agent configs automatically via the pre-commit hook."
- If **no-hook**: run Caliber manually before committing:
  1. Tell the user: "Caliber: Syncing agent configs with your latest changes..."
  2. Run: `caliber refresh && git add CALIBER_LEARNINGS.md CLAUDE.md .claude/ 2>/dev/null`
  3. After it completes, briefly tell the user what Caliber updated. Then proceed with the commit.

**Valid `caliber refresh` options:** `--quiet` (suppress output) and `--dry-run` (preview without writing). Do not pass any other flags — options like `--auto-approve`, `--debug`, or `--force` do not exist and will cause errors.

**`caliber config`** takes no flags — it runs an interactive provider setup. Do not pass `--provider`, `--api-key`, or `--endpoint`.

If `caliber` is not found, tell the user: "This project uses Caliber for agent config sync. Run /setup-caliber to get set up."
<!-- /caliber:managed:pre-commit -->

<!-- caliber:managed:learnings -->
## Session Learnings

Read `CALIBER_LEARNINGS.md` for patterns and anti-patterns learned from previous sessions.
These are auto-extracted from real tool usage — treat them as project-specific rules.
<!-- /caliber:managed:learnings -->

<!-- caliber:managed:sync -->
## Context Sync

This project uses [Caliber](https://github.com/caliber-ai-org/ai-setup) to keep AI agent configs in sync across Claude Code, Cursor, Copilot, and Codex.
Configs update automatically before each commit via `caliber refresh`.
If the pre-commit hook is not set up, run `/setup-caliber` to configure everything automatically.
<!-- /caliber:managed:sync -->
