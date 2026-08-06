export type EngineId = "codex" | "claude";

export type Session = {
  id: string;
  name: string;
  status: string;
  last_active_at: string;
};

export type Thread = {
  id: string;
  title: string;
  updatedAt?: number | string | null;
  active?: boolean;
};

export type MessageBlock =
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | {
      type: "tool";
      name: string;
      input?: unknown;
      output?: unknown;
      error?: boolean;
    };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  blocks: MessageBlock[];
  live?: boolean;
};

export type ThreadSnapshot = {
  thread: Thread;
  messages: ChatMessage[];
  active: boolean;
};

export type AgentEvent =
  | { type: "thread"; thread: Thread }
  | { type: "text"; delta: string }
  | { type: "thinking"; delta: string }
  | { type: "tool"; block: Extract<MessageBlock, { type: "tool" }> }
  | { type: "message"; message: ChatMessage }
  | { type: "done"; threadId: string }
  | { type: "error"; message: string };

export interface AgentAdapter {
  readonly id: EngineId;
  readonly label: string;
  listSessions(): Promise<Session[]>;
  createSession(name: string): Promise<Session>;
  listThreads(sessionId: string): Promise<Thread[]>;
  readThread(sessionId: string, threadId: string): Promise<ThreadSnapshot>;
  deleteThread(sessionId: string, threadId: string): Promise<void>;
  sendMessage(
    sessionId: string,
    threadId: string | "new",
    prompt: string,
  ): AsyncIterable<AgentEvent>;
  dispose(): void;
}
