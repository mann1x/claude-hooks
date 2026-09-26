import fs from 'fs';
import path from 'path';
import Database from 'better-sqlite3';
import * as sqliteVec from 'sqlite-vec';
function sizeOf(p) {
    try {
        return fs.statSync(p).size;
    }
    catch {
        return 0;
    }
}
function dbBytes(dbPath) {
    return sizeOf(dbPath) + sizeOf(`${dbPath}-wal`);
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
export function compactDatabase(dbPath, opts) {
    const log = opts.log ?? (() => { });
    const maxChars = Math.max(0, opts.maxChars);
    const bytesBefore = dbBytes(dbPath);
    const db = new Database(dbPath);
    sqliteVec.load(db);
    db.pragma('busy_timeout = 30000');
    // Sorts during the rebuild spill to files, and the caller points
    // SQLITE_TMPDIR at the database's own directory.
    db.pragma('temp_store = FILE');
    const over = maxChars === 0
        ? `(tool_input IS NOT NULL OR tool_result IS NOT NULL)`
        : `(length(tool_input) > ${maxChars} OR length(tool_result) > ${maxChars})`;
    const rowsTrimmed = db.prepare(`SELECT COUNT(*) AS c FROM tool_calls WHERE ${over}`)
        .get().c;
    log(`${rowsTrimmed} tool_calls row(s) over ${maxChars} chars`);
    if (opts.dryRun) {
        db.close();
        return { rowsTrimmed, bytesBefore, bytesAfter: bytesBefore, backupPath: null };
    }
    db.pragma('wal_checkpoint(TRUNCATE)');
    let backupPath = null;
    if (opts.keepBackup) {
        // A full copy, taken before the UPDATE: the trim below rewrites the
        // original in place, so anything linked to it would be trimmed too.
        backupPath = `${dbPath}.pre-compact`;
        log(`backing up to ${backupPath}`);
        fs.copyFileSync(dbPath, backupPath);
    }
    if (rowsTrimmed > 0) {
        const cut = (col) => maxChars === 0 ? 'NULL' : `substr(${col}, 1, ${maxChars})`;
        db.prepare(`UPDATE tool_calls SET tool_input = ${cut('tool_input')},
                                      tool_result = ${cut('tool_result')}
                WHERE ${over}`).run();
    }
    db.pragma('wal_checkpoint(TRUNCATE)');
    const target = `${dbPath}.compact`;
    fs.rmSync(target, { force: true });
    log(`rebuilding into ${target}`);
    db.prepare('VACUUM INTO ?').run(target);
    db.close();
    // With no other connection, closing the last one deletes the -wal. If it
    // is still there, someone else has the database open.
    if (fs.existsSync(`${dbPath}-wal`)) {
        fs.rmSync(target, { force: true });
        throw new Error(`${dbPath} is still open elsewhere (its -wal remains); stop the other ` +
            `process and run compact again. Trimmed rows are committed; nothing was swapped.`);
    }
    // rename() replaces atomically, so the database path never goes missing:
    // a reader opening it mid-swap would otherwise create an empty database.
    fs.renameSync(target, dbPath);
    const bytesAfter = dbBytes(dbPath);
    log(`${(bytesBefore / 1e9).toFixed(2)} GB -> ${(bytesAfter / 1e9).toFixed(2)} GB`);
    return { rowsTrimmed, bytesBefore, bytesAfter, backupPath };
}
/** Where compaction's temp files must go: next to the database, not /tmp. */
export function compactTempDir(dbPath) {
    return path.dirname(path.resolve(dbPath));
}
