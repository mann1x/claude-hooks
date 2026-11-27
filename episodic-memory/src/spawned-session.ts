import readline from 'readline';
import type Database from 'better-sqlite3';
import { openTranscriptStream, transcriptExists } from './transcript-io.js';

/**
 * Spawned Claude Code runs — `claude -p` and Agent SDK sessions: Caliber's
 * refresh and learning extraction, OpenWolf's reports, skill generators,
 * workflow agents — are prompts a tool wrote, not work the user did. They are
 * not indexed (nor summarized), and searches stop surfacing them. On one
 * index they were 5,193 conversations / 5,280 of 71,807 exchanges.
 *
 * Signals, from the first HEAD_RECORDS records:
 *  - an `entrypoint` starting `sdk-` (`sdk-cli` is `claude -p`; `sdk-ts`,
 *    `sdk-py` the SDKs). Current Claude Code writes it on every message.
 *  - Older versions wrote `entrypoint: "cli"` for `claude -p` as well, but such
 *    a run's file opens with its queued prompt (`queue-operation` / `enqueue`)
 *    and never has a `file-history-snapshot`. An interactive session opens
 *    with something else and snapshots at its first prompt.
 * A subagent transcript (`<session>/subagents/...`) follows its parent.
 * Other harnesses' transcripts never match; an unreadable file is not spawned.
 *
 * `EPISODIC_MEMORY_INDEX_SPAWNED=1` indexes them again.
 */
export const HEAD_RECORDS = 64;

/** Bump to re-run the purge of an existing index after a rule change. */
export const SPAWNED_FILTER_VERSION = '1';

export function indexSpawnedSessions(): boolean {
  return /^(1|true|yes|on)$/i.test(process.env.EPISODIC_MEMORY_INDEX_SPAWNED ?? '');
}

/** True when the transcript should stay out of the index (spawned, and not opted back in). */
export async function excludeAsSpawned(canonical: string): Promise<boolean> {
  return !indexSpawnedSessions() && (await isSpawnedSession(canonical));
}

export async function isSpawnedSession(canonical: string): Promise<boolean> {
  const parent = parentSessionOf(canonical);
  if (parent && transcriptExists(parent) && (await ownSignal(parent))) return true;
  return ownSignal(canonical);
}

/** `<dir>/<session>/subagents/.../agent-x.jsonl` → `<dir>/<session>.jsonl`. */
export function parentSessionOf(p: string): string | null {
  const m = /^(.*?)([\\/])([^\\/]+)\2subagents\2/.exec(p);
  return m ? `${m[1]}${m[2]}${m[3]}.jsonl` : null;
}

async function ownSignal(canonical: string): Promise<boolean> {
  let stream;
  try {
    stream = openTranscriptStream(canonical);
  } catch {
    return false;
  }
  stream.on('error', () => {});
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  let records = 0;
  let enqueueFirst = false;
  let snapshot = false;
  try {
    for await (const line of rl) {
      if (!line.trim()) continue;
      let rec: any;
      try {
        rec = JSON.parse(line);
      } catch {
        continue;
      }
      if (!rec || typeof rec !== 'object') continue;
      if (records === 0) enqueueFirst = rec.type === 'queue-operation' && rec.operation === 'enqueue';
      records++;
      if (typeof rec.entrypoint === 'string' && rec.entrypoint.startsWith('sdk-')) return true;
      if (rec.type === 'file-history-snapshot') snapshot = true;
      if (records >= HEAD_RECORDS) break;
    }
  } catch {
    return false;
  } finally {
    rl.close();
    stream.destroy();
  }
  return enqueueFirst && !snapshot;
}

/**
 * Remove spawned sessions already in the index, once per SPAWNED_FILTER_VERSION
 * (recorded in `index_meta`). The archived transcripts are kept, so
 * `EPISODIC_MEMORY_INDEX_SPAWNED=1` plus a reindex brings them back.
 * Returns the number of exchanges removed.
 */
export async function purgeSpawnedSessions(
  db: Database.Database,
  log: (msg: string) => void = (m) => console.error(m)
): Promise<number> {
  if (indexSpawnedSessions()) return 0;
  db.exec(`CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`);
  const done = db.prepare(`SELECT value FROM index_meta WHERE key = 'spawned_filter'`).get() as
    | { value: string }
    | undefined;
  if (done?.value === SPAWNED_FILTER_VERSION) return 0;

  const paths = (db.prepare(`SELECT DISTINCT archive_path AS p FROM exchanges`).all() as Array<{ p: string }>)
    .map((r) => r.p);
  const doomed: string[] = [];
  for (const p of paths) {
    if (await isSpawnedSession(p)) doomed.push(p);
  }

  const { deleteExchange } = await import('./db.js');
  const ids = db.prepare(`SELECT id FROM exchanges WHERE archive_path = ?`);
  let removed = 0;
  db.transaction(() => {
    for (const p of doomed) {
      for (const { id } of ids.all(p) as Array<{ id: string }>) {
        deleteExchange(db, id);
        removed++;
      }
    }
    db.prepare(`INSERT OR REPLACE INTO index_meta (key, value) VALUES ('spawned_filter', ?)`)
      .run(SPAWNED_FILTER_VERSION);
  })();
  if (removed > 0) {
    log(`episodic-memory: removed ${removed} exchange(s) of ${doomed.length} spawned session(s) ` +
        `(claude -p / Agent SDK) from the index; their transcripts stay archived`);
  }
  return removed;
}
