import Database from 'better-sqlite3';
import { SearchResult, MultiConceptResult } from './types.js';
export interface SearchOptions {
    limit?: number;
    mode?: 'vector' | 'text' | 'both';
    after?: string;
    before?: string;
    project?: string;
    session_id?: string;
    git_branch?: string;
    include_sidechains?: boolean;
    per_conversation?: boolean;
}
/**
 * The FTS5 query for a free-text search: every token as a quoted term, OR-ed,
 * so BM25 ranks rows holding more (and rarer) terms first. Quoting makes
 * operators and punctuation literal; the tokenizer splits `op-296` into the
 * phrase "op 296". Null when the query has no token.
 */
export declare function buildFtsQuery(query: string): string | null;
/**
 * Keep each conversation's best-ranked row (rows arrive in rank order). A
 * long session otherwise fills several of the top 10 with neighbouring
 * exchanges of one piece of work.
 */
export declare function bestPerConversation<T extends {
    archive_path: string;
}>(rows: T[]): T[];
/** Reciprocal rank fusion of ranked row lists (rows keyed by `id`). */
export declare function fuseByRank<T extends {
    id: string;
}>(lists: T[][], limit: number): T[];
/**
 * Escape LIKE wildcards so user input is treated as a literal substring.
 * Callers must use `ESCAPE '\\'` on the LIKE expression.
 */
export declare function escapeLikePattern(term: string): string;
/**
 * Split a text-search query into whitespace-separated terms.
 * Multi-word queries match when every term appears somewhere in the exchange
 * (user or assistant message), in any order — not only as one contiguous phrase.
 */
export declare function tokenizeTextQuery(query: string): string[];
/**
 * Build the text-match WHERE fragment and bound params for LIKE search.
 * One AND-ed clause per token; each token may hit user_message or assistant_message.
 */
export declare function buildTextMatchClause(query: string): {
    sql: string;
    params: string[];
};
/**
 * Convert an L2 (Euclidean) distance between two unit-normalized vectors
 * into a cosine similarity in [-1, 1].
 *
 * For unit vectors u, v:  ||u - v||^2 = 2 - 2 * cos(u, v)
 * Therefore:               cos(u, v) = 1 - d^2 / 2
 *
 * Embeddings written by src/embeddings.ts are normalized at write time, so
 * the L2 distance returned by sqlite-vec satisfies the unit-vector identity.
 */
export declare function l2DistanceToCosineSimilarity(distance: number): number;
export declare function searchConversations(query: string, options?: SearchOptions): Promise<SearchResult[]>;
/**
 * Each transcript's last indexed line, in one query over the index
 * (`idx_archive_path`). Result formatting used to read every result's whole
 * transcript to count its lines; one 6.4 GB live transcript made a search
 * take ~97 s.
 */
export declare function lastIndexedLines(db: Database.Database, archivePaths: string[]): Map<string, number>;
export declare function formatResults(results: Array<SearchResult & {
    summary?: string;
}>): Promise<string>;
export declare function searchMultipleConcepts(concepts: string[], options?: Omit<SearchOptions, 'mode'>): Promise<MultiConceptResult[]>;
export declare function formatMultiConceptResults(results: MultiConceptResult[], concepts: string[]): Promise<string>;
