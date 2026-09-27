// How much of a tool call's input and result the index keeps.
//
// Nothing reads tool_calls.tool_input or tool_result back: search, show and
// the embedding migration use only tool_name, and the full call is still in
// the archived transcript. On a long-lived install the column was most of
// the database (6.5 GB of an 8 GB db.sqlite, 5.6 M rows, on one host), so
// the default is to keep none of it. EPISODIC_MEMORY_TOOL_INPUT_CHARS keeps
// a prefix for anyone who queries the table directly.
export const DEFAULT_TOOL_INPUT_CHARS = 0;
export function getToolInputMaxChars(env = process.env) {
    const raw = env.EPISODIC_MEMORY_TOOL_INPUT_CHARS;
    const parsed = raw !== undefined ? Number.parseInt(raw, 10) : NaN;
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : DEFAULT_TOOL_INPUT_CHARS;
}
/** The stored form of a tool input or result: null, or at most maxChars. */
export function capToolText(value, maxChars) {
    if (maxChars <= 0 || value === undefined || value === null || value === '') {
        return null;
    }
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    return text.length > maxChars ? text.slice(0, maxChars) : text;
}
