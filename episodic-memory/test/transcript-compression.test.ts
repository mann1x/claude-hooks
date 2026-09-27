import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync,
  rmSync, statSync, utimesSync, writeFileSync,
} from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

vi.mock('../src/embeddings.js', () => ({
  initEmbeddings: vi.fn(async () => {}),
  generateExchangeEmbedding: vi.fn(async () => new Array(384).fill(0)),
  generateQueryEmbedding: vi.fn(),
  generateEmbedding: vi.fn(),
  initEmbeddingsFailed: false,
}));

import {
  compressTranscript, isCompressed, readTranscript, resolveTranscript,
  statTranscript, transcriptExists, ZST_SUFFIX,
} from '../src/transcript-io.js';
import { compressArchive, getCompressAfterDays } from '../src/compress-archive.js';
import { findJsonlFiles } from '../src/paths.js';
import { parseConversation } from '../src/parser.js';
import { syncConversations } from '../src/sync.js';

const DAY = 86_400_000;

function line(type: 'user' | 'assistant', seq: number, text: string): string {
  const content = type === 'user' ? text : [{ type: 'text', text }];
  return JSON.stringify({
    type,
    uuid: `${type}-${seq}`,
    parentUuid: seq === 1 && type === 'user' ? null : `prev-${seq}`,
    sessionId: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    isSidechain: false,
    timestamp: new Date(2026, 0, seq).toISOString(),
    message: { role: type, content },
  });
}

/** A real (if small) Claude transcript: n user/assistant exchanges. */
function transcript(n: number): string {
  let out = '';
  for (let i = 1; i <= n; i++) {
    out += line('user', i, `question ${i} about bcache superblocks`) + '\n';
    out += line('assistant', i, `answer ${i}: rebuild with make-bcache --wipe-bcache`) + '\n';
  }
  return out;
}

function writeTranscript(p: string, n = 20): void {
  writeFileSync(p, transcript(n));
}

function setMtime(p: string, ms: number): void {
  utimesSync(p, ms / 1000, ms / 1000);
}

