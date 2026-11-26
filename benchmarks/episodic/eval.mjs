#!/usr/bin/env node
// Episodic search eval: rank of the first relevant conversation per query.
//
//   node benchmarks/episodic/eval.mjs [--db <db.sqlite>] [--modes vector,both]
//                                     [--limit 10] [--json out.json]
//
// A result is relevant when its conversation (archive_path) has an assistant
// message containing the query's `marker` (and its project contains
// `project`, when set). Relevance is computed from the database searched, so
// a variant index is judged against its own rows. Uses the package's own
// searchConversations(), i.e. exactly what the server runs.
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = path.resolve(here, '../../episodic-memory');

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const db = opt('db');
if (db) process.env.EPISODIC_MEMORY_DB_PATH = path.resolve(db);
const modes = opt('modes', 'vector,both').split(',');
const limit = Number(opt('limit', '10'));
const out = opt('json');
const queriesFile = opt('queries', path.join(here, 'queries.json'));

const { searchConversations } = await import(path.join(pkg, 'dist/search.js'));
const { initDatabase } = await import(path.join(pkg, 'dist/db.js'));
const { queries } = JSON.parse(fs.readFileSync(queriesFile, 'utf8'));

const conn = initDatabase();
const relevantFor = (q) => new Set(
  conn.prepare(
    `SELECT DISTINCT archive_path FROM exchanges
     WHERE assistant_message LIKE ? ${q.project ? 'AND project LIKE ?' : ''}`
  ).all(`%${q.marker}%`, ...(q.project ? [`%${q.project}%`] : []))
    .map((r) => r.archive_path)
);
const rel = queries.map(relevantFor);
conn.close();

const report = {};
for (const mode of modes) {
  const rows = [];
  const t0 = Date.now();
  for (let i = 0; i < queries.length; i++) {
    const q = queries[i];
    const results = await searchConversations(q.q, { limit, mode });
    const rank = results.findIndex((r) => rel[i].has(r.exchange.archivePath)) + 1;
    rows.push({ q: q.q, kind: q.kind, relevant: rel[i].size, rank,
                ids: results.slice(0, limit).map((r) => r.exchange.id),
                top: results.slice(0, 3).map((r) => r.exchange.userMessage.slice(0, 60).replace(/\s+/g, ' ')) });
  }
  const ms = Date.now() - t0;
  const summarize = (sel) => {
    const n = sel.length || 1;
    const hit = (k) => sel.filter((r) => r.rank > 0 && r.rank <= k).length / n;
    return { n: sel.length, hit1: hit(1), hit5: hit(5), hit10: hit(limit),
             mrr: sel.reduce((s, r) => s + (r.rank ? 1 / r.rank : 0), 0) / n };
  };
  report[mode] = {
    all: summarize(rows),
    semantic: summarize(rows.filter((r) => r.kind === 'semantic')),
    keyword: summarize(rows.filter((r) => r.kind === 'keyword')),
    ms_per_query: Math.round(ms / queries.length),
    rows,
  };
  const f = (x) => x.toFixed(2);
  for (const k of ['all', 'semantic', 'keyword']) {
    const s = report[mode][k];
    console.log(`${mode.padEnd(7)} ${k.padEnd(8)} n=${String(s.n).padStart(2)}  hit@1 ${f(s.hit1)}  hit@5 ${f(s.hit5)}  hit@${limit} ${f(s.hit10)}  MRR ${f(s.mrr)}`);
  }
  console.log(`${mode.padEnd(7)} ${report[mode].ms_per_query} ms/query`);
}
if (out) fs.writeFileSync(out, JSON.stringify(report, null, 2));
