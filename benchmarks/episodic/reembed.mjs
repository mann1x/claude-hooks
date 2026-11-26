#!/usr/bin/env node
// Re-embed a copy of an episodic index with a given embedder, through the
// package's own migration (space switch + grouped re-embedding), without
// `sync` (which would also copy transcripts into the live archive).
//
//   node benchmarks/episodic/reembed.mjs --db <copy.sqlite> \
//        --embedder '{"kind":"ollama","url":"http://127.0.0.1:11434/api/embed","model":"qwen3-embedding:0.6b"}' \
//        [--pkg <dir holding dist/>] [--rows 2000]
//
// --pkg defaults to the vendored episodic-memory; point it at a separate
// build to try unreleased code without changing the CLI on PATH.
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const db = path.resolve(opt('db'));
process.env.EPISODIC_MEMORY_DB_PATH = db;
process.env.EPISODIC_MEMORY_EMBEDDER = opt('embedder');
const pkg = path.resolve(opt('pkg', path.join(here, '../../episodic-memory')));
const rows = Number(opt('rows', '2000'));

const { initDatabase } = await import(path.join(pkg, 'dist/db.js'));
const emb = await import(path.join(pkg, 'dist/embeddings.js'));
const { ensureVectorSpace, storedSpace } = await import(path.join(pkg, 'dist/vector-space.js'));
const { runMigrationBatch, countStale } = await import(path.join(pkg, 'dist/embedding-migration.js'));

const conn = initDatabase();
await emb.generateEmbedding('episodic-memory vector space probe');
ensureVectorSpace(conn);
console.log('space', storedSpace(conn), 'stale', countStale(conn));
const t0 = Date.now();
for (;;) {
  const done = await runMigrationBatch(conn, path.dirname(db), rows, emb.generateExchangeEmbedding,
    { embedMany: emb.generateExchangeEmbeddings, log: () => {} });
  const left = countStale(conn);
  const vectors = conn.prepare('SELECT COUNT(*) AS c FROM vec_exchanges').get().c;
  console.log(`${new Date().toISOString()} +${done} stale ${left} vectors ${vectors} ` +
              `${((Date.now() - t0) / 1000).toFixed(0)} s`);
  if (done === 0 || left === 0) break;
}
conn.close();
