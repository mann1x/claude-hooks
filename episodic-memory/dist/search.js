import { initDatabase } from './db.js';
import { EmbeddingsUnavailableError, generateQueryEmbedding, getEmbedderConfig, initEmbeddings, } from './embeddings.js';
import { spaceId } from './embedder-config.js';
import { storedSpace } from './vector-space.js';
import { isErroredSentinel } from './summary-sentinel.js';
import fs from 'fs';
import { statTranscript } from './transcript-io.js';
/**
 * Distance penalty (L2, in the same units as vec.distance) added to a
 * sidechain row's score so that an equally-relevant main-thread exchange
 * ranks ahead of it. Sidechains (subagent and `Workflow` transcripts) carry
 * the substance of orchestrated sessions, so they must stay reachable; this
 * only de-prioritizes them on ties and near-ties, letting a clearly-more-
 * relevant sidechain still outrank a weaker main-thread match. Chosen small:
 * ~0.05 in L2 distance is a few points of cosine similarity for typical
 * normalized embeddings.
 */
const SIDECHAIN_DISTANCE_PENALTY = 0.05;
/**
 * Default search (`both`) ranks by reciprocal rank fusion of the vector list
 * and the BM25 keyword list: score = sum of 1 / (RRF_K + rank). Each list is
 * drawn from a pool larger than the limit so fusion has room to reorder.
 * Measured on benchmarks/episodic (judged pool, 28 queries): vector alone
 * nDCG@10 0.37, fused 0.55 — keyword ranking finds ids, file names and
 * error strings an embedding blurs.
 */
const RRF_K = 60;
const FUSION_POOL_MIN = 50;
/** Windows fetched per wanted exchange: an exchange may have several near the query. */
const VECTOR_WINDOW_FETCH = 2;
/**
 * The FTS5 query for a free-text search: every token as a quoted term, OR-ed,
 * so BM25 ranks rows holding more (and rarer) terms first. Quoting makes
 * operators and punctuation literal; the tokenizer splits `op-296` into the
 * phrase "op 296". Null when the query has no token.
 */
export function buildFtsQuery(query) {
    const terms = (query.match(/[\p{L}\p{N}_]+(?:[-.:/][\p{L}\p{N}_]+)*/gu) || [])
        .map((t) => t.toLowerCase());
    const unique = [...new Set(terms)];
    return unique.length ? unique.map((t) => `"${t.replace(/"/g, '""')}"`).join(' OR ') : null;
}
/** Reciprocal rank fusion of ranked row lists (rows keyed by `id`). */
export function fuseByRank(lists, limit) {
    const scored = new Map();
    for (const list of lists) {
        list.forEach((row, i) => {
            const hit = scored.get(row.id);
            const add = 1 / (RRF_K + i + 1);
            if (hit) {
                hit.score += add;
                // Keep the vector row (it carries the distance) when both lists have it.
                if (hit.row.distance == null && row.distance != null)
                    hit.row = row;
            }
            else {
                scored.set(row.id, { row, score: add });
            }
        });
    }
    return [...scored.values()].sort((a, b) => b.score - a.score).slice(0, limit).map((x) => x.row);
}
/**
 * Build the AND-clause and bound-parameter list that constrains a search
 * by the optional time and metadata filters. Bound parameters keep us
 * safe from SQL injection without regex-based input scrubbing.
 */
function buildSearchFilters(options) {
    const parts = [];
    const params = [];
    if (options.after) {
        parts.push('e.timestamp >= ?');
        params.push(options.after);
    }
    if (options.before) {
        parts.push('e.timestamp <= ?');
        params.push(options.before);
    }
    if (options.project) {
        parts.push('e.project = ?');
        params.push(options.project);
    }
    if (options.session_id) {
        parts.push('e.session_id = ?');
        params.push(options.session_id);
    }
    if (options.git_branch) {
        parts.push('e.git_branch = ?');
        params.push(options.git_branch);
    }
    return {
        sql: parts.length ? `AND ${parts.join(' AND ')}` : '',
        params,
    };
}
/**
 * Escape LIKE wildcards so user input is treated as a literal substring.
 * Callers must use `ESCAPE '\\'` on the LIKE expression.
 */
