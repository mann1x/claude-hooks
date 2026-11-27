import type { FeatureExtractionPipeline } from '@huggingface/transformers';
import { getModelCacheDir } from './paths.js';
import {
  BGE_QUERY_PREFIX,
  EmbedderConfig,
  EmbedderConfigError,
  LOCAL_MODEL,
  resolveEmbedderConfig,
  spaceId,
} from './embedder-config.js';
import { composeExchangeChunks } from './exchange-text.js';
import { noteProducedSpace, producedSpace, VectorSpace } from './vector-space.js';

export { BGE_QUERY_PREFIX };

/**
 * Embedding model configuration.
 *
 * Using BAAI's bge-small-en-v1.5 (via Xenova's ONNX export) instead of the
 * older all-MiniLM-L6-v2 — measured +6.34 R@1 on a 17K-corpus retrieval test
 * against real production data. Same 384 dimensions, so vec_exchanges schema
 * is unchanged.
 *
 * BGE models recommend prepending a task prefix to QUERY embeddings only
 * (passages/documents go through unmodified). See `withQueryPrefix` and
 * `generateQueryEmbedding` below.
 *
 * That is the default. A host with its own embedding server (llamafile,
 * Ollama, any OpenAI-style /v1/embeddings) names it in embedder.json — see
 * embedder-config.ts — and every embedding goes there instead.
 */
const MODEL_ID = LOCAL_MODEL;
const MODEL_DTYPE = 'q8';

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
export function resolveIntraOpThreads(): number | null {
  const override = process.env.EPISODIC_MEMORY_EMBED_THREADS;
  if (override !== undefined) {
    const n = Number.parseInt(override, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  if (process.platform === 'darwin' && process.arch === 'arm64') {
    return 2;
  }
  return null;
}


let embeddingPipeline: FeatureExtractionPipeline | null = null;
let embedderConfig: EmbedderConfig | null = null;

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
export class EmbeddingsUnavailableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = 'EmbeddingsUnavailableError';
  }
}

/** The embedder this process uses (resolved once). */
export function getEmbedderConfig(): EmbedderConfig {
  if (!embedderConfig) {
    try {
      embedderConfig = resolveEmbedderConfig();
    } catch (error) {
      if (error instanceof EmbedderConfigError) {
        throw new EmbeddingsUnavailableError(
          `Embedder configuration unusable, semantic search is off until it is fixed: ${error.message}`,
          { cause: error }
        );
      }
      throw error;
    }
  }
  return embedderConfig;
}

/**
 * The space this process's vectors belong to, or null before it has produced
 * one (the width is learned from the first vector).
 */
export function activeVectorSpace(): VectorSpace | null {
  return producedSpace();
}

/** Forget the resolved embedder (tests, and a config written mid-process). */
export function resetEmbedder(): void {
  embedderConfig = null;
  noteProducedSpace(null);
}

async function initLocalPipeline(): Promise<void> {
  if (embeddingPipeline) return;

  // Load @huggingface/transformers lazily. Its module graph eagerly requires
  // `sharp`, so a static top-level import would crash *every* consumer of this
  // file at import time on hosts where sharp's native binding can't load —
  // including code paths that never embed anything. Importing here keeps the
  // failure catchable and confined to callers that actually need embeddings.
  let pipeline: typeof import('@huggingface/transformers').pipeline;
  try {
    const transformers = await import('@huggingface/transformers');
    // Disable progress callbacks / remote cache to prevent stdout pollution in
    // MCP context, where stdout is reserved for JSON-RPC communication.
    transformers.env.allowLocalModels = true;
    transformers.env.useBrowserCache = false;
    // Never the package's own `.cache`: see getModelCacheDir().
    transformers.env.cacheDir = getModelCacheDir();
    pipeline = transformers.pipeline;
  } catch (error) {
    throw new EmbeddingsUnavailableError(
      'Failed to load the embedding backend (@huggingface/transformers). This ' +
      'usually means the native "sharp" binding could not load libvips; ' +
      'semantic search and indexing are unavailable until it is fixed. ' +
      `Underlying error: ${error instanceof Error ? error.message : String(error)}`,
      { cause: error }
    );
  }

  console.error('Loading embedding model (first run may take time)...');
  const options: Parameters<typeof pipeline>[2] = {
    dtype: MODEL_DTYPE,
    progress_callback: () => {},
  };
  const intraOpThreads = resolveIntraOpThreads();
  if (intraOpThreads !== null) {
    options.session_options = {
      intraOpNumThreads: intraOpThreads,
      interOpNumThreads: 1,
    };
  }
  embeddingPipeline = await pipeline('feature-extraction', MODEL_ID, options);
  console.error('Embedding model loaded');
}

