import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { existsSync, mkdtempSync, rmSync, statSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import Database from 'better-sqlite3';
import * as sqliteVec from 'sqlite-vec';
import { initDatabase, insertExchange } from '../src/db.js';
import { compactDatabase, compactTempDir } from '../src/compact.js';
import { capToolText, getToolInputMaxChars } from '../src/tool-input.js';
import type { ConversationExchange } from '../src/types.js';

const BIG = { command: 'x'.repeat(50_000) };

function exchange(id: string, tools: number): ConversationExchange {
  return {
    id,
    project: 'p',
    timestamp: '2026-01-01T00:00:00Z',
    userMessage: 'u',
    assistantMessage: 'a',
    archivePath: '/x.jsonl',
    lineStart: 1,
    lineEnd: 2,
    toolCalls: Array.from({ length: tools }, (_, i) => ({
      id: `${id}-tc${i}`,
      exchangeId: id,
      toolName: 'Bash',
      toolInput: BIG,
      toolResult: 'r'.repeat(10_000),
      isError: false,
      timestamp: '2026-01-01T00:00:00Z',
    })),
  } as ConversationExchange;
}

const EMB = new Array(384).fill(0.01);

function toolRows(dbPath: string) {
  const db = new Database(dbPath, { readonly: true });
  const rows = db.prepare('SELECT tool_name, tool_input, tool_result FROM tool_calls').all() as
    Array<{ tool_name: string; tool_input: string | null; tool_result: string | null }>;
  db.close();
  return rows;
}

describe('tool input cap', () => {
  it('defaults to storing nothing', () => {
    expect(getToolInputMaxChars({})).toBe(0);
    expect(capToolText(BIG, 0)).toBeNull();
  });

  it('keeps a prefix when configured, and rejects nonsense', () => {
    expect(getToolInputMaxChars({ EPISODIC_MEMORY_TOOL_INPUT_CHARS: '12' })).toBe(12);
    expect(getToolInputMaxChars({ EPISODIC_MEMORY_TOOL_INPUT_CHARS: '-3' })).toBe(0);
    expect(getToolInputMaxChars({ EPISODIC_MEMORY_TOOL_INPUT_CHARS: 'lots' })).toBe(0);
    expect(capToolText({ a: 1 }, 100)).toBe('{"a":1}');
    expect(capToolText('abcdef', 3)).toBe('abc');
    expect(capToolText(undefined, 100)).toBeNull();
  });
});

describe('compact', () => {
  let testDir: string;
  let dbPath: string;

  beforeEach(() => {
    testDir = mkdtempSync(join(tmpdir(), 'em-compact-test-'));
    dbPath = join(testDir, 'db.sqlite');
    process.env.TEST_DB_PATH = dbPath;
  });

  afterEach(() => {
    delete process.env.TEST_DB_PATH;
    delete process.env.EPISODIC_MEMORY_TOOL_INPUT_CHARS;
    rmSync(testDir, { recursive: true, force: true });
  });

  function legacyDb(): void {
    // What an older version left behind: full inputs stored.
    process.env.EPISODIC_MEMORY_TOOL_INPUT_CHARS = '1000000';
    const db = initDatabase();
    for (let i = 0; i < 20; i++) insertExchange(db, exchange(`ex${i}`, 5), EMB);
    db.close();
    delete process.env.EPISODIC_MEMORY_TOOL_INPUT_CHARS;
  }

  it('new inserts store no tool input by default, but keep the tool name', () => {
    const db = initDatabase();
    insertExchange(db, exchange('ex', 2), EMB);
    db.close();
    const rows = toolRows(dbPath);
    expect(rows).toHaveLength(2);
    expect(rows.every(r => r.tool_name === 'Bash' && r.tool_input === null && r.tool_result === null)).toBe(true);
  });

  it('drops stored inputs, shrinks the file, keeps every row and vector', () => {
    legacyDb();
    const before = statSync(dbPath).size;
    const res = compactDatabase(dbPath, { maxChars: 0, keepBackup: true });
    expect(res.rowsTrimmed).toBe(100);
    // 100 rows x ~60 KB of input+result went; what remains is mostly
    // sqlite-vec's preallocated chunk (~1.5 MB), a fixed cost.
    expect(before - statSync(dbPath).size).toBeGreaterThan(5_000_000);
    expect(res.backupPath && existsSync(res.backupPath)).toBeTruthy();
    // The backup is the database as it was, not the trimmed original.
    expect(toolRows(res.backupPath!).every(r => r.tool_input !== null)).toBe(true);
    expect(existsSync(`${dbPath}.compact`)).toBe(false);

    const rows = toolRows(dbPath);
    expect(rows).toHaveLength(100);
    expect(rows.every(r => r.tool_input === null)).toBe(true);

    const db = new Database(dbPath, { readonly: true });
    sqliteVec.load(db);
    expect((db.prepare('SELECT COUNT(*) AS c FROM exchanges').get() as { c: number }).c).toBe(20);
    expect((db.prepare('SELECT COUNT(*) AS c FROM vec_exchanges').get() as { c: number }).c).toBe(20);
    db.close();
    // And the regular code path still opens it.
    initDatabase().close();
  });

  it('keeps a prefix when a cap is set', () => {
    legacyDb();
    compactDatabase(dbPath, { maxChars: 7 });
    const rows = toolRows(dbPath);
    expect(rows.every(r => r.tool_input === '{"comma' && r.tool_result === 'rrrrrrr')).toBe(true);
  });

  it('dry run changes nothing', () => {
    legacyDb();
    const before = statSync(dbPath).size;
    const res = compactDatabase(dbPath, { maxChars: 0, dryRun: true });
    expect(res.rowsTrimmed).toBe(100);
    expect(statSync(dbPath).size).toBe(before);
    expect(toolRows(dbPath).every(r => r.tool_input !== null)).toBe(true);
  });

  it('refuses to swap while another connection holds the database open', () => {
    legacyDb();
    const other = initDatabase();
    other.prepare('SELECT COUNT(*) FROM exchanges').get();
    expect(() => compactDatabase(dbPath, { maxChars: 0 })).toThrow(/still open elsewhere/);
    expect(existsSync(`${dbPath}.compact`)).toBe(false);
    other.close();
  });

  it('puts temp files next to the database', () => {
    expect(compactTempDir(dbPath)).toBe(testDir);
  });
});
