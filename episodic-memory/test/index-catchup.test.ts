import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';

// No model loads; sync imports the backend lazily, which vi.mock intercepts.
const initSpy = vi.fn(async () => {});
const embedSpy = vi.fn(async () => new Array(384).fill(0.1));
vi.mock('../src/embeddings.js', () => ({
  initEmbeddings: initSpy,
  generateExchangeEmbedding: embedSpy,
  generateQueryEmbedding: vi.fn(),
  generateEmbedding: vi.fn(),
}));

import { scanMarkers, shouldSkipConversation, syncConversations } from '../src/sync.js';
import { countPending, listPending, seedCatchUp } from '../src/index-pending.js';
import { initDatabase } from '../src/db.js';
import { compressTranscript } from '../src/transcript-io.js';
import { SUMMARIZER_CONTEXT_MARKER } from '../src/constants.js';

const DO_NOT_INDEX = '<INSTRUCTIONS-TO-EPISODIC-MEMORY>DO NOT INDEX THIS CHAT</INSTRUCTIONS-TO-EPISODIC-MEMORY>';
const rec = (o: object) => JSON.stringify(o);
let seq = 0;
const user = (sid: string, content: unknown) => rec({
  type: 'user', uuid: `u${++seq}`, parentUuid: null, sessionId: sid, isSidechain: false, entrypoint: 'cli',
  timestamp: new Date(Date.UTC(2026, 10, 1, 0, seq)).toISOString(), message: { role: 'user', content } });
const assistant = (sid: string, text: string) => rec({
  type: 'assistant', uuid: `a${++seq}`, parentUuid: `u${seq - 1}`, sessionId: sid, isSidechain: false, entrypoint: 'cli',
  timestamp: new Date(Date.UTC(2026, 10, 1, 0, seq)).toISOString(), message: { role: 'assistant', content: [{ type: 'text', text }] } });
const opening = (sid: string) => [
  rec({ type: 'permission-mode', permissionMode: 'default', sessionId: sid }),
  rec({ type: 'file-history-snapshot', messageId: 'm', snapshot: {} }),
];
const turns = (sid: string, n: number, topic = 'parser') => Array.from({ length: n }, (_, i) =>
  [user(sid, `question ${i} about the ${topic}`), assistant(sid, `answer ${i}: the ${topic} needed a fix in step ${i}.`)]).flat();

