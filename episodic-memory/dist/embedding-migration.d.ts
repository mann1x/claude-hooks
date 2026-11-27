/**
 * Embedding migration.
 *
 * The encoder was upgraded from all-MiniLM-L6-v2 to bge-small-en-v1.5. Existing
 * databases have vec_exchanges rows produced by the old encoder. This module
 * provides the primitives for an incremental, lock-protected, resumable
 * background migration that re-embeds stale rows in batches during sync.
 *
 *   EMBEDDING_VERSION   — bumped any time the encoder pipeline changes
 *   acquire/release lock — file-based with PID-liveness fallback
 *   pickStaleBatch       — find rows whose embedding_version is behind
 *   recordReembedded     — atomic update of vec_exchanges + version bump
 */
import Database from 'better-sqlite3';
import { acquireFileLock, releaseFileLock, FileLockHandle } from './file-lock.js';
import { ExchangeVectors } from './vector-space.js';
/**
 * Bump when anything in the embedding pipeline changes (model, dtype, prefix,
 * the text an exchange is embedded as).
 *   1  bge-small-en-v1.5
 *   2  exchange text without harness turns, reminders or tool names (exchange-text.ts)
 * A change of model is not a version: it is a new vector space (vector-space.ts).
 */
export declare const EMBEDDING_VERSION = 2;
/**
 * Lock primitives for the migration are the same as for sync (#97) and any
 * other once-per-machine background task — see src/file-lock.ts for the shape.
 * Re-exported here so existing call sites keep their original import names.
 */
export type MigrationLockHandle = FileLockHandle;
export declare const acquireMigrationLock: typeof acquireFileLock;
export declare const releaseMigrationLock: typeof releaseFileLock;
export interface StaleRow {
    id: string;
    user_message: string;
    assistant_message: string;
    tools: string | null;
}
/**
 * Return up to `limit` rows whose embedding_version is older than
 * EMBEDDING_VERSION, joined with their tool names so the caller can
 * reproduce the production exchange-text format.
 */
export declare function pickStaleBatch(db: Database.Database, limit: number): StaleRow[];
/**
 * Replace a row's vec_exchanges embedding and stamp its embedding_version
 * atomically. Wrap each batch's calls in a single transaction at the caller
 * for durability; this function executes its statements in order without
 * starting its own transaction.
 */
export declare function recordReembedded(db: Database.Database, id: string, embedding: ExchangeVectors): void;
/**
 * Count rows whose embedding is older than the current version.
 * Used to decide whether migration is needed and to report progress.
 */
export declare function countStale(db: Database.Database): number;
/** Path of the migration lock under the index directory. */
export declare function getMigrationLockPath(indexDir: string): string;
/** Rows embedded and committed together; an interrupted run keeps every finished group. */
export declare const MIGRATION_GROUP = 32;
export type ExchangeEmbedFn = (user: string, assistant: string, toolNames?: string[]) => Promise<ExchangeVectors>;
export type ExchangeBatchEmbedFn = (rows: Array<{
    userMessage: string;
    assistantMessage: string;
}>) => Promise<ExchangeVectors[]>;
/**
 * Run a single migration batch: re-embed up to `batchSize` rows whose
 * embedding_version is behind. Lock-protected; exits silently if another
 * process holds the lock.
 *
 * Rows go through `embedMany` (one request per group to an embedding server)
 * when given, else `embedFn` row by row, and each group of MIGRATION_GROUP is
 * committed as soon as it is embedded, so a long rebuild that stops keeps
 * what it finished.
 *
 * Returns the number of rows re-embedded (0 if nothing to do or locked out).
 */
export declare function runMigrationBatch(db: Database.Database, indexDir: string, batchSize: number, embedFn: ExchangeEmbedFn, options?: {
    embedMany?: ExchangeBatchEmbedFn;
    group?: number;
    log?: (msg: string) => void;
}): Promise<number>;
