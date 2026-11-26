/**
 * Which embedder produces the index's vectors.
 *
 * Default: the bundled bge-small model (ONNX, in process). A host that already
 * runs an embedding server can point the package at it instead, so episodic
 * search shares the model the rest of the user's tooling uses:
 *
 *   EPISODIC_MEMORY_EMBEDDER   JSON (same shape as the file), or `local`
 *   <superpowers>/embedder.json
 *
 *   {
 *     "kind": "llamafile" | "ollama" | "openai" | "local",
 *     "url": "http://127.0.0.1:38092/embedding",   // not for local
 *     "model": "qwen3-embedding:0.6b",              // names the vector space
 *     "api_key": "...",                             // openai only, optional
 *     "query_prefix": "...", "document_prefix": "...",  // default per model
 *     "max_chars": 2000, "timeout_ms": 60000, "batch": 16,
 *     "fallback": { ...another server of the same model... }
 *   }
 *
 * `model` names the vector space. Two servers of one model (a GPU Ollama for
 * a rebuild, a CPU llamafile for queries) share the space; a different model
 * is a different space, and the index re-embeds rather than compare across
 * them (see vector-space.ts).
 *
 * A config that cannot be read is an error, never "use the default": falling
 * back to bge-small would switch the index's space and throw away every
 * stored vector.
 */
import fs from 'fs';
import path from 'path';
import { getSuperpowersDir } from './paths.js';
export const LOCAL_MODEL = 'Xenova/bge-small-en-v1.5';
export const LOCAL_DIMS = 384;
export const BGE_QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';
export const QWEN3_QUERY_PREFIX = 'Instruct: Given a search query, retrieve relevant passages from past conversations that answer the query\nQuery: ';
/** Task prefixes the model was trained with, by model name. */
export function modelPrefixes(model) {
    const m = model.toLowerCase();
    if (m.includes('qwen3-embedding'))
        return { query: QWEN3_QUERY_PREFIX, document: '' };
    if (m.includes('nomic-embed'))
        return { query: 'search_query: ', document: 'search_document: ' };
    if (m.includes('bge-'))
        return { query: BGE_QUERY_PREFIX, document: '' };
    if (m.includes('e5-'))
        return { query: 'query: ', document: 'passage: ' };
    return { query: '', document: '' };
}
export class EmbedderConfigError extends Error {
    constructor(message) {
        super(message);
        this.name = 'EmbedderConfigError';
    }
}
export function getEmbedderConfigPath() {
    return path.join(getSuperpowersDir(), 'embedder.json');
}
const KINDS = ['local', 'llamafile', 'ollama', 'openai'];
function fromObject(raw, source, depth = 0) {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
        throw new EmbedderConfigError(`${source}: expected a JSON object`);
    }
    const o = raw;
    const kind = (o.kind ?? 'local');
    if (!KINDS.includes(kind)) {
        throw new EmbedderConfigError(`${source}: unknown kind ${JSON.stringify(o.kind)} (${KINDS.join(' | ')})`);
    }
    if (kind === 'local')
        return localConfig(source);
    const url = o.url;
    const model = o.model;
    if (typeof url !== 'string' || !/^https?:\/\//.test(url)) {
        throw new EmbedderConfigError(`${source}: ${kind} needs "url" (http:// or https://)`);
    }
    if (typeof model !== 'string' || !model.trim()) {
        throw new EmbedderConfigError(`${source}: ${kind} needs "model", the name of its vector space`);
    }
    const prefixes = modelPrefixes(model);
    const str = (k, d) => (typeof o[k] === 'string' ? o[k] : d);
    const num = (k, d) => {
        const v = o[k];
        return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : d;
    };
    const cfg = {
        kind,
        model: model.trim(),
        url,
        apiKey: typeof o.api_key === 'string' && o.api_key ? o.api_key : undefined,
        queryPrefix: str('query_prefix', prefixes.query),
        documentPrefix: str('document_prefix', prefixes.document),
        maxChars: num('max_chars', 2000),
        timeoutMs: num('timeout_ms', 60000),
        batch: Math.floor(num('batch', 16)),
        source,
    };
    if (o.fallback !== undefined && o.fallback !== null) {
        if (depth > 0)
            throw new EmbedderConfigError(`${source}: a fallback cannot have its own fallback`);
        const fb = fromObject(o.fallback, `${source} (fallback)`, depth + 1);
        if (spaceId(fb) !== spaceId(cfg)) {
            throw new EmbedderConfigError(`${source}: the fallback writes ${JSON.stringify(spaceId(fb))}, not ${JSON.stringify(spaceId(cfg))}; ` +
                'a fallback must serve the same model, or its vectors land in another space');
        }
        cfg.fallback = fb;
    }
    return cfg;
}
export function localConfig(source = 'default') {
    return {
        kind: 'local',
        model: LOCAL_MODEL,
        queryPrefix: BGE_QUERY_PREFIX,
        documentPrefix: '',
        maxChars: 2000,
        timeoutMs: 0,
        batch: 1,
        source,
    };
}
/** Resolve the embedder: environment, then the config file, then bge-small. */
export function resolveEmbedderConfig() {
    const env = process.env.EPISODIC_MEMORY_EMBEDDER;
    if (env !== undefined && env.trim() !== '') {
        const v = env.trim();
        if (v === 'local')
            return localConfig('EPISODIC_MEMORY_EMBEDDER');
        let raw;
        try {
            raw = JSON.parse(v);
        }
        catch (e) {
            throw new EmbedderConfigError(`EPISODIC_MEMORY_EMBEDDER is not JSON: ${e.message}`);
        }
        return fromObject(raw, 'EPISODIC_MEMORY_EMBEDDER');
    }
    const file = getEmbedderConfigPath();
    let text;
    try {
        text = fs.readFileSync(file, 'utf-8');
    }
    catch (e) {
        if (e.code === 'ENOENT')
            return localConfig();
        throw new EmbedderConfigError(`${file}: ${e.message}`);
    }
    let raw;
    try {
        raw = JSON.parse(text);
    }
    catch (e) {
        throw new EmbedderConfigError(`${file} is not JSON: ${e.message}`);
    }
    return fromObject(raw, file);
}
/**
 * The vector space a config writes into: the model, plus the document prefix
 * when it has one (a document prefix changes every stored vector; a query
 * prefix changes none).
 */
export function spaceId(cfg) {
    return cfg.documentPrefix ? `${cfg.model}|doc=${cfg.documentPrefix}` : cfg.model;
}