describe('index catch-up: markers, pending files, budget', () => {
  let dir: string;
  let src: string;
  let archive: string;
  const saved = { db: process.env.TEST_DB_PATH, batch: process.env.EPISODIC_MEMORY_INDEX_BATCH };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'em-catchup-'));
    src = join(dir, 'source');
    archive = join(dir, 'archive');
    mkdirSync(join(src, 'proj'), { recursive: true });
    process.env.TEST_DB_PATH = join(dir, 'index.sqlite');
    delete process.env.EPISODIC_MEMORY_INDEX_BATCH;
    initSpy.mockReset();
    initSpy.mockImplementation(async () => {});
    embedSpy.mockClear();
  });

  afterEach(() => {
    for (const [k, v] of [['TEST_DB_PATH', saved.db], ['EPISODIC_MEMORY_INDEX_BATCH', saved.batch]] as const) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    rmSync(dir, { recursive: true, force: true });
  });

  function session(sid: string, lines: string[]): string {
    const p = join(src, 'proj', `${sid}.jsonl`);
    writeFileSync(p, lines.join('\n') + '\n', 'utf-8');
    return join(archive, 'proj', `${sid}.jsonl`);
  }
  const indexedLines = (archivePath: string): number[] => {
    const db = new Database(process.env.TEST_DB_PATH!, { readonly: true });
    try {
      return (db.prepare(`SELECT line_start AS l FROM exchanges WHERE archive_path = ? ORDER BY 1`)
        .all(archivePath) as Array<{ l: number }>).map((r) => r.l);
    } finally {
      db.close();
    }
  };
  const pending = (): string[] => {
    const db = initDatabase();
    try { return listPending(db); } finally { db.close(); }
  };

  it('a marker quoted in a tool result or by the assistant excludes nothing', async () => {
    const sid = '11111111-1111-4111-8111-111111111111';
    const quoted = `const EXCLUSION_MARKERS = ['${DO_NOT_INDEX}', '${SUMMARIZER_CONTEXT_MARKER}'];`;
    const a = session(sid, [...opening(sid),
      user(sid, 'read sync.ts'),
      rec({ type: 'assistant', uuid: 'tc', parentUuid: null, sessionId: sid, entrypoint: 'cli', timestamp: '2026-11-01T00:30:00.000Z',
            message: { role: 'assistant', content: [{ type: 'tool_use', id: 't1', name: 'Read', input: { file_path: 'sync.ts' } }] } }),
      rec({ type: 'user', uuid: 'tr', parentUuid: 'tc', sessionId: sid, entrypoint: 'cli', timestamp: '2026-11-01T00:31:00.000Z',
            message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: quoted }] } }),
      assistant(sid, `The marker ${DO_NOT_INDEX} keeps a chat out.`),
      ...turns(sid, 2)]);
    expect(scanMarkers(join(src, 'proj', `${sid}.jsonl`), [DO_NOT_INDEX, SUMMARIZER_CONTEXT_MARKER])).toBe(false);
    expect(shouldSkipConversation(join(src, 'proj', `${sid}.jsonl`))).toBe(false);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a).length).toBeGreaterThanOrEqual(3);
  });

  it('DO NOT INDEX in a user turn keeps the conversation out; inside a reminder it does not', async () => {
    const out = '22222222-2222-4222-8222-222222222222';
    const reminded = '33333333-3333-4333-8333-333333333333';
    const a = session(out, [...opening(out), user(out, `private notes ${DO_NOT_INDEX}`), assistant(out, 'ok, noted.'), ...turns(out, 1)]);
    const b = session(reminded, [...opening(reminded),
      user(reminded, [{ type: 'text', text: `<system-reminder>docs: ${DO_NOT_INDEX}</system-reminder>\nfix the parser` }]),
      assistant(reminded, 'fixed the parser'), ...turns(reminded, 1)]);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toEqual([]);
    expect(indexedLines(b).length).toBe(2);
    expect(pending()).toEqual([]);
  });

  it('a session opening with a summarizer turn keeps the work after it', async () => {
    const sid = '44444444-4444-4444-8444-444444444444';
    const a = session(sid, [
      rec({ type: 'queue-operation', operation: 'dequeue', sessionId: sid }),
      user(sid, [{ type: 'text', text: `${SUMMARIZER_CONTEXT_MARKER}.\n\nPlease write a concise, factual summary.` }]),
      assistant(sid, '<summary>Cleaned up notes.</summary>'),
      ...turns(sid, 3)]);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toHaveLength(3);
    expect(indexedLines(a)[0]).toBeGreaterThan(3); // the summarizer exchange is not among them
    // The summary queue still treats it as the summarizer's (recursion guard, #87).
    expect(shouldSkipConversation(join(src, 'proj', `${sid}.jsonl`))).toBe(true);
  });

  it('files copied while the embedder is down are indexed by a later sync', async () => {
    const sid = '55555555-5555-4555-8555-555555555555';
    const a = session(sid, [...opening(sid), ...turns(sid, 2)]);
    initSpy.mockImplementationOnce(async () => { throw new Error('model cache unwritable'); });
    const first = await syncConversations(src, archive, { skipSummaries: true });
    expect(first.copied).toBe(1);
    expect(indexedLines(a)).toEqual([]);
    expect(pending()).toEqual([a]);

    // Nothing changed at the source: nothing is copied, the owed file is still indexed.
    const second = await syncConversations(src, archive, { skipSummaries: true });
    expect(second.copied).toBe(0);
    expect(indexedLines(a)).toHaveLength(2);
    expect(pending()).toEqual([]);
  });

  it('a run embeds at most EPISODIC_MEMORY_INDEX_BATCH exchanges and resumes', async () => {
    process.env.EPISODIC_MEMORY_INDEX_BATCH = '2';
    const sid = '66666666-6666-4666-8666-666666666666';
    const a = session(sid, [...opening(sid), ...turns(sid, 5)]);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toHaveLength(2);
    expect(pending()).toEqual([a]);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toHaveLength(4);
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toHaveLength(5);
    expect(pending()).toEqual([]);
    expect(embedSpy).toHaveBeenCalledTimes(5); // nothing embedded twice
  });

  it('the seed lists the archive once, compressed copies by their canonical name', async () => {
    const sid = '77777777-7777-4777-8777-777777777777';
    const other = '88888888-8888-4888-8888-888888888888';
    mkdirSync(join(archive, 'proj'), { recursive: true });
    const a = join(archive, 'proj', `${sid}.jsonl`);
    writeFileSync(a, [...opening(sid), ...turns(sid, 2, 'cache')].join('\n') + '\n');
    await compressTranscript(a);
    rmSync(a, { force: true });
    const b = join(archive, 'proj', `${other}.jsonl`);
    writeFileSync(b, [...opening(other), ...turns(other, 1, 'cache')].join('\n') + '\n');

    const db = initDatabase();
    expect(seedCatchUp(db, archive, () => {})).toBe(2);
    expect(seedCatchUp(db, archive, () => {})).toBe(0); // recorded in index_meta
    expect(countPending(db)).toBe(2);
    db.close();

    await syncConversations(src, archive, { skipSummaries: true }); // nothing to copy; works the list
    expect(existsSync(a)).toBe(false);
    expect(indexedLines(a)).toHaveLength(2);
    expect(indexedLines(b)).toHaveLength(1);
    expect(pending()).toEqual([]);
  });

  it('reads markers in a compressed archive copy', async () => {
    const sid = '99999999-9999-4999-8999-999999999999';
    mkdirSync(join(archive, 'proj'), { recursive: true });
    const a = join(archive, 'proj', `${sid}.jsonl`);
    writeFileSync(a, [...opening(sid), user(sid, `notes ${DO_NOT_INDEX}`), assistant(sid, 'noted, keeping it out.')].join('\n') + '\n');
    await compressTranscript(a);
    rmSync(a, { force: true });
    expect(scanMarkers(a, [DO_NOT_INDEX])).toBe(true);

    const db = initDatabase();
    seedCatchUp(db, archive, () => {});
    db.close();
    await syncConversations(src, archive, { skipSummaries: true });
    expect(indexedLines(a)).toEqual([]);
    expect(pending()).toEqual([]);
  });

  it('a listed file that is gone leaves the list', async () => {
    const db = initDatabase();
    db.exec(`CREATE TABLE IF NOT EXISTS index_pending (archive_path TEXT PRIMARY KEY, reason TEXT NOT NULL, since TEXT NOT NULL)`);
    db.prepare(`INSERT INTO index_pending VALUES (?, 'catch-up', '2026-11-27')`).run(join(archive, 'gone.jsonl'));
    db.close();
    await syncConversations(src, archive, { skipSummaries: true });
    expect(pending()).toEqual([]);
  });
});
