import Database from 'better-sqlite3';
import { ConversationExchange } from './types.js';
export declare function migrateSchema(db: Database.Database): void;
/**
 * Earlier versions created `tool_calls` with a plain
 * `FOREIGN KEY (exchange_id) REFERENCES exchanges(id)`.
 * Without ON DELETE CASCADE, deleting an exchange that had tool calls
 * raised SQLITE_CONSTRAINT_FOREIGNKEY (#81), and orphans accumulated.
 *
 * This migration:
 *   1. Detects the legacy schema by inspecting sqlite_master.sql.
 *   2. Drops orphaned tool_calls rows.
 *   3. Recreates the table with ON DELETE CASCADE and copies surviving rows.
 */
export declare function migrateToolCallsCascade(db: Database.Database): void;
export declare function initDatabase(): Database.Database;
/**
 * Bump to rebuild every database's keyword index on its next open (a change
 * to the FTS table's columns or tokenizer).
 */
export declare const FTS_VERSION = "1";
/**
 * The ranked keyword index (FTS5, BM25) over exchanges, kept in step with
 * the table by triggers. It indexes `exchanges` by reference (no second copy
 * of the text), keyed on rowid, so:
 *  - `recursive_triggers` is on: `INSERT OR REPLACE` deletes the old row, and
 *    SQLite fires that delete's trigger only with it on — without it the
 *    index keeps the replaced text;
 *  - `VACUUM` (compact's `VACUUM INTO`) may renumber rowids, so compact calls
 *    rebuildFtsIndex() after the swap.
 * Built once per database (or FTS_VERSION), recorded in `index_meta`. When
 * another process holds the write lock the build is left to a later open:
 * vector search works meanwhile and keyword ranking finds nothing new.
 */
export declare function ensureFtsIndex(db: Database.Database): void;
/** Rebuild the keyword index from `exchanges` and record it as current. */
export declare function rebuildFtsIndex(db: Database.Database): void;
export declare function insertExchange(db: Database.Database, exchange: ConversationExchange, embedding: number[], toolNames?: string[]): void;
export declare function getAllExchanges(db: Database.Database): Array<{
    id: string;
    archivePath: string;
}>;
export declare function getFileLastIndexed(db: Database.Database, archivePath: string): number | null;
export declare function deleteExchange(db: Database.Database, id: string): void;
