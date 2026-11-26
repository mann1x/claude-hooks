import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import http from 'http';
import { AddressInfo } from 'net';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';
import { deleteExchange, initDatabase, insertExchange } from '../src/db.js';
import {
  EmbeddingsUnavailableError,
  embedTexts,
  generateExchangeEmbedding,
  generateExchangeEmbeddings,
  getEmbedderConfig,
  resetEmbedder,
} from '../src/embeddings.js';
import {
  EmbedderConfigError,
  QWEN3_QUERY_PREFIX,
  getEmbedderConfigPath,
  resolveEmbedderConfig,
} from '../src/embedder-config.js';
import { MAX_CHUNKS, composeExchangeChunks } from '../src/exchange-text.js';
import { EMBEDDING_VERSION, countStale, runMigrationBatch } from '../src/embedding-migration.js';
import { searchConversations } from '../src/search.js';
import { storedSpace } from '../src/vector-space.js';
import { ConversationExchange } from '../src/types.js';

const DIMS = 8;

/** Deterministic, non-zero vector per text: a bag of character codes. */
function fakeVector(text: string): number[] {
  const v = new Array(DIMS).fill(0.01);
  for (let i = 0; i < text.length; i++) v[text.charCodeAt(i) % DIMS] += 1;
  return v;
}

interface FakeServer { url: string; seen: string[][]; close: () => Promise<void> }

/** An embedding server speaking llamafile's or Ollama's wire format. */
async function fakeServer(kind: 'llamafile' | 'ollama', dims = DIMS): Promise<FakeServer> {
  const seen: string[][] = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const j = JSON.parse(body);
      const texts: string[] = kind === 'llamafile' ? j.content : j.input;
      seen.push(texts);
      const vec = (t: string) => fakeVector(t).concat(new Array(Math.max(0, dims - DIMS)).fill(0)).slice(0, dims);
      let out: unknown;
      if (kind === 'llamafile') {
        // Reversed on purpose: callers must order by `index`.
        out = texts.map((t, index) => ({ index, embedding: [vec(t)] })).reverse();
      } else {
        out = { embeddings: texts.map(vec) };
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(out));
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const port = (server.address() as AddressInfo).port;
  const path = kind === 'llamafile' ? '/embedding' : '/api/embed';
  return {
    url: `http://127.0.0.1:${port}${path}`,
    seen,
    close: () => new Promise((r) => server.close(() => r())),
  };
}

const useEmbedder = (cfg: object | 'local') => {
  process.env.EPISODIC_MEMORY_EMBEDDER = cfg === 'local' ? 'local' : JSON.stringify(cfg);
  resetEmbedder();
};