export async function initEmbeddings(): Promise<void> {
  const cfg = getEmbedderConfig();
  if (cfg.kind === 'local') await initLocalPipeline();
}

function unit(v: ArrayLike<number>): number[] {
  let n = 0;
  for (let i = 0; i < v.length; i++) n += v[i] * v[i];
  n = Math.sqrt(n) || 1;
  return Array.from(v, (x) => x / n);
}

/** One request to an embedding server; vectors in input order, unit length. */
async function embedRemote(cfg: EmbedderConfig, texts: string[]): Promise<number[][]> {
  let body: unknown;
  if (cfg.kind === 'llamafile') body = { content: texts };
  else if (cfg.kind === 'ollama') body = { model: cfg.model, input: texts, truncate: true };
  else body = { model: cfg.model, input: texts };
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (cfg.apiKey) headers.Authorization = `Bearer ${cfg.apiKey}`;

  let res: Response;
  try {
    res = await fetch(cfg.url!, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(cfg.timeoutMs),
    });
  } catch (error) {
    throw new EmbeddingsUnavailableError(
      `${cfg.kind} embedder unreachable at ${cfg.url} (${cfg.source}): ` +
      `${error instanceof Error ? error.message : String(error)}`,
      { cause: error }
    );
  }
  if (!res.ok) {
    const detail = (await res.text().catch(() => '')).slice(0, 300);
    throw new EmbeddingsUnavailableError(`${cfg.kind} embedder at ${cfg.url}: HTTP ${res.status} ${detail}`);
  }
  const data: any = await res.json();
  let vectors: unknown[];
  if (cfg.kind === 'llamafile') {
    // [{index, embedding: [[...]] | [...]}], or a bare {embedding} for one text.
    const items: any[] = Array.isArray(data) ? data : [data];
    vectors = [...items]
      .sort((a, b) => (a.index ?? 0) - (b.index ?? 0))
      .map((it) => (Array.isArray(it?.embedding?.[0]) ? it.embedding[0] : it?.embedding));
  } else if (cfg.kind === 'ollama') {
    vectors = data?.embeddings;
  } else {
    vectors = [...(data?.data ?? [])]
      .sort((a: any, b: any) => (a.index ?? 0) - (b.index ?? 0))
      .map((it: any) => it.embedding);
  }
  if (!Array.isArray(vectors) || vectors.length !== texts.length ||
      !vectors.every((v) => Array.isArray(v) && v.length > 0 && typeof v[0] === 'number')) {
    throw new EmbeddingsUnavailableError(
      `${cfg.kind} embedder at ${cfg.url} answered ${texts.length} text(s) with an unexpected shape`
    );
  }
  return (vectors as number[][]).map(unit);
}

async function embedLocal(texts: string[]): Promise<number[][]> {
  await initLocalPipeline();
  const out: number[][] = [];
  // One at a time, as always: batching pads, and padded inputs are not
  // guaranteed bit-identical to the vectors already stored.
  for (const text of texts) {
    const output = await embeddingPipeline!(text, { pooling: 'mean', normalize: true });
    out.push(Array.from(output.data as Float32Array));
  }
  return out;
}