export function escapeLikePattern(term) {
    return term.replace(/\\/g, '\\\\').replace(/%/g, '\\%').replace(/_/g, '\\_');
}
/**
 * Split a text-search query into whitespace-separated terms.
 * Multi-word queries match when every term appears somewhere in the exchange
 * (user or assistant message), in any order — not only as one contiguous phrase.
 */
export function tokenizeTextQuery(query) {
    return query.trim().split(/\s+/).filter(t => t.length > 0);
}
/**
 * Build the text-match WHERE fragment and bound params for LIKE search.
 * One AND-ed clause per token; each token may hit user_message or assistant_message.
 */
export function buildTextMatchClause(query) {
    const tokens = tokenizeTextQuery(query);
    // Empty / whitespace-only: keep previous %% semantics (match all messages).
    const terms = tokens.length > 0 ? tokens : [''];
    const parts = [];
    const params = [];
    for (const term of terms) {
        parts.push(`(e.user_message LIKE ? ESCAPE '\\' OR e.assistant_message LIKE ? ESCAPE '\\')`);
        const pattern = `%${escapeLikePattern(term)}%`;
        params.push(pattern, pattern);
    }
    return { sql: parts.join(' AND '), params };
}
const EXCHANGE_SELECT_COLUMNS = `
        e.id,
        e.project,
        e.timestamp,
        e.user_message,
        e.assistant_message,
        e.archive_path,
        e.line_start,
        e.line_end,
        e.parent_uuid,
        e.is_sidechain,
        e.harness,
        e.session_id,
        e.cwd,
        e.git_branch,
        e.claude_version,
        e.agent_version,
        e.model,
        e.model_provider,
        e.thinking_level,
        e.thinking_disabled,
        e.thinking_triggers`;
function exchangeFromRow(row) {
    return {
        id: row.id,
        project: row.project,
        timestamp: row.timestamp,
        userMessage: row.user_message,
        assistantMessage: row.assistant_message,
        archivePath: row.archive_path,
        lineStart: row.line_start,
        lineEnd: row.line_end,
        parentUuid: row.parent_uuid || undefined,
        isSidechain: Boolean(row.is_sidechain),
        harness: row.harness,
        sessionId: row.session_id || undefined,
        cwd: row.cwd || undefined,
        gitBranch: row.git_branch || undefined,
        claudeVersion: row.claude_version || undefined,
        agentVersion: row.agent_version || undefined,
        model: row.model || undefined,
        modelProvider: row.model_provider || undefined,
        thinkingLevel: row.thinking_level || undefined,
        thinkingDisabled: row.thinking_disabled === null ? undefined : Boolean(row.thinking_disabled),
        thinkingTriggers: row.thinking_triggers || undefined,
    };
}
/**
 * Embed a query for the index's vectors. Refuses (EmbeddingsUnavailableError)
 * when the index holds another model's vectors — they are never compared —
 * or the embedder can't run.
 */
