/**
 * The text an exchange is embedded as.
 *
 * Measured on the owner's index (benchmarks/episodic in claude-hooks): a
 * sixth of all exchanges are harness turns — a task notification, a
 * continued-session summary, Stop hook feedback — whose user side is machine
 * text that matches every query a little, and 13.6% of exchanges never had
 * their answer embedded because the user side alone filled the 2000-character
 * window. So: drop the user side of harness turns, strip system reminders and
 * relayed-message wrappers from real prompts, cap the question at 600
 * characters so the answer always gets the rest of the window, and leave tool
 * names out. An exchange with nothing left worth a vector gets none (it is
 * still found by keyword search).
 *
 * Long answers are cut into overlapping windows, each with its own vector:
 * the work a query looks for is often deep in a long answer (median 561
 * characters, p90 4118, p99 17203), past a single window. On the judged
 * bench, windows lifted vector P@10 from 0.38 to 0.45 and fused P@10 from
 * 0.46 to 0.52 (benchmarks/episodic/README.md).
 */

/** User messages written by the harness, not the person. */
const HARNESS_TURN =
  /^\s*(<task-notification>|This session is being continued|Stop hook feedback|\[SYSTEM NOTIFICATION|<local-command|Caveat:|<command-(name|message)>|Tool loaded\.?\s*$)/;

/** Answers shorter than this, on a harness turn, carry nothing to find. */
export const MIN_ANSWER_CHARS = 40;
/** The question's share of the first window. */
export const QUESTION_CHARS = 600;
/** Later windows repeat a short form of the question... */
export const TAG_CHARS = 200;
/** ...and overlap the window before by this much. */
export const OVERLAP_CHARS = 200;
/** Windows per exchange; the rest of a very long answer is left to keyword search. */
export const MAX_CHUNKS = 8;

/** The vector id of an exchange's k-th window: the exchange id itself for the first. */
export function chunkId(exchangeId: string, k: number): string {
  return k === 0 ? exchangeId : `${exchangeId}#${k}`;
}

/** Every vector id an exchange can have. */
export function chunkIds(exchangeId: string): string[] {
  return Array.from({ length: MAX_CHUNKS }, (_, k) => chunkId(exchangeId, k));
}

/** The exchange a vector id belongs to. */
export function exchangeOfChunk(vectorId: string): string {
  const i = vectorId.indexOf('#');
  return i < 0 ? vectorId : vectorId.slice(0, i);
}

export function isHarnessTurn(userMessage: string): boolean {
  return HARNESS_TURN.test(userMessage.slice(0, 300));
}

export function cleanUserMessage(userMessage: string): string {
  if (isHarnessTurn(userMessage)) return '';
  return userMessage
    .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, ' ')
    .replace(/<cross-session-message[\s\S]*?<\/cross-session-message>/g, ' ')
    .replace(/This is how Claude Code surfaces messages[^\n]*/g, ' ')
    .replace(/The user sent a new message while you were working:/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function cleanAssistantMessage(assistantMessage: string): string {
  return (assistantMessage || '').replace(/\s+/g, ' ').trim();
}

/**
 * The windows an exchange is embedded as, each at most `maxChars`; none when
 * it has nothing worth a vector. The first carries the question and the start
 * of the answer; the others a short form of the question and the next stretch
 * of the answer.
 */
export function composeExchangeChunks(userMessage: string, assistantMessage: string, maxChars: number): string[] {
  const user = cleanUserMessage(userMessage || '');
  const answer = cleanAssistantMessage(assistantMessage);
  if (!user && answer.length < MIN_ANSWER_CHARS) return [];
  const head = user ? `User: ${user.slice(0, QUESTION_CHARS)}\n\n` : '';
  const tag = user ? `User: ${user.slice(0, TAG_CHARS)}\n\n` : '';
  const label = 'Assistant: ';
  const first = Math.max(maxChars - head.length - label.length, 400);
  const out = [`${head}${label}${answer.slice(0, first)}`.slice(0, maxChars)];
  const step = Math.max(maxChars - tag.length - label.length - OVERLAP_CHARS, 200);
  for (let s = first - OVERLAP_CHARS; s + OVERLAP_CHARS < answer.length && out.length < MAX_CHUNKS; s += step) {
    out.push(`${tag}${label}${answer.slice(s, s + step + OVERLAP_CHARS)}`.slice(0, maxChars));
  }
  return out;
}
