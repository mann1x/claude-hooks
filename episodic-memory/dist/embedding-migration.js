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
import path from 'path';
import { acquireFileLock, releaseFileLock } from './file-lock.js';
import { writeExchangeVectors } from './vector-space.js';
/**
 * Bump when anything in the embedding pipeline changes (model, dtype, prefix,
 * the text an exchange is embedded as).
 *   1  bge-small-en-v1.5
 *   2  exchange text without harness turns, reminders or tool names (exchange-text.ts)
 * A change of model is not a version: it is a new vector space (vector-space.ts).
 */
export const EMBEDDING_VERSION = 2;
export const acquireMigrationLock = acquireFileLock;
export const releaseMigrationLock = releaseFileLock;
/**
 * Return up to `limit` rows whose embedding_version is older than
 * EMBEDDING_VERSION, joined with their tool names so the caller can
 * reproduce the production exchange-text format.
 */
export function pickStaleBatch(db, limit) {
    return db.prepare(`
    SELECT
      e.id,
      e.user_message,
      e.assistant_message,
      GROUP_CONCAT(DISTINCT tc.tool_name) AS tools
    FROM exchanges e
    LEFT JOIN tool_calls tc ON tc.exchange_id = e.id
    WHERE e.embedding_version < ?
    GROUP BY e.id
    LIMIT ?
  `).all(EMBEDDING_VERSION, limit);
}
/**
 * Replace a row's vec_exchanges embedding and stamp its embedding_version
 * atomically. Wrap each batch's calls in a single transaction at the caller
 * for durability; this function executes its statements in order without
 * starting its own transaction.
 */
export function recordReembedded(db, id, embedding) {
    writeExchangeVectors(db, id, embedding);
    db.prepare('UPDATE exchanges SET embedding_version = ? WHERE id = ?')
        .run(EMBEDDING_VERSION, id);
}
/**
 * Count rows whose embedding is older than the current version.
 * Used to decide whether migration is needed and to report progress.
 */
export function countStale(db) {
    const row = db.prepare('SELECT COUNT(*) AS c FROM exchanges WHERE embedding_version < ?').get(EMBEDDING_VERSION);
    return row.c;
}
/** Path of the migration lock under the index directory. */
export function getMigrationLockPath(indexDir) {
    return path.join(indexDir, '.embedding-migration.lock');
}
/** Rows embedded and committed together; an interrupted run keeps every finished group. */
export const MIGRATION_GROUP = 32;
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
export async function runMigrationBatch(db, indexDir, batchSize, embedFn, options = {}) {
    const remaining = countStale(db);
    if (remaining === 0)
        return 0;
    const lockPath = getMigrationLockPath(indexDir);
    const lock = acquireMigrationLock(lockPath);
    if (!lock) {
        console.error(`episodic-memory: another process is migrating embeddings (${remaining} rows still stale); skipping`);
        return 0;
    }
    const group = Math.max(1, options.group ?? MIGRATION_GROUP);
    const log = options.log ?? ((m) => console.error(m));
    try {
        const rows = pickStaleBatch(db, batchSize);
        if (rows.length === 0)
            return 0;
        log(`episodic-memory: re-embedding batch of ${rows.length} (${remaining} stale total)...`);
        const writeTx = db.transaction((items) => {
            for (const item of items)
                recordReembedded(db, item.id, item.vec);
        });
        const started = Date.now();
        let done = 0;
        let lastLog = started;
        for (let i = 0; i < rows.length; i += group) {
            const slice = rows.slice(i, i + group);
            // Embed outside the transaction (async work), then write the group atomically.
            let vecs;
            if (options.embedMany) {
                vecs = await options.embedMany(slice.map((r) => ({
                    userMessage: r.user_message, assistantMessage: r.assistant_message,
                })));
            }
            else {
                vecs = [];
                for (const row of slice) {
                    const tools = row.tools ? row.tools.split(',') : undefined;
                    vecs.push(await embedFn(row.user_message, row.assistant_message, tools));
                }
            }
            writeTx(slice.map((row, j) => ({ id: row.id, vec: vecs[j] })));
            done += slice.length;
            if (Date.now() - lastLog > 30_000) {
                lastLog = Date.now();
                const rate = done / ((lastLog - started) / 1000);
                log(`episodic-memory: re-embedded ${done}/${rows.length} (${rate.toFixed(1)}/s)`);
            }
        }
        return done;
    }
    finally {
        releaseMigrationLock(lock);
    }
}
