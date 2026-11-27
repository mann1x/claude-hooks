import type Database from 'better-sqlite3';
/**
 * Spawned Claude Code runs — `claude -p` and Agent SDK sessions: Caliber's
 * refresh and learning extraction, OpenWolf's reports, skill generators,
 * workflow agents — are prompts a tool wrote, not work the user did. They are
 * not indexed (nor summarized), and searches stop surfacing them. On one
 * index they were 5,193 conversations / 5,280 of 71,807 exchanges.
 *
 * Signals, from the first HEAD_RECORDS records:
 *  - an `entrypoint` starting `sdk-` (`sdk-cli` is `claude -p`; `sdk-ts`,
 *    `sdk-py` the SDKs). Current Claude Code writes it on every message.
 *  - Older versions wrote `entrypoint: "cli"` for `claude -p` as well, but such
 *    a run's file opens with its queued prompt (`queue-operation` / `enqueue`)
 *    and never has a `file-history-snapshot`. An interactive session opens
 *    with something else and snapshots at its first prompt.
 * A subagent transcript (`<session>/subagents/...`) follows its parent.
 * Other harnesses' transcripts never match; an unreadable file is not spawned.
 *
 * `EPISODIC_MEMORY_INDEX_SPAWNED=1` indexes them again.
 */
export declare const HEAD_RECORDS = 64;
/** Bump to re-run the purge of an existing index after a rule change. */
export declare const SPAWNED_FILTER_VERSION = "1";
export declare function indexSpawnedSessions(): boolean;
/** True when the transcript should stay out of the index (spawned, and not opted back in). */
export declare function excludeAsSpawned(canonical: string): Promise<boolean>;
export declare function isSpawnedSession(canonical: string): Promise<boolean>;
/** `<dir>/<session>/subagents/.../agent-x.jsonl` → `<dir>/<session>.jsonl`. */
export declare function parentSessionOf(p: string): string | null;
/**
 * Remove spawned sessions already in the index, once per SPAWNED_FILTER_VERSION
 * (recorded in `index_meta`). The archived transcripts are kept, so
 * `EPISODIC_MEMORY_INDEX_SPAWNED=1` plus a reindex brings them back.
 * Returns the number of exchanges removed.
 */
export declare function purgeSpawnedSessions(db: Database.Database, log?: (msg: string) => void): Promise<number>;
