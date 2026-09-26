export interface CompactOptions {
    /** Keep at most this many characters of tool_input / tool_result (0 = none). */
    maxChars: number;
    /** Copy the untouched database to `<db>.pre-compact` before changing it. */
    keepBackup?: boolean;
    /** Count what would change and stop. */
    dryRun?: boolean;
    log?: (msg: string) => void;
}
export interface CompactResult {
    rowsTrimmed: number;
    bytesBefore: number;
    bytesAfter: number;
    backupPath: string | null;
}
/**
 * Trim stored tool inputs/results to `maxChars` and rebuild the database
 * without the freed space.
 *
 * The rebuild is `VACUUM INTO` a sibling file, then an atomic rename over
 * the original — never plain `VACUUM`, which builds its copy in the temp
 * directory (often a RAM-backed /tmp) and needs as much free space there as
 * the database is large. The caller must hold the sync lock; the swap
 * refuses to run if another connection still has the database open, since
 * the new file would inherit that connection's -wal and -shm.
 */
export declare function compactDatabase(dbPath: string, opts: CompactOptions): CompactResult;
/** Where compaction's temp files must go: next to the database, not /tmp. */
export declare function compactTempDir(dbPath: string): string;
