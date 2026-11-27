import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';
import { initDatabase, insertExchange } from '../src/db.js';
import { compressTranscript } from '../src/transcript-io.js';
import {
  excludeAsSpawned,
  isSpawnedSession,
  parentSessionOf,
  purgeSpawnedSessions,
} from '../src/spawned-session.js';
import { ConversationExchange } from '../src/types.js';

// Record shapes as Claude Code writes them (archive survey, 2026-11-27).
const enqueue = (prompt: string) =>
  ({ type: 'queue-operation', operation: 'enqueue', sessionId: 's', timestamp: '2026-04-09T17:08:42.628Z', content: prompt });
const dequeue = { type: 'queue-operation', operation: 'dequeue', sessionId: 's', timestamp: '2026-04-09T17:08:42.700Z' };
const snapshot = { type: 'file-history-snapshot', messageId: 'm1', snapshot: {} };
const permission = { type: 'permission-mode', permissionMode: 'default', sessionId: 's' };
const user = (text: string, entrypoint?: string) =>
  ({ type: 'user', uuid: 'u1', parentUuid: null, sessionId: 's', isSidechain: false, userType: 'external',
     timestamp: '2026-04-09T17:08:43.000Z', message: { role: 'user', content: text }, ...(entrypoint ? { entrypoint } : {}) });
const assistant = (text: string, entrypoint?: string) =>
  ({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', sessionId: 's', isSidechain: false,
     timestamp: '2026-04-09T17:08:44.000Z', message: { role: 'assistant', content: [{ type: 'text', text }] },
     ...(entrypoint ? { entrypoint } : {}) });