/** Embed texts with the configured embedder (and its fallback). */
export async function embedTexts(texts: string[]): Promise<number[][]> {
  if (texts.length === 0) return [];
  const cfg = getEmbedderConfig();
  const cut = texts.map((t) => t.substring(0, cfg.maxChars));
  let vectors: number[][];
  if (cfg.kind === 'local') {
    vectors = await embedLocal(cut);
  } else {
    vectors = [];
    // Small requests: a CPU server answers each within its timeout.
    for (let i = 0; i < cut.length; i += cfg.batch) {
      const part = cut.slice(i, i + cfg.batch);
      try {
        vectors.push(...await embedRemote(cfg, part));
      } catch (error) {
        if (!cfg.fallback || !(error instanceof EmbeddingsUnavailableError)) throw error;
        console.error(`episodic-memory: ${error.message}; using the fallback`);
        const fb = cfg.fallback;
        const again = texts.slice(i, i + cfg.batch).map((t) => t.substring(0, fb.maxChars));
        vectors.push(...(fb.kind === 'local' ? await embedLocal(again) : await embedRemote(fb, again)));
      }
    }
  }
  const dims = vectors[0].length;
  const before = producedSpace();
  if (!vectors.every((v) => v.length === dims) || (before !== null && before.dims !== dims)) {
    throw new EmbeddingsUnavailableError(
      `${cfg.model} returned ${dims}-dimension vectors after ${before?.dims ?? 'other'}-dimension ones; ` +
      'is every server named in the embedder config serving the same model?'
    );
  }
  noteProducedSpace({ id: spaceId(cfg), dims });
  return vectors;
}

export async function generateEmbedding(text: string): Promise<number[]> {
  // Retrieval quality was best at the 2000-char cut for bge-small (its window
  // is 512 tokens; longer inputs degrade mean-pooled embeddings). The cut is
  // `max_chars` in the embedder config.
  const [v] = await embedTexts([text]);
  return v;
}

/**
 * Prepend the BGE retrieval prefix to a query string. Idempotent: returns
 * the input unchanged if the prefix is already present.
 */
export function withQueryPrefix(query: string, prefix: string = BGE_QUERY_PREFIX): string {
  if (!prefix || query.startsWith(prefix)) return query;
  return prefix + query;
}

/**
 * Generate an embedding for a search QUERY. Adds the model's query prefix
 * (an instruction for qwen3-embedding, the retrieval prefix for BGE), which
 * gives a consistent recall lift. Documents get the model's document prefix,
 * empty for both — the asymmetric pattern these models are trained for.
 */
export async function generateQueryEmbedding(query: string): Promise<number[]> {
  return generateEmbedding(withQueryPrefix(query, getEmbedderConfig().queryPrefix));
}

/** The exact texts an exchange is embedded as, one per window (none = no vector). */
export function exchangeEmbeddingTexts(userMessage: string, assistantMessage: string): string[] {
  const cfg = getEmbedderConfig();
  return composeExchangeChunks(userMessage, assistantMessage, cfg.maxChars - cfg.documentPrefix.length)
    .map((t) => cfg.documentPrefix + t);
}

/**
 * Embed an exchange: one vector per window (exchange-text.ts), or null when it
 * has nothing worth a vector (a harness turn with no answer to speak of).
 * `toolNames` is accepted for compatibility and not embedded: tool names
 * matched every query a little.
 */
export async function generateExchangeEmbedding(
  userMessage: string,
  assistantMessage: string,
  _toolNames?: string[]
): Promise<number[][] | null> {
  const texts = exchangeEmbeddingTexts(userMessage, assistantMessage);
  if (texts.length === 0) return null;
  return embedTexts(texts);
}

/** Batch form of generateExchangeEmbedding, for the migration. */
export async function generateExchangeEmbeddings(
  rows: Array<{ userMessage: string; assistantMessage: string }>
): Promise<Array<number[][] | null>> {
  const texts = rows.map((r) => exchangeEmbeddingTexts(r.userMessage, r.assistantMessage));
  const vectors = await embedTexts(texts.flat());
  let at = 0;
  return texts.map((t) => {
    if (t.length === 0) return null;
    const mine = vectors.slice(at, at + t.length);
    at += t.length;
    return mine;
  });
}
