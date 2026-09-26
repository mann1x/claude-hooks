import fs from 'fs';
import path from 'path';
import { compressTranscript, DEFAULT_COMPRESSION_LEVEL } from './transcript-io.js';

export interface CompressOptions {
  /** Only transcripts not modified for this many days. */
  afterDays: number;
  level?: number;
  dryRun?: boolean;
  /** Stop after this many files (0 = no limit), to bound one run's time. */
  maxFiles?: number;
  now?: number;
  log?: (msg: string) => void;
}

export interface CompressResult {
  files: number;
  bytesBefore: number;
  bytesAfter: number;
  errors: Array<{ file: string; error: string }>;
}

/** EPISODIC_MEMORY_COMPRESS_AFTER_DAYS: 0 or unset = sync never compresses. */
export function getCompressAfterDays(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.EPISODIC_MEMORY_COMPRESS_AFTER_DAYS;
  const parsed = raw !== undefined ? Number.parseFloat(raw) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function* archivedTranscripts(dir: string): Generator<string> {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      yield* archivedTranscripts(p);
    } else if (e.isFile() && e.name.endsWith('.jsonl')) {
      yield p;
    }
  }
}

/**
 * Compress archived transcripts idle for `afterDays` into `<name>.jsonl.zst`
 * (see transcript-io.ts). The caller holds the sync lock, so nothing else
 * is copying into the archive meanwhile. A transcript that grows again is
 * re-copied as plain `.jsonl` by sync, which drops the stale `.zst`.
 */
export async function compressArchive(archiveDir: string, opts: CompressOptions): Promise<CompressResult> {
  const log = opts.log ?? (() => {});
  const cutoff = (opts.now ?? Date.now()) - opts.afterDays * 86_400_000;
  const result: CompressResult = { files: 0, bytesBefore: 0, bytesAfter: 0, errors: [] };
  for (const file of archivedTranscripts(archiveDir)) {
    if (opts.maxFiles && result.files >= opts.maxFiles) break;
    let st: fs.Stats;
    try {
      st = fs.statSync(file);
    } catch {
      continue;
    }
    if (st.mtimeMs > cutoff) continue;
    if (opts.dryRun) {
      result.files++;
      result.bytesBefore += st.size;
      continue;
    }
    try {
      const { before, after } = await compressTranscript(file, opts.level ?? DEFAULT_COMPRESSION_LEVEL);
      result.files++;
      result.bytesBefore += before;
      result.bytesAfter += after;
    } catch (err) {
      result.errors.push({ file, error: err instanceof Error ? err.message : String(err) });
    }
  }
  const gb = (n: number) => (n / 1e9).toFixed(2);
  log(opts.dryRun
    ? `${result.files} transcript(s) idle > ${opts.afterDays} d, ${gb(result.bytesBefore)} GB`
    : `compressed ${result.files} transcript(s): ${gb(result.bytesBefore)} GB -> ${gb(result.bytesAfter)} GB` +
      (result.errors.length ? `, ${result.errors.length} error(s)` : ''));
  return result;
}
