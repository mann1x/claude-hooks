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
/** Answers shorter than this, on a harness turn, carry nothing to find. */
export declare const MIN_ANSWER_CHARS = 40;
/** The question's share of the first window. */
export declare const QUESTION_CHARS = 600;
/** Later windows repeat a short form of the question... */
export declare const TAG_CHARS = 200;
/** ...and overlap the window before by this much. */
export declare const OVERLAP_CHARS = 200;
/** Windows per exchange; the rest of a very long answer is left to keyword search. */
export declare const MAX_CHUNKS = 8;
/** The vector id of an exchange's k-th window: the exchange id itself for the first. */
export declare function chunkId(exchangeId: string, k: number): string;
/** Every vector id an exchange can have. */
export declare function chunkIds(exchangeId: string): string[];
/** The exchange a vector id belongs to. */
export declare function exchangeOfChunk(vectorId: string): string;
export declare function isHarnessTurn(userMessage: string): boolean;
export declare function cleanUserMessage(userMessage: string): string;
export declare function cleanAssistantMessage(assistantMessage: string): string;
/**
 * The windows an exchange is embedded as, each at most `maxChars`; none when
 * it has nothing worth a vector. The first carries the question and the start
 * of the answer; the others a short form of the question and the next stretch
 * of the answer.
 */
export declare function composeExchangeChunks(userMessage: string, assistantMessage: string, maxChars: number): string[];
