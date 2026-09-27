export declare const DEFAULT_TOOL_INPUT_CHARS = 0;
export declare function getToolInputMaxChars(env?: NodeJS.ProcessEnv): number;
/** The stored form of a tool input or result: null, or at most maxChars. */
export declare function capToolText(value: unknown, maxChars: number): string | null;
