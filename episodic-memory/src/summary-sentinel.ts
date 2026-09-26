import * as fs from 'fs';

/**
 * Sentinel file format for `<archive>/<project>/<session>-summary.txt`:
 *
 * - File missing → conversation has not been processed; queue for summarization.
 * - File empty → permanent skip (zero-exchange / metadata-only file; #91).
 * - File starts with `__ERRORED__\n` → previous summarization failed.
 *   Skip-then-retry: if mtime is older than the retry threshold, the file is
 *   re-queued; otherwise it's treated as "recently failed, leave alone".
 * - Anything else → real summary content.
 *
 * The error-marker path is what fixes #96: previously a failed summarization
 * wrote no sentinel at all, so the file re-queued every sync run forever and
 * could pin the head of the queue.
 */
export const ERROR_MARKER = '__ERRORED__';
const ERROR_MARKER_PREFIX = `${ERROR_MARKER}\n`;
const DEFAULT_RETRY_MS = 3600_000; // 1 hour

export function formatErrorSentinel(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return `${ERROR_MARKER}\n${new Date().toISOString()}\n${message}\n`;
}

export function isErroredSentinel(content: string): boolean {
  return content.startsWith(ERROR_MARKER_PREFIX);
}

function getErrorRetryMs(): number {
  const raw = process.env.EPISODIC_MEMORY_SUMMARY_ERROR_RETRY_HOURS;
  if (!raw) return DEFAULT_RETRY_MS;
  const hours = parseFloat(raw);
  return Number.isFinite(hours) && hours > 0 ? hours * 3600_000 : DEFAULT_RETRY_MS;
}

/**
 * True when the sentinel at `summaryPath` represents a real summary —
 * a non-empty file that is not an error marker. Empty zero-exchange
 * sentinels and error sentinels both return false. Use this for callers
 * that care about "is this conversation summarized and useful" (stats,
 * verify, search).
 */
export function hasRealSummary(summaryPath: string): boolean {
  if (!fs.existsSync(summaryPath)) return false;
  let content: string;
  try {
    content = fs.readFileSync(summaryPath, 'utf-8');
  } catch {
    return false;
  }
  if (content.length === 0) return false;
  if (isErroredSentinel(content)) return false;
  return true;
}

/**
 * True when the conversation at `summaryPath` should be (re-)summarized:
 * no sentinel yet, or a stale error marker.
 */
export function shouldQueueForSummary(summaryPath: string): boolean {
  if (!fs.existsSync(summaryPath)) return true;
  let content: string;
  try {
    content = fs.readFileSync(summaryPath, 'utf-8');
  } catch {
    return false;
  }
  if (!isErroredSentinel(content)) return false;
  try {
    const stat = fs.statSync(summaryPath);
    return Date.now() - stat.mtimeMs >= getErrorRetryMs();
  } catch {
    return false;
  }
}
