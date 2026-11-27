import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';

// No model loads; sync imports both lazily, which vi.mock intercepts.
const embedSpy = vi.fn(async () => new Array(384).fill(0.1));
vi.mock('../src/embeddings.js', () => ({
  initEmbeddings: vi.fn(async () => {}),
  generateExchangeEmbedding: embedSpy,
  generateQueryEmbedding: vi.fn(),
  generateEmbedding: vi.fn(),
}));
const summarizeSpy = vi.fn(async () => 'a summary');
vi.mock('../src/summarizer.js', () => ({
  summarizeConversation: summarizeSpy,
  isAuthFailure: () => false,
}));

import { syncConversations } from '../src/sync.js';

const rec = (o: object) => JSON.stringify(o);
const user = (sid: string, text: string, entrypoint: string) => rec({
  type: 'user', uuid: `u-${sid}`, parentUuid: null, sessionId: sid, isSidechain: false, entrypoint,
  timestamp: '2026-11-27T00:00:00.000Z', message: { role: 'user', content: text } });
const assistant = (sid: string, text: string, entrypoint: string) => rec({
  type: 'assistant', uuid: `a-${sid}`, parentUuid: `u-${sid}`, sessionId: sid, isSidechain: false, entrypoint,
  timestamp: '2026-11-27T00:00:01.000Z', message: { role: 'assistant', content: [{ type: 'text', text }] } });

describe('sync leaves spawned sessions out', () => {
  let dir: string;
  const originalDb = process.env.TEST_DB_PATH;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'em-sync-spawned-'));
    process.env.TEST_DB_PATH = join(dir, 'index.sqlite');
    delete process.env.EPISODIC_MEMORY_INDEX_SPAWNED;
    embedSpy.mockClear();
    summarizeSpy.mockClear();
  });

  afterEach(() => {
    if (originalDb === undefined) delete process.env.TEST_DB_PATH;
    else process.env.TEST_DB_PATH = originalDb;
    rmSync(dir, { recursive: true, force: true });
  });

  it('copies a claude -p transcript but neither indexes nor summarizes it', async () => {
    const src = join(dir, 'source', 'proj');
    mkdirSync(src, { recursive: true });
    const spawnedId = '11111111-1111-4111-8111-111111111111';
    const realId = '22222222-2222-4222-8222-222222222222';
    writeFileSync(join(src, `${spawnedId}.jsonl`), [
      rec({ type: 'queue-operation', operation: 'enqueue', sessionId: spawnedId, content: 'extract lessons' }),
      user(spawnedId, 'You are an expert developer experience engineer', 'cli'),
      assistant(spawnedId, '[]', 'cli'),
    ].join('\n') + '\n');
    writeFileSync(join(src, `${realId}.jsonl`), [
      rec({ type: 'permission-mode', permissionMode: 'default', sessionId: realId }),
      rec({ type: 'file-history-snapshot', messageId: 'm', snapshot: {} }),
      user(realId, 'why does the parser drop tool results', 'cli'),
      assistant(realId, 'Because the tool_result block is matched by the wrong id.', 'cli'),
    ].join('\n') + '\n');

    const result = await syncConversations(join(dir, 'source'), join(dir, 'archive'), {});
    expect(result.copied).toBe(2);
    expect(result.indexed).toBe(1);
    expect(summarizeSpy).toHaveBeenCalledTimes(1);

    const db = new Database(process.env.TEST_DB_PATH!, { readonly: true });
    const paths = (db.prepare(`SELECT DISTINCT archive_path AS p FROM exchanges`).all() as Array<{ p: string }>)
      .map((r) => r.p);
    db.close();
    expect(paths).toEqual([join(dir, 'archive', 'proj', `${realId}.jsonl`)]);
  });
});