describe('transcript compression', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'em-zst-test-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('round-trips, removes the original and keeps its mtime', async () => {
    const p = join(dir, 'a.jsonl');
    writeTranscript(p);
    const old = Date.now() - 30 * DAY;
    setMtime(p, old);
    const original = readFileSync(p, 'utf-8');

    const { before, after } = await compressTranscript(p);
    expect(after).toBeLessThan(before);
    expect(existsSync(p)).toBe(false);
    expect(existsSync(p + ZST_SUFFIX)).toBe(true);
    expect(Math.abs(statSync(p + ZST_SUFFIX).mtimeMs - old)).toBeLessThan(1000);

    expect(transcriptExists(p)).toBe(true);
    expect(isCompressed(p)).toBe(true);
    expect(resolveTranscript(p)).toBe(p + ZST_SUFFIX);
    expect(statTranscript(p)?.mtimeMs).toBe(statSync(p + ZST_SUFFIX).mtimeMs);
    expect(await readTranscript(p)).toBe(original);
  });

  it('a plain file wins over a compressed one, and nothing resolves when both are gone', () => {
    const p = join(dir, 'b.jsonl');
    writeFileSync(p, 'plain\n');
    writeFileSync(p + ZST_SUFFIX, 'stale');
    expect(resolveTranscript(p)).toBe(p);
    expect(isCompressed(p)).toBe(false);
    expect(resolveTranscript(join(dir, 'none.jsonl'))).toBeNull();
  });

  it('parses a compressed transcript exactly like the plain one', async () => {
    const plain = join(dir, 'plain.jsonl');
    const packed = join(dir, 'packed.jsonl');
    writeTranscript(plain);
    writeTranscript(packed);
    await compressTranscript(packed);
    const a = await parseConversation(plain, 'p', plain);
    const b = await parseConversation(packed, 'p', packed);
    expect(a.length).toBeGreaterThan(0);
    const strip = (xs: any[]) => xs.map(({ id, archivePath, ...rest }) => rest);
    expect(strip(b)).toEqual(strip(a));
    expect(b[0].archivePath).toBe(packed); // canonical name, not .zst
  });

  it('findJsonlFiles reports a compressed copy under its .jsonl name, once', () => {
    writeFileSync(join(dir, 'x.jsonl.zst'), '');
    writeFileSync(join(dir, 'y.jsonl'), '');
    writeFileSync(join(dir, 'y.jsonl.zst'), '');
    writeFileSync(join(dir, 'y-summary.txt'), '');
    expect(findJsonlFiles(dir).sort()).toEqual(['x.jsonl', 'y.jsonl']);
  });

  it('compressArchive takes only idle transcripts, and a dry run changes nothing', async () => {
    const proj = join(dir, 'proj');
    mkdirSync(proj);
    const idle = join(proj, 'idle.jsonl');
    const fresh = join(proj, 'fresh.jsonl');
    writeTranscript(idle);
    writeTranscript(fresh);
    writeFileSync(join(proj, 'idle-summary.txt'), 'summary');
    setMtime(idle, Date.now() - 10 * DAY);

    const dry = await compressArchive(dir, { afterDays: 7, dryRun: true });
    expect(dry.files).toBe(1);
    expect(existsSync(idle)).toBe(true);

    const r = await compressArchive(dir, { afterDays: 7 });
    expect(r.files).toBe(1);
    expect(r.errors).toEqual([]);
    expect(isCompressed(idle)).toBe(true);
    expect(existsSync(fresh)).toBe(true);
    expect(existsSync(join(proj, 'idle-summary.txt'))).toBe(true);
  });

  it('the sync threshold is opt-in', () => {
    expect(getCompressAfterDays({})).toBe(0);
    expect(getCompressAfterDays({ EPISODIC_MEMORY_COMPRESS_AFTER_DAYS: '7' })).toBe(7);
    expect(getCompressAfterDays({ EPISODIC_MEMORY_COMPRESS_AFTER_DAYS: 'x' })).toBe(0);
  });
});

describe('sync with a compressed archive', () => {
  let root: string;
  let src: string;
  let dest: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'em-zst-sync-'));
    src = join(root, 'projects');
    dest = join(root, 'archive');
    mkdirSync(join(src, 'proj'), { recursive: true });
    process.env.TEST_DB_PATH = join(root, 'db.sqlite');
  });

  afterEach(() => {
    delete process.env.TEST_DB_PATH;
    rmSync(root, { recursive: true, force: true });
  });

  const opts = { skipIndex: true, skipSummaries: true };

  it('does not re-copy a compressed copy whose source is unchanged', async () => {
    const s = join(src, 'proj', 'sess.jsonl');
    writeTranscript(s);
    setMtime(s, Date.now() - 10 * DAY);
    await syncConversations(src, dest, opts);
    const archived = join(dest, 'proj', 'sess.jsonl');
    await compressTranscript(archived);

    const r = await syncConversations(src, dest, opts);
    expect(r.copied).toBe(0);
    expect(isCompressed(archived)).toBe(true);
  });

  it('a resumed session is re-copied plain and its stale .zst dropped', async () => {
    const s = join(src, 'proj', 'sess.jsonl');
    writeTranscript(s);
    setMtime(s, Date.now() - 10 * DAY);
    await syncConversations(src, dest, opts);
    const archived = join(dest, 'proj', 'sess.jsonl');
    await compressTranscript(archived);

    appendFileSync(s, line('user', 99, 'resumed later') + '\n');
    const r = await syncConversations(src, dest, opts);
    expect(r.copied).toBe(1);
    expect(existsSync(archived)).toBe(true);
    expect(existsSync(archived + ZST_SUFFIX)).toBe(false);
    expect(readFileSync(archived, 'utf-8')).toBe(readFileSync(s, 'utf-8'));
  });
});
