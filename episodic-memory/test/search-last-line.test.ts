import { describe, it, expect, vi } from 'vitest';
import Database from 'better-sqlite3';

// Formatting a result used to read the whole transcript to count its lines.
// One result in a 6.4 GB live transcript made every search take ~97 s (and
// blocked the episodic server). The count now comes from the index; opening a
// transcript while formatting is a failure.
vi.mock('../src/transcript-io.js', async (orig) => ({
  ...(await orig<typeof import('../src/transcript-io.js')>()),
  openTranscriptStream: () => {
    throw new Error('formatting must not read the transcript');
  },
  statTranscript: () => ({ size: 1024 }),
}));

import { formatResults, formatMultiConceptResults, lastIndexedLines } from '../src/search.js';

const exchange = (archivePath: string) => ({
  id: 'x1',
  project: 'proj',
  timestamp: '2026-11-26T10:00:00Z',
  userMessage: 'how do I fix it',
  assistantMessage: 'like this',
  archivePath,
  lineStart: 10,
  lineEnd: 20,
}) as any;

describe('result line counts come from the index', () => {
  it('lastIndexedLines takes max(line_end) per transcript in one query', () => {
    const db = new Database(':memory:');
    db.exec('CREATE TABLE exchanges (archive_path TEXT, line_end INTEGER)');
    const ins = db.prepare('INSERT INTO exchanges VALUES (?, ?)');
    ins.run('/a.jsonl', 5);
    ins.run('/a.jsonl', 945924);
    ins.run('/b.jsonl', 12);
    ins.run('/c.jsonl', 99);
    const prepare = vi.spyOn(db, 'prepare');
    const got = lastIndexedLines(db, ['/a.jsonl', '/b.jsonl', '/a.jsonl', '/gone.jsonl']);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(got).toEqual(new Map([['/a.jsonl', 945924], ['/b.jsonl', 12]]));
    expect(lastIndexedLines(db, []).size).toBe(0);
    db.close();
  });

  it('formatResults prints the indexed count without opening the transcript', async () => {
    const out = await formatResults([
      { exchange: exchange('/huge.jsonl'), similarity: 0.8, snippet: 's', lastLine: 945924 },
      { exchange: exchange('/unindexed.jsonl'), similarity: 0.7, snippet: 's' },
    ]);
    expect(out).toContain('Lines 10-20 in /huge.jsonl (1KB, 945924 lines)');
    expect(out).toContain('Lines 10-20 in /unindexed.jsonl (1KB)');
  });

  it('formatMultiConceptResults does the same', async () => {
    const out = await formatMultiConceptResults([
      { exchange: exchange('/huge.jsonl'), snippet: 's', conceptSimilarities: [0.8, 0.6],
        averageSimilarity: 0.7, lastLine: 945924 },
    ], ['a', 'b']);
    expect(out).toContain('(1KB, 945924 lines)');
  });
});
