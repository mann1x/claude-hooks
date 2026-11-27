import { BGE_QUERY_PREFIX, EmbedderConfig } from './embedder-config.js';
import { VectorSpace } from './vector-space.js';
export { BGE_QUERY_PREFIX };
/**
 * Resolve an intra-op thread cap for the embedding session, or null to leave
 * onnxruntime at its default (one worker per core).
 *
 * Embedding is done one exchange at a time (batch=1) throughout indexing, sync,
 * and the re-embed migration. At that size onnxruntime-node's default intra-op
 * thread pool fans each tiny int8 matmul across every core, which on Apple
 * Silicon pegs ~5 cores for almost no throughput gain and makes a bulk re-embed
 * hog the whole machine. Capping intra-op threads keeps embedding off the
 * user's cores; measured on an M-series Mac, a cap of 2 is as fast or faster
 * than the uncapped default while using ~1.7 cores instead of ~5 (and pulls
 * further ahead when the machine is otherwise busy, since it stops the pool
 * from oversubscribing).
 *
 * This stays on the same CPU / q8 execution provider, so embeddings are
 * bit-identical to the uncapped output — no re-index is triggered. (CoreML and
 * WebGPU were evaluated and rejected: on this quantized model they either run
 * far slower and never leave the CPU, or require an fp32 model whose vectors
 * differ from the stored q8 ones and would force a full re-index.)
 *
 * Override with EPISODIC_MEMORY_EMBED_THREADS: a positive integer sets the cap;
 * 0 (or any non-positive/invalid value) restores onnxruntime's default.
 */
export declare function resolveIntraOpThreads(): number | null;
/**
 * Thrown when the embedding backend can't be loaded. The most common cause is
 * that `@huggingface/transformers` eagerly requires `sharp`, whose native
 * binding fails to `dlopen` libvips on some hosts (#135) — even though sharp is
 * only needed for image inputs, not the text feature-extraction this package
 * uses. Callers that can proceed without semantic features (e.g. background
 * sync indexing) should catch this and degrade gracefully rather than crash.
 *
 * Also thrown when a configured embedding server can't be reached or its
 * configuration can't be read.
 */
export declare class EmbeddingsUnavailableError extends Error {
    constructor(message: string, options?: {
        cause?: unknown;
    });
}
/** The embedder this process uses (resolved once). */
export declare function getEmbedderConfig(): EmbedderConfig;
/**
 * The space this process's vectors belong to, or null before it has produced
 * one (the width is learned from the first vector).
 */
export declare function activeVectorSpace(): VectorSpace | null;
/** Forget the resolved embedder (tests, and a config written mid-process). */
export declare function resetEmbedder(): void;
export declare function initEmbeddings(): Promise<void>;
/** Embed texts with the configured embedder (and its fallback). */
export declare function embedTexts(texts: string[]): Promise<number[][]>;
export declare function generateEmbedding(text: string): Promise<number[]>;
/**
 * Prepend the BGE retrieval prefix to a query string. Idempotent: returns
 * the input unchanged if the prefix is already present.
 */
export declare function withQueryPrefix(query: string, prefix?: string): string;
/**
 * Generate an embedding for a search QUERY. Adds the model's query prefix
 * (an instruction for qwen3-embedding, the retrieval prefix for BGE), which
 * gives a consistent recall lift. Documents get the model's document prefix,
 * empty for both — the asymmetric pattern these models are trained for.
 */
export declare function generateQueryEmbedding(query: string): Promise<number[]>;
/** The exact texts an exchange is embedded as, one per window (none = no vector). */
export declare function exchangeEmbeddingTexts(userMessage: string, assistantMessage: string): string[];
/**
 * Embed an exchange: one vector per window (exchange-text.ts), or null when it
 * has nothing worth a vector (a harness turn with no answer to speak of).
 * `toolNames` is accepted for compatibility and not embedded: tool names
 * matched every query a little.
 */
export declare function generateExchangeEmbedding(userMessage: string, assistantMessage: string, _toolNames?: string[]): Promise<number[][] | null>;
/** Batch form of generateExchangeEmbedding, for the migration. */
export declare function generateExchangeEmbeddings(rows: Array<{
    userMessage: string;
    assistantMessage: string;
}>): Promise<Array<number[][] | null>>;
