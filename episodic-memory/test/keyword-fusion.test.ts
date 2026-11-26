import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';
import { initDatabase, insertExchange, deleteExchange, FTS_VERSION } from '../src/db.js';
import { buildFtsQuery, fuseByRank, searchConversations } from '../src/search.js';
import { generateQueryEmbedding } from '../src/embeddings.js';
import { compactDatabase } from '../src/compact.js';
import { ConversationExchange } from '../src/types.js';

// The default search fuses vector ranks with BM25 keyword ranks (RRF). On the
// owner's index the old default (vector, then unranked LIKE hits appended
// after them) put a relevant conversation in the top 10 for 32% of queries.
describe('keyword index and fused ranking', () => {
  let testDir: string;
  const originalDbPath = process.env.TEST_DB_PATH;

  beforeEach(() => {
    testDir = mkdtempSync(join(tmpdir(), 'em-keyword-fusion-'));
    process.env.TEST_DB_PATH = join(testDir, 'index.sqlite');
  });

  afterEach(() => {
    if (originalDbPath === undefined) delete process.env.TEST_DB_PATH;
    else process.env.TEST_DB_PATH = originalDbPath;
    rmSync(testDir, { recursive: true, force: true });
  });

  function seed(id: string, userMessage: string, assistantMessage: string,
                embedding: number[] = new Array(384).fill(0.1)): void {
    const archiveDir = join(testDir, 'archive');
    mkdirSync(archiveDir, { recursive: true });
    const archivePath = join(archiveDir, `${id}.jsonl`);
    writeFileSync(archivePath, '{}\n', 'utf-8');
    const db = initDatabase();
    const exchange: ConversationExchange = {
      id, userMessage, assistantMessage, archivePath,
      project: 'p', timestamp: '2026-11-26T20:00:00.000Z', lineStart: 1, lineEnd: 2,
      harness: 'claude', sessionId: `s-${id}`,
    };
    insertExchange(db, exchange, embedding);
    db.close();
  }

  /** FTS5 compares the index with the exchanges rows; throws on a mismatch. */
  const indexIsConsistent = (): boolean => {
    const db = new Database(process.env.TEST_DB_PATH!);
    try {
      db.exec(`INSERT INTO exchanges_fts(exchanges_fts, rank) VALUES ('integrity-check', 1)`);
      return true;
    } catch {
      return false;
    } finally {
      db.close();
    }
  };

  const keywordHits = (q: string): string[] => {
    const db = initDatabase();
    try {
      return (db.prepare(
        `SELECT e.id FROM exchanges_fts f JOIN exchanges e ON e.rowid = f.rowid
         WHERE exchanges_fts MATCH ? ORDER BY bm25(exchanges_fts)`
      ).all(buildFtsQuery(q)) as Array<{ id: string }>).map((r) => r.id);
    } finally {
      db.close();
    }
  };

  it('buildFtsQuery quotes every token and ORs them', () => {
    expect(buildFtsQuery('op-296 KV "spill" OR x')).toBe('"op-296" OR "kv" OR "spill" OR "or" OR "x"');
    expect(buildFtsQuery('  ...  ')).toBeNull();
    expect(buildFtsQuery('native_git_path')).toBe('"native_git_path"');
  });

  it('fuseByRank sums reciprocal ranks and keeps the row with a distance', () => {
    const v = [{ id: 'a', distance: 0.1 }, { id: 'b', distance: 0.2 }];
    const k = [{ id: 'b', distance: null }, { id: 'c', distance: null }];
    const fused = fuseByRank<any>([v, k], 3);
    expect(fused.map((r) => r.id)).toEqual(['b', 'a', 'c']);
    expect(fused[0].distance).toBe(0.2);
  });

  it('the default search lifts a keyword match that vector search ranks last', async () => {
    // Distractors sit exactly on the query's vector, the target opposite it:
    // vector search alone ranks the target 31st of 31.
    const q = await generateQueryEmbedding('op-296 KV spill');
    for (let i = 0; i < 30; i++) seed(`noise-${i}`, `question ${i} about logging`, `answer ${i}`, q);
    seed('target', 'what about the KV-less run', 'op-296 adds the spill to host RAM', q.map((x) => -x));
    const vectorOnly = await searchConversations('op-296 KV spill', { mode: 'vector', limit: 5 });
    expect(vectorOnly.map((r) => r.exchange.id)).not.toContain('target');
    const results = await searchConversations('op-296 KV spill', { limit: 5 });
    expect(results[0].exchange.id).toBe('target');
    // A keyword-only hit carries no made-up similarity.
    expect(results[0].similarity === undefined || Number.isFinite(results[0].similarity)).toBe(true);
  });

  it('--text keeps its exact all-terms semantics', async () => {
    seed('partial', 'op-296 notes', 'nothing about the rest');
    const results = await searchConversations('op-296 spill', { mode: 'text', limit: 5 });
    expect(results.map((r) => r.exchange.id)).not.toContain('partial');
  });

  it('the index follows inserts, replacements and deletes', () => {
    seed('x', 'alpha question', 'bravo answer');
    expect(keywordHits('bravo')).toEqual(['x']);
    // insertExchange is INSERT OR REPLACE: the old text must leave the index.
    seed('x', 'alpha question', 'charlie answer');
    expect(keywordHits('bravo')).toEqual([]);
    expect(keywordHits('charlie')).toEqual(['x']);
    // A join back to exchanges hides a stale entry; FTS5's own check does not.
    expect(indexIsConsistent()).toBe(true);
    const db = initDatabase();
    deleteExchange(db, 'x');
    db.close();
    expect(keywordHits('charlie')).toEqual([]);
    expect(indexIsConsistent()).toBe(true);
  });

  it('an existing database gets its index built once, on open', () => {
    seed('old', 'delta question', 'echo answer');
    const raw = new Database(process.env.TEST_DB_PATH!);
    raw.exec(`DELETE FROM index_meta; INSERT INTO exchanges_fts(exchanges_fts) VALUES ('delete-all')`);
    raw.close();
    expect(keywordHits('echo')).toEqual(['old']);
    const check = new Database(process.env.TEST_DB_PATH!, { readonly: true });
    expect((check.prepare(`SELECT value FROM index_meta WHERE key='fts_version'`).get() as any).value)
      .toBe(FTS_VERSION);
    check.close();
  });

  it('compact leaves a consistent index', () => {
    for (let i = 0; i < 20; i++) seed(`r-${i}`, `row ${i}`, i === 7 ? 'foxtrot marker' : `filler ${i}`);
    const db = initDatabase();
    for (let i = 0; i < 20; i += 2) deleteExchange(db, `r-${i}`);
    db.close();
    compactDatabase(process.env.TEST_DB_PATH!, { maxChars: 0, keepBackup: false, log: () => {} });
    expect(keywordHits('foxtrot')).toEqual(['r-7']);
    expect(indexIsConsistent()).toBe(true);
  });
});
