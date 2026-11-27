import type Database from 'better-sqlite3';
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
export declare const CATCHUP_VERSION = "1";
export declare const DEFAULT_INDEX_BATCH = 500;
export declare function getIndexBatch(env?: NodeJS.ProcessEnv): number;
export declare function ensurePendingTable(db: Database.Database): void;
export declare function addPending(db: Database.Database, paths: readonly string[], reason: string): void;
export declare function removePending(db: Database.Database, archivePath: string): void;
/** Oldest first, so a file that keeps failing does not starve the rest forever. */
export declare function listPending(db: Database.Database): string[];
export declare function countPending(db: Database.Database): number;
/** Every transcript under the archive, by canonical (`.jsonl`) name. */
export declare function archivedTranscripts(archiveDir: string): string[];
/**
 * List the whole archive once per CATCHUP_VERSION (recorded in `index_meta`),
 * so files that fell through before this list existed are picked up. Files
 * already complete leave the list on their first look. Returns how many were
 * listed (0 when already done).
 */
export declare function seedCatchUp(db: Database.Database, archiveDir: string, log?: (msg: string) => void): number;
