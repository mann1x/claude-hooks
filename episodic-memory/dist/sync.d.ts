export declare function isSummarizerPrompt(userMessage: string): boolean;
/**
 * Whether the transcript carries one of `markers` as an instruction; null on a
 * read error. Only lines holding a marker's bare string are parsed (markers
 * have no newline, so none spans two lines). Reading line by line keeps any
 * size readable: one readFileSync throws ERR_STRING_TOO_LONG above ~512 MB (#152).
 */
export declare function scanMarkers(canonical: string, markers: readonly string[]): boolean | null;
/**
 * Leave the conversation out of summaries: a marker in a user turn, or an
 * unreadable file (fail closed, #152). This is also the summarizer's
 * recursion guard — its own sessions carry SUMMARIZER_CONTEXT_MARKER.
 */
export declare function shouldSkipConversation(filePath: string): boolean;
/** DO NOT INDEX in a user turn: the whole conversation stays out of the index (null: unreadable). */
export declare function shouldSkipIndexing(filePath: string): boolean | null;
export interface SyncResult {
    copied: number;
    skipped: number;
    indexed: number;
    summarized: number;
    errors: Array<{
        file: string;
        error: string;
    }>;
}
export interface SyncOptions {
    skipIndex?: boolean;
    skipSummaries?: boolean;
    summaryLimit?: number;
}
/**
 * Derive sync options from the process environment.
 *
 * `EPISODIC_MEMORY_SKIP_SUMMARIES=1` turns the summarization pass off.
 * Only the exact string '1' enables the switch — unset, '0', 'true',
 * and anything else leave summarization on, so a stray value can't
 * silently disable a feature the user still expects.
 *
 * Why anyone wants this: summaries are display-only. search.ts reads
 * the `-summary.txt` sidecar solely to decorate result output; summary
 * text is never embedded and never searched, so skipping it leaves
 * recall untouched. The summarizer, by contrast, resumes each
 * conversation through the Claude Agent SDK, which spends the user's
 * Claude quota and can stall on a permission prompt.
 */
export declare function buildSyncOptionsFromEnv(env: NodeJS.ProcessEnv): SyncOptions;
export declare function extractSessionIdFromPath(filePath: string): string | null;
export declare function syncConversations(sourceDir: string, destDir: string, options?: SyncOptions): Promise<SyncResult>;