async function vectorQuery(db, query) {
    const cfg = getEmbedderConfig();
    const stored = storedSpace(db);
    if (stored.id !== spaceId(cfg)) {
        throw new EmbeddingsUnavailableError(`the index holds ${stored.id} vectors and the configured embedder (${cfg.source}) writes ` +
            `${spaceId(cfg)}; the next sync re-embeds them`);
    }
    await initEmbeddings();
    const v = await generateQueryEmbedding(query);
    if (v.length !== stored.dims) {
        throw new EmbeddingsUnavailableError(`${cfg.model} returned ${v.length}-dimension vectors, the index holds ${stored.dims}`);
    }
    return v;
}
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
export function l2DistanceToCosineSimilarity(distance) {
    const similarity = 1 - (distance * distance) / 2;
    return Math.max(-1, Math.min(1, similarity));
}
function validateISODate(dateStr, paramName) {
    const isoDateRegex = /^\d{4}-\d{2}-\d{2}$/;
    if (!isoDateRegex.test(dateStr)) {
        throw new Error(`Invalid ${paramName} date: "${dateStr}". Expected YYYY-MM-DD format (e.g., 2025-10-01)`);
    }
    // Verify it's actually a valid date
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) {
        throw new Error(`Invalid ${paramName} date: "${dateStr}". Not a valid calendar date.`);
    }
}
export async function searchConversations(query, options = {}) {
    const { limit = 10, mode = 'both', after, before } = options;
    const includeSidechains = options.include_sidechains !== false;
    const sidechainClause = includeSidechains ? '' : 'AND e.is_sidechain = 0';
    // Validate date parameters
    if (after)
        validateISODate(after, '--after');
    if (before)
        validateISODate(before, '--before');
    const db = initDatabase();
    let results = [];
    const { sql: filterClause, params: filterParams } = buildSearchFilters(options);
    if (mode === 'vector' || mode === 'both') {
        // Vector similarity search.
        // vec0 applies KNN before the WHERE clause and before our sidechain
        // de-rank, so we over-fetch candidates and trim after the final ordering.
        try {
            const queryEmbedding = await vectorQuery(db, query);
            // Vectors are windows (`<exchange id>` or `<exchange id>#<k>`, exchange-text.ts),
            // so fetch more and keep each exchange's nearest window.
            const k = Math.min((mode === 'both' ? Math.max(limit * 5, FUSION_POOL_MIN) : limit * 3) * VECTOR_WINDOW_FETCH, 4096);
            const stmt = db.prepare(`
        SELECT
          ${EXCHANGE_SELECT_COLUMNS},
          vec.distance
        FROM vec_exchanges AS vec
        JOIN exchanges AS e ON e.id = CASE WHEN instr(vec.id, '#') > 0
                                          THEN substr(vec.id, 1, instr(vec.id, '#') - 1)
                                          ELSE vec.id END
        WHERE vec.embedding MATCH ?
          AND k = ?
          ${sidechainClause}
          ${filterClause}
        ORDER BY (vec.distance + e.is_sidechain * ?) ASC
      `);
            const seen = new Set();
            results = stmt.all(Buffer.from(new Float32Array(queryEmbedding).buffer), k, ...filterParams, SIDECHAIN_DISTANCE_PENALTY).filter((r) => !seen.has(r.id) && seen.add(r.id));
        }
        catch (error) {
            // Default search keeps its keyword half when the vector half can't run.
            if (mode === 'vector' || !(error instanceof EmbeddingsUnavailableError))
                throw error;
            console.error(`episodic-memory: keyword ranking only — ${error.message}`);
            results = [];
        }
        if (mode === 'vector' && results.length > limit) {
            results = results.slice(0, limit);
        }
    }
    if (mode === 'both') {
        const ftsQuery = buildFtsQuery(query);
        let keywordRows = [];
        if (ftsQuery) {
            keywordRows = db.prepare(`
        SELECT
          ${EXCHANGE_SELECT_COLUMNS},
          NULL AS distance
        FROM exchanges_fts AS f
        JOIN exchanges AS e ON e.rowid = f.rowid
        WHERE exchanges_fts MATCH ?
          ${sidechainClause}
          ${filterClause}
        ORDER BY bm25(exchanges_fts)
        LIMIT ?
      `).all(ftsQuery, ...filterParams, Math.max(limit * 5, FUSION_POOL_MIN));
        }
        results = fuseByRank([results, keywordRows], limit);
    }
    if (mode === 'text') {
        // Text search: AND of per-token LIKE patterns so multi-word queries match
        // when every term appears somewhere in the exchange (any order, either field).
        // See #127 — whole-query contiguous substring matching returned empty for
        // typical multi-word searches that are not verbatim phrases. Sidechain rows
        // are de-ranked (ordered after main-thread rows), not excluded (#128).
        const { sql: textMatchSql, params: textMatchParams } = buildTextMatchClause(query);
        const textStmt = db.prepare(`
      SELECT
        ${EXCHANGE_SELECT_COLUMNS},
        0 as distance
      FROM exchanges AS e
      WHERE ${textMatchSql}
        ${sidechainClause}
        ${filterClause}
      ORDER BY e.is_sidechain ASC, e.timestamp DESC
      LIMIT ?
    `);
        results = textStmt.all(...textMatchParams, ...filterParams, limit);
    }
    const lastLines = lastIndexedLines(db, results.map((row) => row.archive_path));
    db.close();
    return results.map((row) => {
        const exchange = exchangeFromRow(row);
        // Try to load summary if available. Skip error sentinels (#96) so failed
        // summarizations don't surface as the conversation's summary in results.
        const summaryPath = row.archive_path.replace('.jsonl', '-summary.txt');
        let summary;
        if (fs.existsSync(summaryPath)) {
            const raw = fs.readFileSync(summaryPath, 'utf-8');
            if (!isErroredSentinel(raw)) {
                summary = raw.trim();
            }
        }
        // Create snippet (first 200 chars, collapse newlines)
        const snippetText = exchange.userMessage.substring(0, 200).replace(/\s+/g, ' ').trim();
        const snippet = snippetText + (exchange.userMessage.length > 200 ? '...' : '');
        return {
            exchange,
            similarity: mode === 'text' || row.distance == null
                ? undefined
                : l2DistanceToCosineSimilarity(row.distance),
            snippet,
            summary,
            lastLine: lastLines.get(row.archive_path)
        };
    });
}
/**
 * Each transcript's last indexed line, in one query over the index
 * (`idx_archive_path`). Result formatting used to read every result's whole
 * transcript to count its lines; one 6.4 GB live transcript made a search
 * take ~97 s.
 */
