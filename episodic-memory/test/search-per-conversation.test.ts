import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { initDatabase, insertExchange } from '../src/db.js';
import { bestPerConversation, searchConversations } from '../src/search.js';
import { ConversationExchange } from '../src/types.js';

// A long session used to fill several of the top 10 with neighbouring
// exchanges of one piece of work; results are now one per conversation.
describe('one result per conversation', () => {
  let dir: string;
  const originalDb = process.env.TEST_DB_PATH;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'em-per-conv-'));
    process.env.TEST_DB_PATH = join(dir, 'index.sqlite');
  });

  afterEach(() => {
    if (originalDb === undefined) delete process.env.TEST_DB_PATH;
    else process.env.TEST_DB_PATH = originalDb;
    rmSync(dir, { recursive: true, force: true });
  });

  function seed(conversation: string, n: number, text: string): void {
    mkdirSync(join(dir, 'archive'), { recursive: true });
    const archivePath = join(dir, 'archive', `${conversation}.jsonl`);
    writeFileSync(archivePath, '{}\n', 'utf-8');
    const db = initDatabase();
    for (let i = 0; i < n; i++) {
      const ex: ConversationExchange = {
        id: `${conversation}${i}`.padEnd(32, '0'), userMessage: `${text} (step ${i})`,
        assistantMessage: `worked on ${text}`, archivePath, project: 'p',
        timestamp: `2026-11-2${i}T10:00:00.000Z`, lineStart: i * 2 + 1, lineEnd: i * 2 + 2,
      };
      insertExchange(db, ex, new Array(384).fill(0.1));
    }
    db.close();
  }

  it('keeps the first (best-ranked) row of each conversation', () => {
    const rows = [{ id: 'x1', archive_path: 'a' }, { id: 'x2', archive_path: 'b' },
                  { id: 'x3', archive_path: 'a' }, { id: 'x4', archive_path: 'c' }];
    expect(bestPerConversation(rows).map((r) => r.id)).toEqual(['x1', 'x2', 'x4']);
  });

  it('keyword and fused search return each conversation once', async () => {
    seed('aaaa', 4, 'kv spill eviction');
    seed('bbbb', 1, 'kv spill eviction');
    for (const mode of ['text', 'both'] as const) {
      const results = await searchConversations('kv spill eviction', { mode, limit: 10 });
      const paths = results.map((r) => r.exchange.archivePath);
      expect(new Set(paths).size).toBe(paths.length);
      expect(paths).toHaveLength(2);
    }
  });

  it('fills the limit with other conversations rather than repeats', async () => {
    seed('aaaa', 6, 'kv spill eviction');
    seed('bbbb', 1, 'kv spill eviction');
    seed('cccc', 1, 'kv spill eviction');
    const results = await searchConversations('kv spill eviction', { mode: 'text', limit: 3 });
    expect(results.map((r) => r.exchange.archivePath.slice(-10))).toEqual(
      expect.arrayContaining(['aaaa.jsonl', 'bbbb.jsonl', 'cccc.jsonl']));
  });

  it('per_conversation: false returns every exchange', async () => {
    seed('aaaa', 3, 'kv spill eviction');
    const results = await searchConversations('kv spill eviction',
      { mode: 'text', limit: 10, per_conversation: false });
    expect(results).toHaveLength(3);
  });
});