describe('spawned sessions (claude -p / Agent SDK)', () => {
  let dir: string;
  const original = { db: process.env.TEST_DB_PATH, opt: process.env.EPISODIC_MEMORY_INDEX_SPAWNED };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'em-spawned-'));
    process.env.TEST_DB_PATH = join(dir, 'index.sqlite');
    delete process.env.EPISODIC_MEMORY_INDEX_SPAWNED;
  });

  afterEach(() => {
    for (const [k, v] of [['TEST_DB_PATH', original.db], ['EPISODIC_MEMORY_INDEX_SPAWNED', original.opt]] as const) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    rmSync(dir, { recursive: true, force: true });
  });

  function transcript(rel: string, records: object[]): string {
    const p = join(dir, rel);
    mkdirSync(join(p, '..'), { recursive: true });
    writeFileSync(p, records.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf-8');
    return p;
  }

  it('an sdk-* entrypoint is spawned', async () => {
    const p = transcript('proj/a.jsonl', [enqueue('You are an expert at detecting languages'),
      user('You are an expert at detecting languages', 'sdk-cli'), assistant('python', 'sdk-cli')]);
    expect(await isSpawnedSession(p)).toBe(true);
    const ts = transcript('proj/b.jsonl', [user('hi', 'sdk-ts'), assistant('hello', 'sdk-ts')]);
    expect(await isSpawnedSession(ts)).toBe(true);
  });

  it('an older claude -p run (entrypoint cli) opens with its queued prompt and has no snapshot', async () => {
    const p = transcript('proj/c.jsonl', [enqueue('You are an expert developer experience engineer'), dequeue,
      user('You are an expert developer experience engineer', 'cli'), assistant('[]', 'cli')]);
    expect(await isSpawnedSession(p)).toBe(true);
  });

  it('interactive sessions are not spawned', async () => {
    const plain = transcript('proj/d.jsonl', [permission, snapshot, user('fix the bug', 'cli'), assistant('done', 'cli')]);
    expect(await isSpawnedSession(plain)).toBe(false);
    // A prompt typed while the session was starting is queued first, but the session snapshots.
    const typedEarly = transcript('proj/e.jsonl', [enqueue('fix the bug'), dequeue, snapshot,
      user('fix the bug', 'cli'), assistant('done', 'cli')]);
    expect(await isSpawnedSession(typedEarly)).toBe(false);
    // Transcripts from before entrypoint existed.
    const old = transcript('proj/f.jsonl', [user('fix the bug'), assistant('done')]);
    expect(await isSpawnedSession(old)).toBe(false);
    expect(await isSpawnedSession(join(dir, 'proj/missing.jsonl'))).toBe(false);
  });

  it('a subagent follows its parent session', async () => {
    transcript('proj/p1.jsonl', [enqueue('weekly report'), user('weekly report', 'cli'), assistant('ok', 'cli')]);
    const spawnedChild = transcript('proj/p1/subagents/workflows/wf_1/agent-a1.jsonl',
      [user('read the buglog', 'cli'), assistant('3 bugs', 'cli')]);
    expect(parentSessionOf(spawnedChild)).toBe(join(dir, 'proj/p1.jsonl'));
    expect(await isSpawnedSession(spawnedChild)).toBe(true);

    transcript('proj/p2.jsonl', [permission, snapshot, user('audit this', 'cli'), assistant('ok', 'cli')]);
    const interactiveChild = transcript('proj/p2/subagents/agent-a2.jsonl',
      [user('read the buglog', 'cli'), assistant('3 bugs', 'cli')]);
    expect(await isSpawnedSession(interactiveChild)).toBe(false);
  });

  it('reads a compressed archive copy', async () => {
    const p = transcript('proj/g.jsonl', [user('hi', 'sdk-cli'), assistant('hello', 'sdk-cli')]);
    await compressTranscript(p);
    rmSync(p, { force: true });
    expect(existsSync(p + '.zst')).toBe(true);
    expect(await isSpawnedSession(p)).toBe(true);
  });

  it('EPISODIC_MEMORY_INDEX_SPAWNED=1 indexes them again', async () => {
    const p = transcript('proj/h.jsonl', [user('hi', 'sdk-cli'), assistant('hello', 'sdk-cli')]);
    expect(await excludeAsSpawned(p)).toBe(true);
    process.env.EPISODIC_MEMORY_INDEX_SPAWNED = '1';
    expect(await excludeAsSpawned(p)).toBe(false);
  });

  it('purges spawned sessions from an existing index once', async () => {
    const spawned = transcript('proj/s.jsonl', [enqueue('extract lessons'), user('extract lessons', 'cli'), assistant('[]', 'cli')]);
    const real = transcript('proj/r.jsonl', [permission, snapshot, user('fix the bug', 'cli'), assistant('done', 'cli')]);
    const db = initDatabase();
    const seed = (id: string, archivePath: string, text: string) => {
      const ex: ConversationExchange = { id, userMessage: text, assistantMessage: 'answer text here', archivePath,
        project: 'proj', timestamp: '2026-04-09T17:08:43.000Z', lineStart: 1, lineEnd: 2 };
      insertExchange(db, ex, new Array(384).fill(0.1));
    };
    seed('a'.repeat(32), spawned, 'extract lessons from tool events');
    seed('b'.repeat(32), spawned, 'extract more lessons');
    seed('c'.repeat(32), real, 'fix the parser bug');

    const logs: string[] = [];
    expect(await purgeSpawnedSessions(db, (m) => logs.push(m))).toBe(2);
    expect(logs[0]).toMatch(/removed 2 exchange\(s\) of 1 spawned session/);
    const left = db.prepare(`SELECT archive_path AS p FROM exchanges`).all() as Array<{ p: string }>;
    expect(left.map((r) => r.p)).toEqual([real]);
    expect((db.prepare(`SELECT COUNT(*) AS c FROM vec_exchanges`).get() as { c: number }).c).toBe(1);
    expect(existsSync(spawned)).toBe(true); // the transcript stays archived

    // Recorded: a later run does not scan again.
    seed('d'.repeat(32), spawned, 'added after the purge');
    expect(await purgeSpawnedSessions(db)).toBe(0);
    db.close();

    // The keyword index followed the deletes.
    const check = new Database(process.env.TEST_DB_PATH!);
    expect(() => check.exec(`INSERT INTO exchanges_fts(exchanges_fts, rank) VALUES ('integrity-check', 1)`)).not.toThrow();
    check.close();
  });
});
