export interface CompressOptions {
    /** Only transcripts not modified for this many days. */
    afterDays: number;
    level?: number;
    dryRun?: boolean;
    /** Stop after this many files (0 = no limit), to bound one run's time. */
    maxFiles?: number;
    now?: number;
    log?: (msg: string) => void;
}
export interface CompressResult {
    files: number;
    bytesBefore: number;
    bytesAfter: number;
    errors: Array<{
        file: string;
        error: string;
    }>;
}
/** EPISODIC_MEMORY_COMPRESS_AFTER_DAYS: 0 or unset = sync never compresses. */
export declare function getCompressAfterDays(env?: NodeJS.ProcessEnv): number;
/**
 * Compress archived transcripts idle for `afterDays` into `<name>.jsonl.zst`
 * (see transcript-io.ts). The caller holds the sync lock, so nothing else
 * is copying into the archive meanwhile. A transcript that grows again is
 * re-copied as plain `.jsonl` by sync, which drops the stale `.zst`.
 */
export declare function compressArchive(archiveDir: string, opts: CompressOptions): Promise<CompressResult>;
