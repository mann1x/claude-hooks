export interface ToolCall {
  id: string;
  exchangeId: string;
  toolName: string;
  toolInput?: any;
  toolResult?: string;
  isError: boolean;
  timestamp: string;
}

export type ConversationHarness = 'claude' | 'codex' | 'cursor' | 'opencode' | 'omp';

export interface ConversationExchange {
  id: string;
  project: string;
  timestamp: string;
  userMessage: string;
  assistantMessage: string;
  archivePath: string;
  lineStart: number;
  lineEnd: number;

  // Conversation structure
  parentUuid?: string;
  isSidechain?: boolean;

  // Session context
  harness?: ConversationHarness;
  sessionId?: string;
  cwd?: string;
  gitBranch?: string;
  claudeVersion?: string;
  agentVersion?: string;
  model?: string;
  modelProvider?: string;

  // Thinking metadata
  thinkingLevel?: string;
  thinkingDisabled?: boolean;
  thinkingTriggers?: string; // JSON array

  // Tool calls (populated separately)
  toolCalls?: ToolCall[];
}

export interface SearchResult {
  exchange: ConversationExchange;
  similarity: number;
  snippet: string;
  /**
   * The transcript's last indexed line (`max(line_end)` over its exchanges),
   * read from the index. Never counted by reading the file: a live transcript
   * can be gigabytes, and counting a 6.4 GB one took ~97 s per search.
   */
  lastLine?: number;
}

export interface MultiConceptResult {
  exchange: ConversationExchange;
  snippet: string;
  conceptSimilarities: number[];
  averageSimilarity: number;
  /**
   * The transcript's last indexed line (`max(line_end)` over its exchanges),
   * read from the index. Never counted by reading the file: a live transcript
   * can be gigabytes, and counting a 6.4 GB one took ~97 s per search.
   */
  lastLine?: number;
}