describe('configured embedder', () => {
  let testDir: string;
  const servers: FakeServer[] = [];

  beforeEach(() => {
    testDir = mkdtempSync(join(tmpdir(), 'em-configured-embedder-'));
    process.env.TEST_DB_PATH = join(testDir, 'index.sqlite');
    process.env.EPISODIC_MEMORY_CONFIG_DIR = join(testDir, 'superpowers');
  });

  afterEach(async () => {
    delete process.env.EPISODIC_MEMORY_EMBEDDER;
    delete process.env.TEST_DB_PATH;
    resetEmbedder();
    while (servers.length) await servers.pop()!.close();
    rmSync(testDir, { recursive: true, force: true });
  });

  const serve = async (kind: 'llamafile' | 'ollama', dims = DIMS) => {
    const s = await fakeServer(kind, dims);
    servers.push(s);
    return s;
  };

  function seed(db: Database.Database, id: string, user: string, answer: string, vec: number[] | null): void {
    const archiveDir = join(testDir, 'archive');
    mkdirSync(archiveDir, { recursive: true });
    const archivePath = join(archiveDir, `${id}.jsonl`);
    writeFileSync(archivePath, '{}\n', 'utf-8');
    const exchange: ConversationExchange = {
      id, userMessage: user, assistantMessage: answer, archivePath,
      project: 'p', timestamp: '2026-11-26T20:00:00.000Z', lineStart: 1, lineEnd: 2,
      harness: 'claude', sessionId: `s-${id}`,
    };
    insertExchange(db, exchange, vec);
  }

  it('defaults to the bundled model, and reads embedder.json in the superpowers dir', () => {
    expect(resolveEmbedderConfig().kind).toBe('local');
    mkdirSync(join(testDir, 'superpowers'), { recursive: true });
    writeFileSync(getEmbedderConfigPath(), JSON.stringify({
      kind: 'llamafile', url: 'http://127.0.0.1:1/embedding', model: 'qwen3-embedding:0.6b',
    }));
    const cfg = resolveEmbedderConfig();
    expect(cfg.kind).toBe('llamafile');
    expect(cfg.queryPrefix).toBe(QWEN3_QUERY_PREFIX);
    expect(cfg.documentPrefix).toBe('');
    // The environment wins over the file.
    process.env.EPISODIC_MEMORY_EMBEDDER = 'local';
    expect(resolveEmbedderConfig().kind).toBe('local');
  });

  it('an unreadable config is an error, never a silent switch to the bundled model', () => {
    mkdirSync(join(testDir, 'superpowers'), { recursive: true });
    writeFileSync(getEmbedderConfigPath(), '{ not json');
    expect(() => resolveEmbedderConfig()).toThrow(EmbedderConfigError);
    resetEmbedder();
    expect(() => getEmbedderConfig()).toThrow(EmbeddingsUnavailableError);
  });

  it('refuses a fallback that serves another model', () => {
    useEmbedder({
      kind: 'ollama', url: 'http://127.0.0.1:1/api/embed', model: 'qwen3-embedding:0.6b',
      fallback: { kind: 'llamafile', url: 'http://127.0.0.1:2/embedding', model: 'nomic-embed-text' },
    });
    expect(() => getEmbedderConfig()).toThrow(/same model|another space|writes/);
  });

  it('embeds through llamafile and Ollama in input order, as unit vectors', async () => {
    for (const kind of ['llamafile', 'ollama'] as const) {
      const s = await serve(kind);
      useEmbedder({ kind, url: s.url, model: 'm' });
      const [a, b] = await embedTexts(['alpha', 'zz']);
      const unit = (v: number[]) => v.map((x) => x / Math.hypot(...v));
      const close = (x: number[], y: number[]) => x.forEach((xi, i) => expect(xi).toBeCloseTo(y[i], 9));
      close(a, unit(fakeVector('alpha')));
      close(b, unit(fakeVector('zz')));
      expect(Math.hypot(...a)).toBeCloseTo(1, 6);
    }
  });

  it('uses a fallback of the same model when the primary is unreachable', async () => {
    const s = await serve('llamafile');
    useEmbedder({
      kind: 'ollama', url: 'http://127.0.0.1:9/api/embed', model: 'm', timeout_ms: 2000,
      fallback: { kind: 'llamafile', url: s.url, model: 'm' },
    });
    const [v] = await embedTexts(['alpha']);
    expect(v).toHaveLength(DIMS);
    expect(s.seen).toHaveLength(1);
  });

  it('exchange text drops harness turns, reminders and tool names', async () => {
    expect(composeExchangeChunks('<task-notification> done </task-notification>', 'ok', 2000)).toEqual([]);
    expect(composeExchangeChunks('Stop hook feedback: x', 'The stop hook said the tests were not run, so I ran them.', 2000))
      .toEqual(['Assistant: The stop hook said the tests were not run, so I ran them.']);
    expect(composeExchangeChunks('fix it <system-reminder>secret context</system-reminder> now', 'done', 2000))
      .toEqual(['User: fix it now\n\nAssistant: done']);
    const s = await serve('ollama');
    useEmbedder({ kind: 'ollama', url: s.url, model: 'm' });
    expect(await generateExchangeEmbedding('<task-notification>x', 'ok', ['Bash'])).toBeNull();
    await generateExchangeEmbedding('why', 'because', ['Bash']);
    expect(s.seen.at(-1)).toEqual(['User: why\n\nAssistant: because']);
  });

  it('a long answer is cut into overlapping windows, each under the cut', () => {
    const answer = Array.from({ length: 900 }, (_, i) => `w${i}`).join(' ');   // ~4.9k chars
    const chunks = composeExchangeChunks('q'.repeat(5000), answer, 2000);
    expect(chunks.length).toBeGreaterThan(2);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(2000);
    // The question never crowds out the answer, and later windows carry a short form of it.
    expect(chunks[0]).toContain('Assistant: w0 w1');
    expect(chunks[1].startsWith(`User: ${'q'.repeat(200)}\n\nAssistant: `)).toBe(true);
    // Consecutive windows overlap, and together they cover the whole answer.
    const body = (c: string) => c.slice(c.indexOf('Assistant: ') + 11);
    expect(body(chunks[0]).endsWith(body(chunks[1]).slice(0, 200))).toBe(true);
    expect(answer.endsWith(body(chunks.at(-1)!))).toBe(true);
    // An answer inside the first window gets one vector, not a copy of its tail.
    expect(composeExchangeChunks('q', 'a'.repeat(1900), 2000)).toHaveLength(1);
    expect(composeExchangeChunks('q', 'a'.repeat(200000), 2000)).toHaveLength(MAX_CHUNKS);
  });

  it('windows are stored per exchange, replaced whole, deleted whole, found once', async () => {
    const s = await serve('llamafile');
    useEmbedder({ kind: 'llamafile', url: s.url, model: 'qwen3-embedding:0.6b' });
    const long = 'hotel '.repeat(1200);
    const db = initDatabase();
    seed(db, 'e1', 'hotel question', long, await generateExchangeEmbedding('hotel question', long));
    const ids = () => (db.prepare('SELECT id FROM vec_exchanges ORDER BY id').all() as any[]).map((r) => r.id);
    expect(ids().length).toBeGreaterThan(2);
    expect(ids()[0]).toBe('e1');
    expect(ids().slice(1).every((id: string) => /^e1#\d$/.test(id))).toBe(true);
    // Re-indexed with a short answer: the old windows go.
    seed(db, 'e1', 'hotel question', 'hotel', await generateExchangeEmbedding('hotel question', 'hotel'));
    expect(ids()).toEqual(['e1']);
    seed(db, 'e1', 'hotel question', long, await generateExchangeEmbedding('hotel question', long));
    db.close();
    // Every window is near the query; the exchange still comes back once.
    const results = await searchConversations('hotel', { mode: 'vector', limit: 5 });
    expect(results.map((r) => r.exchange.id)).toEqual(['e1']);
    const db2 = initDatabase();
    deleteExchange(db2, 'e1');
    expect((db2.prepare('SELECT COUNT(*) AS c FROM vec_exchanges').get() as any).c).toBe(0);
    db2.close();
  });

  it('a vector from another model switches the index: new width, every exchange stale', async () => {
    const db = initDatabase();
    seed(db, 'old', 'alpha question', 'bravo answer', new Array(384).fill(0.1));
    expect(storedSpace(db)).toEqual({ id: 'Xenova/bge-small-en-v1.5', dims: 384 });
    expect(countStale(db)).toBe(0);

    const s = await serve('llamafile');
    useEmbedder({ kind: 'llamafile', url: s.url, model: 'qwen3-embedding:0.6b' });
    const vec = await generateExchangeEmbedding('charlie question', 'delta answer');
    seed(db, 'new', 'charlie question', 'delta answer', vec);

    expect(storedSpace(db)).toEqual({ id: 'qwen3-embedding:0.6b', dims: DIMS });
    // The old row lost its bge vector and waits for the migration; the new one is done.
    expect((db.prepare('SELECT id FROM vec_exchanges').all() as any[]).map((r) => r.id)).toEqual(['new']);
    expect(countStale(db)).toBe(1);

    const done = await runMigrationBatch(db, testDir, 10, generateExchangeEmbedding,
      { embedMany: generateExchangeEmbeddings });
    expect(done).toBe(1);
    expect(countStale(db)).toBe(0);
    expect((db.prepare('SELECT COUNT(*) AS c FROM vec_exchanges').get() as any).c).toBe(2);
    db.close();
  });

  it('search never compares across spaces: keyword results only, until the re-embed', async () => {
    const db = initDatabase();
    seed(db, 'x', 'where is the foxtrot config', 'foxtrot lives in settings', new Array(384).fill(0.1));
    db.close();
    const s = await serve('llamafile');
    useEmbedder({ kind: 'llamafile', url: s.url, model: 'qwen3-embedding:0.6b' });

    const both = await searchConversations('foxtrot', { limit: 5 });
    expect(both.map((r) => r.exchange.id)).toEqual(['x']);
    expect(s.seen).toHaveLength(0); // no query vector was even asked for
    await expect(searchConversations('foxtrot', { mode: 'vector', limit: 5 }))
      .rejects.toBeInstanceOf(EmbeddingsUnavailableError);
  });

  it('an unreachable embedder leaves default search its keyword half', async () => {
    const s = await serve('llamafile');
    useEmbedder({ kind: 'llamafile', url: s.url, model: 'qwen3-embedding:0.6b' });
    const db = initDatabase();
    seed(db, 'x', 'golf question', 'golf answer', await generateExchangeEmbedding('golf question', 'golf answer'));
    db.close();
    useEmbedder({ kind: 'llamafile', url: 'http://127.0.0.1:9/embedding', model: 'qwen3-embedding:0.6b', timeout_ms: 2000 });
    const results = await searchConversations('golf', { limit: 5 });
    expect(results.map((r) => r.exchange.id)).toEqual(['x']);
  });

  it('the migration commits each group, so a rebuild that stops keeps its work', async () => {
    const s = await serve('ollama');
    useEmbedder({ kind: 'ollama', url: s.url, model: 'm' });
    const db = initDatabase();
    for (let i = 0; i < 5; i++) seed(db, `r${i}`, `question ${i}`, `answer ${i}`, null);
    db.prepare('UPDATE exchanges SET embedding_version = 0').run();
    let calls = 0;
    const flaky = async (rows: Array<{ userMessage: string; assistantMessage: string }>) => {
      if (++calls === 2) throw new Error('server went away');
      return generateExchangeEmbeddings(rows);
    };
    await expect(runMigrationBatch(db, testDir, 10, generateExchangeEmbedding, { embedMany: flaky, group: 2 }))
      .rejects.toThrow('server went away');
    expect(countStale(db)).toBe(3);
    expect((db.prepare('SELECT COUNT(*) AS c FROM exchanges WHERE embedding_version = ?')
      .get(EMBEDDING_VERSION) as any).c).toBe(2);
    db.close();
  });
});
