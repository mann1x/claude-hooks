#!/usr/bin/env node
// Episodic search variants: build a subset index with a given text
// composition and embedding model, then score vector / BM25 / fused ranking
// on benchmarks/episodic/queries.json.
//
//   node variant.mjs build --src <db.sqlite> --out <variant.sqlite>
//        --model Xenova/bge-small-en-v1.5 [--dtype q8] [--pooling mean|cls]
//        [--compose raw|clean|chunk] [--max-chars 2000] [--distractors 15000]
//        [--doc-prefix ''] [--query-prefix '...'] [--batch 16]
//        [--embed-url http://127.0.0.1:11434/api/embed]   (Ollama; else local ONNX)
//   node variant.mjs eval --db <variant.sqlite> [--json out.json]
//        [--query-url http://127.0.0.1:38092/embedding]  (queries from another
//        server of the same model: llamafile /embedding or Ollama /api/embed)
//
// The subset holds every exchange of every conversation the queries count as
// relevant, plus a seeded random sample of the rest (natural proportions, so
// harness turns stay in as distractors). Scores are therefore higher than on
// the full index, but comparable between variants built on the same subset.
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { createRequire } from 'module';

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = path.resolve(here, '../../episodic-memory');
const require = createRequire(path.join(pkg, 'package.json'));
const Database = require('better-sqlite3');
const sqliteVec = require('sqlite-vec');
const { pipeline, env } = await import(path.join(pkg, 'node_modules/@huggingface/transformers/dist/transformers.node.mjs'));
env.cacheDir = process.env.EPISODIC_MEMORY_MODEL_CACHE || path.join(process.env.HOME, '.config/superpowers/models');

// An embedder: local ONNX through transformers.js, or Ollama's /api/embed
// (the same model the claude-hooks embedder serves). Both return unit vectors.
async function embedder({ model, dtype, pooling, url }) {
  if (url && /\/embedding\/?$/.test(url)) {
    // llama.cpp / llamafile server: {"content": [...]}, one result per text.
    return async (texts) => {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: texts }) });
      if (!r.ok) throw new Error(`${url}: HTTP ${r.status} ${await r.text()}`);
      return (await r.json()).sort((a, b) => (a.index ?? 0) - (b.index ?? 0)).map(({ embedding: e }) => {
        const v = Array.isArray(e[0]) ? e[0] : e;
        const n = Math.hypot(...v) || 1;
        return Float32Array.from(v, (x) => x / n);
      });
    };
  }
  if (url) {
    return async (texts) => {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, input: texts, truncate: true }) });
      if (!r.ok) throw new Error(`${url}: HTTP ${r.status} ${await r.text()}`);
      return (await r.json()).embeddings.map((v) => {
        const n = Math.hypot(...v) || 1;
        return Float32Array.from(v, (x) => x / n);
      });
    };
  }
  const pipe = await pipeline('feature-extraction', model, { dtype });
  return async (texts) => {
    const out = await pipe(texts, { pooling, normalize: true });
    const d = out.dims.at(-1);
    return texts.map((_, i) => Float32Array.from(out.data.subarray(i * d, (i + 1) * d)));
  };
}

const [cmd, ...rest] = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = rest.indexOf(`--${name}`);
  return i >= 0 ? rest[i + 1] : dflt;
};
const { queries } = JSON.parse(fs.readFileSync(opt('queries', path.join(here, 'queries.json')), 'utf8'));

