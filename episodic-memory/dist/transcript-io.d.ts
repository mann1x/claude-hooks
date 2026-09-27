import fs from 'fs';
import { Readable } from 'stream';
export declare const ZST_SUFFIX = ".zst";
export declare const DEFAULT_COMPRESSION_LEVEL = 3;
/** The file that actually holds `canonical`'s content, or null. */
export declare function resolveTranscript(canonical: string): string | null;
export declare function transcriptExists(canonical: string): boolean;
export declare function isCompressed(canonical: string): boolean;
/** stat of whichever form is on disk (its mtime is the transcript's). */
export declare function statTranscript(canonical: string): fs.Stats | null;
/** A byte stream of the transcript's (decompressed) content. */
export declare function openTranscriptStream(canonical: string): Readable;
/** The whole transcript as a string (for show / read, which format all of it). */
export declare function readTranscript(canonical: string): Promise<string>;
/**
 * Drop a stale compressed copy after a fresh `.jsonl` was written: the
 * plain file wins on read anyway, and the `.zst` would only waste space.
 */
export declare function removeCompressedCopy(canonical: string): void;
/**
 * Replace `<canonical>` with `<canonical>.zst`, byte-verified: the
 * compressed file is decompressed and hashed against the original before
 * the original is removed. Keeps the original's mtime. Returns the bytes
 * before and after.
 */
export declare function compressTranscript(canonical: string, level?: number): Promise<{
    before: number;
    after: number;
}>;
