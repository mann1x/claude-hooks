import fs from 'fs';
import path from 'path';
import type Database from 'better-sqlite3';
import { ZST_SUFFIX } from './transcript-io.js';

/**
 * Archived transcripts the index still owes exchanges to.
 *
 * Sync used to index only the files it copied in the same run, and the
 * archive copy keeps the source's mtime, so a file whose indexing was skipped
 * or failed — the embedder down (the 26-day outage of 2026-10-31..11-26), an
 * exclusion marker read in its tool output, an error — counted as current
 * and was never looked at again until its source changed. On one index that
 * left 17,838 exchanges in 4,450 conversations unindexed, among them the
 * largest session of most projects.
 *
 * Now such a file is listed here, and every sync works through the list after
 * the files it copied, within EPISODIC_MEMORY_INDEX_BATCH exchanges a run
 * (default 500, so a CPU embedder never holds a sync for days). A file leaves
 * the list once every exchange past its high-water mark is indexed, or when
 * there is nothing to index (gone, spawned, DO NOT INDEX).
 */
export const CATCHUP_VERSION = '1';
export const DEFAULT_INDEX_BATCH = 500;

export function getIndexBatch(env: NodeJS.ProcessEnv = process.env): number {
  const n = Number.parseInt(env.EPISODIC_MEMORY_INDEX_BATCH ?? '', 10);
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_INDEX_BATCH;
}

export function ensurePendingTable(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS index_pending (
      archive_path TEXT PRIMARY KEY,
      reason TEXT NOT NULL,
      since TEXT NOT NULL
    )
  `);
}

export function addPending(db: Database.Database, paths: readonly string[], reason: string): void {
  if (paths.length === 0) return;
  ensurePendingTable(db);
  const stmt = db.prepare(
    `INSERT INTO index_pending (archive_path, reason, since) VALUES (?, ?, ?)
     ON CONFLICT(archive_path) DO UPDATE SET reason = excluded.reason`
  );
  const now = new Date().toISOString();
  db.transaction(() => { for (const p of paths) stmt.run(p, reason, now); })();
}

export function removePending(db: Database.Database, archivePath: string): void {
  ensurePendingTable(db);
  db.prepare(`DELETE FROM index_pending WHERE archive_path = ?`).run(archivePath);
}

/** Oldest first, so a file that keeps failing does not starve the rest forever. */
export function listPending(db: Database.Database): string[] {
  ensurePendingTable(db);
  return (db.prepare(`SELECT archive_path AS p FROM index_pending ORDER BY since, archive_path`).all() as
    Array<{ p: string }>).map((r) => r.p);
}

export function countPending(db: Database.Database): number {
  ensurePendingTable(db);
  return (db.prepare(`SELECT COUNT(*) AS c FROM index_pending`).get() as { c: number }).c;
}

/** Every transcript under the archive, by canonical (`.jsonl`) name. */
export function archivedTranscripts(archiveDir: string): string[] {
  const out = new Set<string>();
  const walk = (dir: string) => {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.jsonl')) out.add(p);
      else if (e.name.endsWith('.jsonl' + ZST_SUFFIX)) out.add(p.slice(0, -ZST_SUFFIX.length));
    }
  };
  walk(archiveDir);
  return [...out].sort();
}

/**
 * List the whole archive once per CATCHUP_VERSION (recorded in `index_meta`),
 * so files that fell through before this list existed are picked up. Files
 * already complete leave the list on their first look. Returns how many were
 * listed (0 when already done).
 */
export function seedCatchUp(
  db: Database.Database,
  archiveDir: string,
  log: (msg: string) => void = (m) => console.error(m)
): number {
  db.exec(`CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`);
  const done = db.prepare(`SELECT value FROM index_meta WHERE key = 'catchup'`).get() as
    | { value: string }
    | undefined;
  if (done?.value === CATCHUP_VERSION) return 0;
  const files = archivedTranscripts(archiveDir);
  ensurePendingTable(db);
  const stmt = db.prepare(
    `INSERT OR IGNORE INTO index_pending (archive_path, reason, since) VALUES (?, 'catch-up', ?)`
  );
  const now = new Date().toISOString();
  db.transaction(() => {
    for (const p of files) stmt.run(p, now);
    db.prepare(`INSERT OR REPLACE INTO index_meta (key, value) VALUES ('catchup', ?)`).run(CATCHUP_VERSION);
  })();
  if (files.length > 0) {
    log(`episodic-memory: checking ${files.length} archived transcript(s) for exchanges the index ` +
        `is missing (EPISODIC_MEMORY_INDEX_BATCH a sync)`);
  }
  return files.length;
}
