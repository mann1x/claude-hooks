// Archived transcripts may be stored zstd-compressed as `<name>.jsonl.zst`.
//
// The canonical name stays `<name>.jsonl` everywhere — in the database's
// archive_path, in summary paths (`-summary.txt` is derived from it), in
// sync's copy logic — and only opening a file resolves it to the `.zst`
// sibling. Compression keeps the original mtime, so sync's "is the copy
// current?" check sees the same timestamp whichever form is on disk.
//
// Transcripts are JSON lines, which compress ~6-7x at zstd level 3 (a
// 200 MB sample: 29.7 MB in 0.5 s). Decompression uses Node's built-in
// zlib zstd (Node >= 22.15), so there is no new dependency.
import fs from 'fs';
import zlib from 'zlib';
import crypto from 'crypto';
import { pipeline } from 'stream/promises';
export const ZST_SUFFIX = '.zst';
export const DEFAULT_COMPRESSION_LEVEL = 3;
function hasZstd() {
    return typeof zlib.createZstdDecompress === 'function';
}
function requireZstd(file) {
    if (!hasZstd()) {
        throw new Error(`${file} is zstd-compressed and this Node (${process.version}) has no zstd; use Node >= 22.15`);
    }
}
/** The file that actually holds `canonical`'s content, or null. */
export function resolveTranscript(canonical) {
    if (fs.existsSync(canonical))
        return canonical;
    const zst = canonical + ZST_SUFFIX;
    return fs.existsSync(zst) ? zst : null;
}
export function transcriptExists(canonical) {
    return resolveTranscript(canonical) !== null;
}
export function isCompressed(canonical) {
    return !fs.existsSync(canonical) && fs.existsSync(canonical + ZST_SUFFIX);
}
/** stat of whichever form is on disk (its mtime is the transcript's). */
export function statTranscript(canonical) {
    const actual = resolveTranscript(canonical);
    if (!actual)
        return null;
    try {
        return fs.statSync(actual);
    }
    catch {
        return null;
    }
}
/** A byte stream of the transcript's (decompressed) content. */
export function openTranscriptStream(canonical) {
    const actual = resolveTranscript(canonical) ?? canonical; // ENOENT surfaces on read
    const raw = fs.createReadStream(actual);
    if (!actual.endsWith(ZST_SUFFIX))
        return raw;
    requireZstd(actual);
    const out = zlib.createZstdDecompress();
    raw.on('error', (err) => out.destroy(err));
    return raw.pipe(out);
}
/** The whole transcript as a string (for show / read, which format all of it). */
export async function readTranscript(canonical) {
    const chunks = [];
    for await (const chunk of openTranscriptStream(canonical)) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks).toString('utf-8');
}
/**
 * Drop a stale compressed copy after a fresh `.jsonl` was written: the
 * plain file wins on read anyway, and the `.zst` would only waste space.
 */
export function removeCompressedCopy(canonical) {
    fs.rmSync(canonical + ZST_SUFFIX, { force: true });
}
async function sha256(stream) {
    const h = crypto.createHash('sha256');
    for await (const chunk of stream)
        h.update(chunk);
    return h.digest('hex');
}
/**
 * Replace `<canonical>` with `<canonical>.zst`, byte-verified: the
 * compressed file is decompressed and hashed against the original before
 * the original is removed. Keeps the original's mtime. Returns the bytes
 * before and after.
 */
export async function compressTranscript(canonical, level = DEFAULT_COMPRESSION_LEVEL) {
    requireZstd(canonical);
    const st = fs.statSync(canonical);
    const target = canonical + ZST_SUFFIX;
    const tmp = `${target}.tmp.${process.pid}`;
    try {
        await pipeline(fs.createReadStream(canonical), zlib.createZstdCompress({
            params: { [zlib.constants.ZSTD_c_compressionLevel]: level },
        }), fs.createWriteStream(tmp));
        const [want, got] = await Promise.all([
            sha256(fs.createReadStream(canonical)),
            sha256(fs.createReadStream(tmp).pipe(zlib.createZstdDecompress())),
        ]);
        if (want !== got) {
            throw new Error(`${canonical}: compressed copy does not round-trip`);
        }
        // The source may have been rewritten while we compressed it.
        const now = fs.statSync(canonical);
        if (now.mtimeMs !== st.mtimeMs || now.size !== st.size) {
            throw new Error(`${canonical} changed while compressing`);
        }
        fs.utimesSync(tmp, st.atimeMs / 1000, st.mtimeMs / 1000);
        fs.renameSync(tmp, target);
        fs.unlinkSync(canonical);
        return { before: st.size, after: fs.statSync(target).size };
    }
    finally {
        fs.rmSync(tmp, { force: true });
    }
}
