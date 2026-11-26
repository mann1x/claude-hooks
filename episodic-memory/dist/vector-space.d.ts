/**
 * The vector space the index's vectors live in.
 *
 * `index_meta.embedder` names the space (see spaceId() in embedder-config.ts)
 * and the vec_exchanges column's declared width is its dimension. An index
 * with no `embedder` entry predates this and holds bge-small vectors.
 *
 * Vectors from two models are never compared. When the configured embedder
 * writes into another space, switchVectorSpace() recreates vec_exchanges at
 * the new width and marks every exchange stale, and the embedding migration
 * re-embeds them. Until it has, search uses the vectors already rebuilt and
 * keyword ranking for the rest.
 */
import Database from 'better-sqlite3';
export interface VectorSpace {
    id: string;
    dims: number;
}
export declare const LEGACY_SPACE: VectorSpace;
/** The width vec_exchanges was declared with, or null when it has none. */
export declare function vecTableDims(db: Database.Database): number | null;
export declare function storedSpace(db: Database.Database): VectorSpace;
export declare function sameSpace(a: VectorSpace, b: VectorSpace): boolean;
/**
 * Recreate vec_exchanges for `to` and mark every exchange for re-embedding.
 * One transaction: a reader sees the old space or the new one, never a mix.
 */
export declare function switchVectorSpace(db: Database.Database, to: VectorSpace): void;
/** Set by embeddings.ts when its embedder produces a vector. */
export declare function noteProducedSpace(space: VectorSpace | null): void;
/** The space this process's embedder writes into, or null before it has run. */
export declare function producedSpace(): VectorSpace | null;
/**
 * Make the index hold `space` (default: the space this process's embedder
 * produced) before a vector from it is written. `null` — no embedder has run
 * in this process, e.g. a caller storing vectors it made itself — checks
 * nothing. Cached per connection.
 */
export declare function ensureVectorSpace(db: Database.Database, space?: VectorSpace | null): void;
/** One vector, or one per window (exchange-text.ts), or none. */
export type ExchangeVectors = number[] | number[][] | null;
/** Remove every vector of an exchange, every window included. */
export declare function deleteExchangeVectors(db: Database.Database, exchangeId: string): void;
/**
 * Replace an exchange's vectors. A vector from another model's space switches
 * the index first (and marks every exchange for re-embedding).
 */
export declare function writeExchangeVectors(db: Database.Database, exchangeId: string, vectors: ExchangeVectors): void;