// ---------------------------------------------------------------- text ---
// Harness turns: the user side is machine text, the assistant side may still
// carry the finding, so the user side is dropped and the answer kept.
const HARNESS = /^\s*(<task-notification>|This session is being continued|Stop hook feedback|\[SYSTEM NOTIFICATION|<local-command|Caveat:|<command-(name|message)>|Tool loaded\.?\s*$)/;
const cleanUser = (u) => {
  if (HARNESS.test(u.slice(0, 300))) return '';
  return u
    .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, ' ')
    .replace(/<cross-session-message[\s\S]*?<\/cross-session-message>/g, ' ')
    .replace(/This is how Claude Code surfaces messages[^\n]*/g, ' ')
    .replace(/The user sent a new message while you were working:/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
};
const cleanAssistant = (a) => (a || '').replace(/\s+/g, ' ').trim();

function compose(kind, u, a, maxChars) {
  if (kind === 'raw') return [`User: ${u}\n\nAssistant: ${a}`.slice(0, maxChars)];
  const cu = cleanUser(u);
  const ca = cleanAssistant(a);
  if (!cu && ca.length < 40) return [];
  const head = cu ? `User: ${cu.slice(0, 600)}\n\n` : '';
  if (kind === 'clean') return [`${head}Assistant: ${ca}`.slice(0, maxChars)];
  // chunk: the first window carries the question, later windows a short form
  // of it; windows overlap by 200 characters; at most 8 per exchange.
  const out = [];
  const tag = cu ? `User: ${cu.slice(0, 200)}\n\n` : '';
  const first = maxChars - head.length - 11;
  out.push(`${head}Assistant: ${ca.slice(0, Math.max(first, 400))}`);
  const step = maxChars - tag.length - 11 - 200;
  for (let s = Math.max(first, 400) - 200; s < ca.length && out.length < 8; s += step) {
    out.push(`${tag}Assistant: ${ca.slice(s, s + step + 200)}`);
  }
  return out;
}
const ftsText = (u, a) => `${cleanUser(u)} ${cleanAssistant(a)}`;

// --------------------------------------------------------------- build ---
async function build() {
  const src = new Database(opt('src'), { readonly: true });
  const outPath = opt('out');
  const model = opt('model', 'Xenova/bge-small-en-v1.5');
  const dtype = opt('dtype', 'q8');
  const pooling = opt('pooling', 'mean');
  const kind = opt('compose', 'clean');
  const maxChars = Number(opt('max-chars', '2000'));
  const docPrefix = opt('doc-prefix', '');
  const queryPrefix = opt('query-prefix', 'Represent this sentence for searching relevant passages: ');
  const batch = Number(opt('batch', '16'));
  const nDistract = Number(opt('distractors', '15000'));

  const relevant = new Set();
  for (const q of queries) {
    for (const r of src.prepare(
      `SELECT DISTINCT archive_path FROM exchanges WHERE assistant_message LIKE ? ${q.project ? 'AND project LIKE ?' : ''}`
    ).all(`%${q.marker}%`, ...(q.project ? [`%${q.project}%`] : []))) relevant.add(r.archive_path);
  }
  const all = src.prepare('SELECT id, archive_path, project, timestamp, user_message u, assistant_message a FROM exchanges').all();
  // Seeded shuffle (mulberry32) so every variant gets the same subset.
  let s = 7;
  const rnd = () => { s |= 0; s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const others = all.filter((r) => !relevant.has(r.archive_path));
  for (let i = others.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [others[i], others[j]] = [others[j], others[i]]; }
  const rows = all.filter((r) => relevant.has(r.archive_path)).concat(others.slice(0, nDistract));

  fs.rmSync(outPath, { force: true });
  const db = new Database(outPath);
  sqliteVec.load(db);
  db.exec(`CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
           CREATE TABLE exchanges (n INTEGER PRIMARY KEY, id TEXT, archive_path TEXT, project TEXT, timestamp TEXT, user_message TEXT, assistant_message TEXT);
           CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, n INTEGER);
           CREATE VIRTUAL TABLE fts USING fts5(text, tokenize = 'unicode61 remove_diacritics 2');`);
  const url = opt('embed-url', '');
  const meta = { model, dtype, pooling, url, kind, maxChars, docPrefix, queryPrefix, rows: rows.length, relevantConvs: relevant.size };
  const insMeta = db.prepare('INSERT INTO meta VALUES (?, ?)');
  for (const [k, v] of Object.entries(meta)) insMeta.run(k, String(v));

  const embed = await embedder({ model, dtype, pooling, url });
  const t0 = Date.now();
  let dims = 0, nChunks = 0, vecReady = false;
  const insEx = db.prepare('INSERT INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)');
  const insFts = db.prepare('INSERT INTO fts (rowid, text) VALUES (?, ?)');
  const insChunk = db.prepare('INSERT INTO chunks (rowid, n) VALUES (?, ?)');
  let pending = [];
  const flush = async () => {
    if (!pending.length) return;
    const vecs = await embed(pending.map((p) => docPrefix + p.text));
    const d = vecs[0].length;
    if (!vecReady) {
      dims = d;
      db.exec(`CREATE VIRTUAL TABLE vec USING vec0(embedding float[${dims}] distance_metric=cosine)`);
      vecReady = true;
    }
    const insVec = db.prepare('INSERT INTO vec (rowid, embedding) VALUES (?, ?)');
    db.transaction(() => {
      pending.forEach((p, i) => {
        nChunks++;
        insChunk.run(nChunks, p.n);
        insVec.run(BigInt(nChunks), Buffer.from(vecs[i].buffer));
      });
    })();
    pending = [];
  };
  for (let n = 0; n < rows.length; n++) {
    const r = rows[n];
    insEx.run(n, r.id, r.archive_path, r.project, r.timestamp, r.u, r.a);
    insFts.run(n, ftsText(r.u, r.a));
    for (const text of compose(kind, r.u, r.a || '', maxChars)) {
      pending.push({ n, text });
      if (pending.length >= batch) await flush();
    }
    if (n % 2000 === 0) process.stderr.write(`  ${n}/${rows.length} exchanges, ${nChunks} vectors, ${((Date.now() - t0) / 1000).toFixed(0)} s\n`);
  }
  await flush();
  insMeta.run('dims', String(dims));
  insMeta.run('vectors', String(nChunks));
  insMeta.run('build_s', String(Math.round((Date.now() - t0) / 1000)));
  db.close();
  console.log(JSON.stringify({ ...meta, dims, vectors: nChunks, build_s: Math.round((Date.now() - t0) / 1000) }));
}

// ---------------------------------------------------------------- eval ---
const ftsQuery = (q) => {
  const terms = q.toLowerCase().match(/[\p{L}\p{N}_]+(?:[-.][\p{L}\p{N}_]+)*/gu) || [];
  return [...new Set(terms)].map((t) => `"${t.replace(/"/g, '""')}"`).join(' OR ');
};

async function evaluate() {
  const db = new Database(opt('db'), { readonly: true });
  sqliteVec.load(db);
  const meta = Object.fromEntries(db.prepare('SELECT k, v FROM meta').all().map((r) => [r.k, r.v]));
  const embed = await embedder({ model: meta.model, dtype: meta.dtype, pooling: meta.pooling, url: opt('query-url', meta.url) });
  const K = 100, limit = 10, RRF_K = 60;
  const exArchive = db.prepare('SELECT archive_path FROM exchanges WHERE n = ?');
  const relevantFor = (q) => new Set(db.prepare(
    `SELECT DISTINCT archive_path FROM exchanges WHERE assistant_message LIKE ? ${q.project ? 'AND project LIKE ?' : ''}`
  ).all(`%${q.marker}%`, ...(q.project ? [`%${q.project}%`] : [])).map((r) => r.archive_path));
  const knn = db.prepare(`SELECT c.n AS n, v.distance AS d FROM vec v JOIN chunks c ON c.rowid = v.rowid
                          WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance`);
  const bm25 = db.prepare('SELECT rowid AS n, bm25(fts) AS s FROM fts WHERE fts MATCH ? ORDER BY s LIMIT ?');

  const methods = { vector: [], bm25: [], rrf: [] };
  for (const q of queries) {
    const rel = relevantFor(q);
    const [qv] = await embed([meta.queryPrefix + q.q]);
    const vecRank = [];
    const seen = new Set();
    for (const r of knn.all(Buffer.from(qv.buffer), K)) {
      if (!seen.has(r.n)) { seen.add(r.n); vecRank.push(r.n); }
    }
    let ftsRank = [];
    const fq = ftsQuery(q.q);
    if (fq) ftsRank = bm25.all(fq, K).map((r) => r.n);
    const fused = new Map();
    vecRank.forEach((n, i) => fused.set(n, (fused.get(n) || 0) + 1 / (RRF_K + i + 1)));
    ftsRank.forEach((n, i) => fused.set(n, (fused.get(n) || 0) + 1 / (RRF_K + i + 1)));
    const rrfRank = [...fused.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n);
    for (const [m, ranked] of [['vector', vecRank], ['bm25', ftsRank], ['rrf', rrfRank]]) {
      const top = ranked.slice(0, limit);
      const rank = top.findIndex((n) => rel.has(exArchive.get(n).archive_path)) + 1;
      methods[m].push({ q: q.q, kind: q.kind, rank,
                        top: top.map((x) => db.prepare('SELECT id FROM exchanges WHERE n = ?').get(x).id) });
    }
  }
  const f = (x) => x.toFixed(2);
  const report = { meta };
  for (const [m, rows] of Object.entries(methods)) {
    report[m] = {};
    for (const k of ['all', 'semantic', 'keyword']) {
      const sel = k === 'all' ? rows : rows.filter((r) => r.kind === k);
      const n = sel.length || 1;
      const hit = (c) => sel.filter((r) => r.rank > 0 && r.rank <= c).length / n;
      const s = { n: sel.length, hit1: hit(1), hit5: hit(5), hit10: hit(10), mrr: sel.reduce((a, r) => a + (r.rank ? 1 / r.rank : 0), 0) / n };
      report[m][k] = s;
      console.log(`${meta.model.split('/').pop()}/${meta.kind} ${m.padEnd(6)} ${k.padEnd(8)} hit@1 ${f(s.hit1)} hit@5 ${f(s.hit5)} hit@10 ${f(s.hit10)} MRR ${f(s.mrr)}`);
    }
    report[m].rows = rows;
  }
  const out = opt('json');
  if (out) fs.writeFileSync(out, JSON.stringify(report, null, 2));
}

if (cmd === 'build') await build();
else if (cmd === 'eval') await evaluate();
else { console.error('usage: variant.mjs build|eval ...'); process.exit(2); }
