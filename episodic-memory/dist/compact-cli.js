#!/usr/bin/env node
import { spawnSync } from 'child_process';
import { compactDatabase, compactTempDir } from './compact.js';
import { getDbPath } from './paths.js';
import { getSyncLockPath } from './logging.js';
import { acquireFileLock, readLockHolder, releaseFileLock } from './file-lock.js';
import { getToolInputMaxChars } from './tool-input.js';
const args = process.argv.slice(2);
if (args.includes('--help') || args.includes('-h')) {
    console.log(`
Usage: episodic-memory compact [--dry-run] [--no-backup]

Shrink the index: trim stored tool inputs/results to
EPISODIC_MEMORY_TOOL_INPUT_CHARS (default 0 = drop them; nothing reads them,
and the full call stays in the archived transcript), then rebuild the
database without the freed space.

Takes the sync lock. Stop anything else holding the database open first
(e.g. a server that runs searches): the swap refuses to run otherwise.

OPTIONS:
  --dry-run    Report how many rows would be trimmed; change nothing
  --no-backup  Do not copy the untouched database to <db>.pre-compact first
`);
    process.exit(0);
}
const dbPath = getDbPath();
// SQLite reads SQLITE_TMPDIR when it initializes, before any code here can
// set it, so re-run with it pointing next to the database. Otherwise the
// rebuild's sort files land in /tmp, which is often RAM.
if (!process.env.SQLITE_TMPDIR) {
    const r = spawnSync(process.execPath, process.argv.slice(1), {
        stdio: 'inherit',
        env: { ...process.env, SQLITE_TMPDIR: compactTempDir(dbPath) },
    });
    process.exit(r.status ?? 1);
}
const lockPath = getSyncLockPath();
const lock = acquireFileLock(lockPath);
if (!lock) {
    const holder = readLockHolder(lockPath);
    console.error(`episodic-memory: sync running (${holder !== null ? `pid ${holder}` : 'another process'}); compact not started`);
    process.exit(1);
}
try {
    const result = compactDatabase(dbPath, {
        maxChars: getToolInputMaxChars(),
        keepBackup: !args.includes('--no-backup'),
        dryRun: args.includes('--dry-run'),
        log: (m) => console.log(m),
    });
    if (result.backupPath) {
        console.log(`untouched database kept at ${result.backupPath} (delete it once satisfied)`);
    }
}
catch (err) {
    console.error(`compact failed: ${err instanceof Error ? err.message : String(err)}`);
    process.exitCode = 1;
}
finally {
    releaseFileLock(lock);
}
