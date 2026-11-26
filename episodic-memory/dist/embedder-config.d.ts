export type EmbedderKind = 'local' | 'llamafile' | 'ollama' | 'openai';
export interface EmbedderConfig {
    kind: EmbedderKind;
    model: string;
    url?: string;
    apiKey?: string;
    queryPrefix: string;
    documentPrefix: string;
    maxChars: number;
    timeoutMs: number;
    /** Texts per request to a server. */
    batch: number;
    fallback?: EmbedderConfig;
    /** Where the setting came from, for messages. */
    source: string;
}
export declare const LOCAL_MODEL = "Xenova/bge-small-en-v1.5";
export declare const LOCAL_DIMS = 384;
export declare const BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: ";
export declare const QWEN3_QUERY_PREFIX = "Instruct: Given a search query, retrieve relevant passages from past conversations that answer the query\nQuery: ";
/** Task prefixes the model was trained with, by model name. */
export declare function modelPrefixes(model: string): {
    query: string;
    document: string;
};
export declare class EmbedderConfigError extends Error {
    constructor(message: string);
}
export declare function getEmbedderConfigPath(): string;
export declare function localConfig(source?: string): EmbedderConfig;
/** Resolve the embedder: environment, then the config file, then bge-small. */
export declare function resolveEmbedderConfig(): EmbedderConfig;
/**
 * The vector space a config writes into: the model, plus the document prefix
 * when it has one (a document prefix changes every stored vector; a query
 * prefix changes none).
 */
export declare function spaceId(cfg: EmbedderConfig): string;
