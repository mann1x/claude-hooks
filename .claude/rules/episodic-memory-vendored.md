---
description: Vendored episodic-memory subtree — build, deploy, verify and upstream conventions
globs: episodic-memory/**,scripts/episodic_doctor.py,episodic_server/**
---

- `episodic-memory/` is a git subtree of obra/episodic-memory (imported at upstream `7e06519`, v1.6.0+2, on 2026-09-26) with our patches on top. Never point the server at an out-of-tree checkout — the previous one went 79 commits stale and its native module was unloadable for 12 days while `/health` still said OK.
- Upstream's conventions apply inside the directory (its own `CLAUDE.md`): edit `src/`, run `npm run build`, commit `src/` and `dist/` together. `npx vitest run` is its suite (as root, one `chmod`-based file-lock test fails by design).
- `scripts/deploy.py` never runs `npm run build` — `dist/` is committed, and rebuilding in deploy dirties the tree with dependency drift. It builds on the server host only (`episodic.mode == "server"` in `config/claude-hooks.json`), before services, so `episodic-server` restarts onto the new copy.
- `install_vendored()` in `scripts/episodic_doctor.py` = `npm install` with a C++20 compiler + static C++ runtime when the vendored tree or the Node ABI changed (stamp in `node_modules/`), a load check of every host-built `.node` module, then `npm link`. Returns `False` on any failure and writes no stamp for an install that does not load — never a partial success.
- `scripts/verify_deploy.py` FAILs on the server host when `linked_root()` is not `VENDORED`; `tests/test_deploy_completeness.py` asserts `step_episodic(` precedes `step_services(` and that `check_episodic` exists. A new deployable artifact class gets a test there first.
- Pull upstream / extract our patches for an upstream PR:
  ```bash
  git remote add episodic-upstream https://github.com/obra/episodic-memory.git  # once
  git subtree pull --prefix=episodic-memory episodic-upstream main
  git subtree split --prefix=episodic-memory -b episodic-split
  ```
- Our patches (upstreamable): `EPISODIC_MEMORY_TOOL_INPUT_CHARS` (default 0) caps stored tool inputs/results; `episodic-memory compact [--dry-run] [--no-backup]` applies the cap to existing rows (`VACUUM INTO` + rename, backup at `<db>.pre-compact`).
- Archived transcripts may be stored as `<name>.jsonl.zst`; the canonical name stays `.jsonl` everywhere (DB `archive_path`, `-summary.txt`). Inside `episodic-memory/src`, open transcripts only through `src/transcript-io.ts`, and call `removeCompressedCopy` after writing a fresh archive copy. Sync compresses idle files when `EPISODIC_MEMORY_COMPRESS_AFTER_DAYS` is set; claude-hooks sets it from `episodic.compress_after_days` (default 7) in `session_end.sync_env()` and `episodic_server/server.py:sync_env()`. `/ingest` drops a stale `.zst`.
- After an encoder change run one sync with the migration batch raised (`EPISODIC_MEMORY_MIGRATION_BATCH=60000 episodic-memory sync`); the default 500 per sync leaves new-encoder queries compared against old-encoder vectors until it finishes. Runbook: `docs/episodic-server.md`.
