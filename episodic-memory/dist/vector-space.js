import { LOCAL_DIMS, LOCAL_MODEL } from './embedder-config.js';
import { MAX_CHUNKS, chunkId, chunkIds } from './exchange-text.js';
export const LEGACY_SPACE = { id: LOCAL_MODEL, dims: LOCAL_DIMS };
/** The width vec_exchanges was declared with, or null when it has none. */
export function vecTableDims(db) {
    const row = db.prepare(`SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vec_exchanges'`).get();
    const m = row?.sql.match(/FLOAT\s*\[\s*(\d+)\s*\]/i);
    return m ? Number(m[1]) : null;
}
export function storedSpace(db) {
    let id = LEGACY_SPACE.id;
    try {
        const row = db.prepare(`SELECT value FROM index_meta WHERE key = 'embedder'`).get();
        if (row?.value)
            id = row.value;
    }
    catch {
        // No index_meta table: an index this code never opened.
    }
    return { id, dims: vecTableDims(db) ?? LEGACY_SPACE.dims };
}
export function sameSpace(a, b) {
    return a.id === b.id && a.dims === b.dims;
}
/**
 * Recreate vec_exchanges for `to` and mark every exchange for re-embedding.
 * One transaction: a reader sees the old space or the new one, never a mix.
 */
export function switchVectorSpace(db, to) {
    if (!Number.isInteger(to.dims) || to.dims <= 0 || to.dims > 65536) {
        throw new Error(`refusing a vector space of ${to.dims} dimensions`);
    }
    const from = storedSpace(db);
    db.transaction(() => {
        db.exec('DROP TABLE IF EXISTS vec_exchanges');
        db.exec(`CREATE VIRTUAL TABLE vec_exchanges USING vec0(
      id TEXT PRIMARY KEY,
      embedding FLOAT[${to.dims}]
    )`);
        db.prepare('UPDATE exchanges SET embedding_version = 0').run();
        db.exec('CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
        db.prepare(`INSERT OR REPLACE INTO index_meta (key, value) VALUES ('embedder', ?)`).run(to.id);
    })();
    console.error(`episodic-memory: embedder changed from ${from.id} (${from.dims} dims) to ${to.id} ` +
        `(${to.dims} dims); every exchange will be re-embedded`);
}
const verified = new WeakMap();
let produced = null;
/** Set by embeddings.ts when its embedder produces a vector. */
export function noteProducedSpace(space) {
    produced = space;
}
/** The space this process's embedder writes into, or null before it has run. */
export function producedSpace() {
    return produced;
}
/**
 * Make the index hold `space` (default: the space this process's embedder
 * produced) before a vector from it is written. `null` — no embedder has run
 * in this process, e.g. a caller storing vectors it made itself — checks
 * nothing. Cached per connection.
 */
export function ensureVectorSpace(db, space = produced) {
    if (!space)
        return;
    const key = `${space.id}#${space.dims}`;
    if (verified.get(db) === key)
        return;
    if (!sameSpace(storedSpace(db), space))
        switchVectorSpace(db, space);
    verified.set(db, key);
}
function asList(v) {
    if (!v || v.length === 0)
        return [];
    return typeof v[0] === 'number' ? [v] : v;
}
/** Remove every vector of an exchange, every window included. */
export function deleteExchangeVectors(db, exchangeId) {
    const del = db.prepare('DELETE FROM vec_exchanges WHERE id = ?');
    for (const id of chunkIds(exchangeId))
        del.run(id);
}
/**
 * Replace an exchange's vectors. A vector from another model's space switches
 * the index first (and marks every exchange for re-embedding).
 */
export function writeExchangeVectors(db, exchangeId, vectors) {
    const list = asList(vectors).slice(0, MAX_CHUNKS);
    if (list.length > 0)
        ensureVectorSpace(db);
    deleteExchangeVectors(db, exchangeId);
    const ins = db.prepare('INSERT INTO vec_exchanges (id, embedding) VALUES (?, ?)');
    list.forEach((v, k) => ins.run(chunkId(exchangeId, k), Buffer.from(new Float32Array(v).buffer)));
}