export function lastIndexedLines(db, archivePaths) {
    const unique = [...new Set(archivePaths)];
    const out = new Map();
    if (unique.length === 0)
        return out;
    const rows = db
        .prepare(`SELECT archive_path, MAX(line_end) AS last_line FROM exchanges
       WHERE archive_path IN (${unique.map(() => '?').join(', ')})
       GROUP BY archive_path`)
        .all(...unique);
    for (const row of rows) {
        if (row.last_line !== null)
            out.set(row.archive_path, row.last_line);
    }
    return out;
}
function fileInfo(sizeKB, lastLine) {
    return lastLine !== undefined ? `${sizeKB}KB, ${lastLine} lines` : `${sizeKB}KB`;
}
// Helper function to get file size in KB
function getFileSizeInKB(filePath) {
    try {
        const stats = statTranscript(filePath);
        if (!stats)
            return 0;
        return Math.round(stats.size / 1024 * 10) / 10; // Round to 1 decimal place
    }
    catch (error) {
        return 0;
    }
}
export async function formatResults(results) {
    if (results.length === 0) {
        return 'No results found.';
    }
    let output = `Found ${results.length} relevant conversation${results.length > 1 ? 's' : ''}:\n\n`;
    // Process results sequentially to get file metadata
    for (let index = 0; index < results.length; index++) {
        const result = results[index];
        const date = new Date(result.exchange.timestamp).toISOString().split('T')[0];
        const simPct = result.similarity !== undefined ? Math.round(result.similarity * 100) : null;
        // Header with match percentage
        output += `${index + 1}. [${result.exchange.project}, ${date}]`;
        if (simPct !== null) {
            output += ` - ${simPct}% match`;
        }
        output += '\n';
        // Show summary only if it's concise (< 300 chars)
        if (result.summary && result.summary.length < 300) {
            output += `   ${result.summary}\n`;
        }
        // Show snippet
        output += `   "${result.snippet}"\n`;
        // Show tool usage if available
        if (result.exchange.toolCalls && result.exchange.toolCalls.length > 0) {
            const toolCounts = new Map();
            result.exchange.toolCalls.forEach(tc => {
                toolCounts.set(tc.toolName, (toolCounts.get(tc.toolName) || 0) + 1);
            });
            const toolSummary = Array.from(toolCounts.entries())
                .map(([name, count]) => `${name}(${count})`)
                .join(', ');
            output += `   Tools: ${toolSummary}\n`;
        }
        // Get file metadata (the line count comes from the index, never the file)
        const fileSizeKB = getFileSizeInKB(result.exchange.archivePath);
        const lineRange = `${result.exchange.lineStart}-${result.exchange.lineEnd}`;
        // File information with metadata (clean format for smart tool selection)
        output += `   Lines ${lineRange} in ${result.exchange.archivePath} (${fileInfo(fileSizeKB, result.lastLine)})\n\n`;
    }
    return output;
}
export async function searchMultipleConcepts(concepts, options = {}) {
    const { limit = 10 } = options;
    if (concepts.length === 0) {
        return [];
    }
    // Search for each concept independently
    const conceptResults = await Promise.all(concepts.map(concept => searchConversations(concept, { ...options, limit: limit * 5, mode: 'vector' })));
    // Build map of conversation path -> array of results (one per concept)
    const conversationMap = new Map();
    conceptResults.forEach((results, conceptIndex) => {
        results.forEach(result => {
            const key = result.exchange.archivePath;
            if (!conversationMap.has(key)) {
                conversationMap.set(key, []);
            }
            conversationMap.get(key).push({ ...result, conceptIndex });
        });
    });
    // Find conversations that match ALL concepts
    const multiConceptResults = [];
    for (const [archivePath, results] of conversationMap.entries()) {
        // Check if all concepts are represented
        const representedConcepts = new Set(results.map(r => r.conceptIndex));
        if (representedConcepts.size === concepts.length) {
            // All concepts found in this conversation
            const conceptSimilarities = concepts.map((_concept, index) => {
                const result = results.find(r => r.conceptIndex === index);
                return result?.similarity || 0;
            });
            const averageSimilarity = conceptSimilarities.reduce((sum, sim) => sum + sim, 0) / conceptSimilarities.length;
            // Use the first result's exchange data (they're all from the same conversation)
            const firstResult = results[0];
            multiConceptResults.push({
                exchange: firstResult.exchange,
                snippet: firstResult.snippet,
                conceptSimilarities,
                averageSimilarity,
                lastLine: firstResult.lastLine
            });
        }
    }
    // Sort by average similarity (highest first)
    multiConceptResults.sort((a, b) => b.averageSimilarity - a.averageSimilarity);
    // Apply limit
    return multiConceptResults.slice(0, limit);
}
export async function formatMultiConceptResults(results, concepts) {
    if (results.length === 0) {
        return `No conversations found matching all concepts: ${concepts.join(', ')}`;
    }
    let output = `Found ${results.length} conversation${results.length > 1 ? 's' : ''} matching all concepts [${concepts.join(' + ')}]:\n\n`;
    // Process results sequentially to get file metadata
    for (let index = 0; index < results.length; index++) {
        const result = results[index];
        const date = new Date(result.exchange.timestamp).toISOString().split('T')[0];
        const avgPct = Math.round(result.averageSimilarity * 100);
        // Header with average match percentage
        output += `${index + 1}. [${result.exchange.project}, ${date}] - ${avgPct}% avg match\n`;
        // Show individual concept scores
        const scores = result.conceptSimilarities
            .map((sim, i) => `${concepts[i]}: ${Math.round(sim * 100)}%`)
            .join(', ');
        output += `   Concepts: ${scores}\n`;
        // Show snippet
        output += `   "${result.snippet}"\n`;
        // Show tool usage if available
        if (result.exchange.toolCalls && result.exchange.toolCalls.length > 0) {
            const toolCounts = new Map();
            result.exchange.toolCalls.forEach(tc => {
                toolCounts.set(tc.toolName, (toolCounts.get(tc.toolName) || 0) + 1);
            });
            const toolSummary = Array.from(toolCounts.entries())
                .map(([name, count]) => `${name}(${count})`)
                .join(', ');
            output += `   Tools: ${toolSummary}\n`;
        }
        // Get file metadata (the line count comes from the index, never the file)
        const fileSizeKB = getFileSizeInKB(result.exchange.archivePath);
        const lineRange = `${result.exchange.lineStart}-${result.exchange.lineEnd}`;
        // File information with metadata (clean format for smart tool selection)
        output += `   Lines ${lineRange} in ${result.exchange.archivePath} (${fileInfo(fileSizeKB, result.lastLine)})\n\n`;
    }
    return output;
}
