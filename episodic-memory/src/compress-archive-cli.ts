#!/usr/bin/env node
import { compressArchive, getCompressAfterDays } from './compress-archive.js';
import { DEFAULT_COMPRESSION_LEVEL } from './transcript-io.js';
import { getArchiveDir } from './paths.js';
import { getSyncLockPath } from './logging.js';
import { acquireFileLock, readLockHolder, releaseFileLock } from './file-lock.js';

const args = process.argv.slice(2);

if (args.includes('--help') || args.includes('-h')) {
  console.log(`
Usage: episodic-memory compress-archive [--after-days N] [--level L] [--dry-run]

Store archived transcripts not modified for N days as <name>.jsonl.zst
(zstd; JSON lines shrink ~6-7x at level 3). Every command still reads them
by their .jsonl name. Each file is verified by decompressing it before the
original is removed, and keeps its original mtime.

Sync does this itself when EPISODIC_MEMORY_COMPRESS_AFTER_DAYS is set.

OPTIONS:
  --after-days N  Idle threshold in days (default: EPISODIC_MEMORY_COMPRESS_AFTER_DAYS, else 7)
  --level L       zstd level (default ${DEFAULT_COMPRESSION_LEVEL})
  --dry-run       Report what would be compressed; change nothing
`);
  process.exit(0);
}

function flag(name: string): string | undefined {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
}

const afterDays = Number.parseFloat(flag('--after-days') ?? '') || getCompressAfterDays() || 7;
const level = Number.parseInt(flag('--level') ?? '', 10) || DEFAULT_COMPRESSION_LEVEL;

const lockPath = getSyncLockPath();
const lock = acquireFileLock(lockPath);
if (!lock) {
  const holder = readLockHolder(lockPath);
  console.error(`episodic-memory: sync running (${holder !== null ? `pid ${holder}` : 'another process'}); compress-archive not started`);
  process.exit(1);
}

try {
  const r = await compressArchive(getArchiveDir(), {
    afterDays, level, dryRun: args.includes('--dry-run'), log: (m) => console.log(m),
  });
  for (const e of r.errors) console.error(`  ${e.file}: ${e.error}`);
  if (r.errors.length) process.exitCode = 1;
} finally {
  releaseFileLock(lock);
}
