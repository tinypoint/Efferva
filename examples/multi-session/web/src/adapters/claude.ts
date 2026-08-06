import { createSession, listSessions, request } from "../api";
import type {
  AgentAdapter,
  AgentEvent,
  ChatMessage,
  MessageBlock,
  Thread,
  ThreadSnapshot,
} from "../types";

type JsonObject = Record<string, unknown>;
type NativeThread = {
  session_id: string;
  summary?: string;
  last_modified?: number;
};
type NativeSnapshot = {
  thread: NativeThread;
  messages: unknown[];
  active: boolean;
};
type SseEvent = { event: string; data: JsonObject };

export class ClaudeAdapter implements AgentAdapter {
  readonly id = "claude" as const;
  readonly label = "Claude Code";
  private readonly prefix = "/claude";
  private controller?: AbortController;

  listSessions() {
    return listSessions(this.prefix);
  }

  createSession(name: string) {
    return createSession(this.prefix, name);
  }

  async listThreads(sessionId: string): Promise<Thread[]> {
    const threads = await request<NativeThread[]>(
      this.prefix,
      `/api/sessions/${sessionId}/claude/threads`,
    );
    return threads.map(projectThread);
  }

  async readThread(
    sessionId: string,
    threadId: string,
  ): Promise<ThreadSnapshot> {
    const snapshot = await request<NativeSnapshot>(
      this.prefix,
      `/api/sessions/${sessionId}/claude/threads/${threadId}`,
    );
    return {
      thread: projectThread(snapshot.thread),
      messages: snapshot.messages.flatMap(projectMessage),
      active: snapshot.active,
    };
  }

  async deleteThread(sessionId: string, threadId: string): Promise<void> {
    await request(
      this.prefix,
      `/api/sessions/${sessionId}/claude/threads/${threadId}`,
      { method: "DELETE" },
    );
  }

  async *sendMessage(
    sessionId: string,
    threadId: string | "new",
    prompt: string,
  ): AsyncIterable<AgentEvent> {
    this.controller?.abort();
    this.controller = new AbortController();
    const response = await fetch(
      `${this.prefix}/api/sessions/${sessionId}/claude/messages`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ threadId, prompt }),
        signal: this.controller.signal,
      },
    );
    if (!response.ok || !response.body) {
      const payload = (await response.json().catch(() => ({}))) as JsonObject;
      yield {
        type: "error",
        message: String(payload.detail ?? `HTTP ${response.status}`),
      };
      return;
    }

    for await (const item of readSse(response.body)) {
      if (item.event === "error") {
        yield {
          type: "error",
          message: String(item.data.message ?? "Claude run failed"),
        };
        return;
      }
      const discovered = threadFromEvent(item);
      if (discovered && threadId === "new") {
        threadId = discovered;
        yield {
          type: "thread",
          thread: { id: discovered, title: shortTitle(prompt) },
        };
      }
      if (item.event === "done") {
        yield { type: "done", threadId: discovered ?? threadId };
        return;
      }
      if (item.event !== "message") continue;
      const messageType = String(item.data.type ?? "");
      if (messageType === "StreamEvent") {
        const event = object(object(item.data.message).event);
        if (event.type !== "content_block_delta") continue;
        const delta = object(event.delta);
        if (typeof delta.text === "string") {
          yield { type: "text", delta: delta.text };
        } else if (typeof delta.thinking === "string") {
          yield { type: "thinking", delta: delta.thinking };
        }
      } else if (messageType === "AssistantMessage") {
        const projected = projectMessage(item.data)[0];
        if (projected) yield { type: "message", message: projected };
      }
    }
  }

  dispose(): void {
    this.controller?.abort();
    this.controller = undefined;
  }
}

function projectThread(thread: NativeThread): Thread {
  return {
    id: thread.session_id,
    title: thread.summary?.trim() || "Untitled Thread",
    updatedAt: thread.last_modified ?? null,
  };
}

function projectMessage(value: unknown): ChatMessage[] {
  const record = object(value);
  const envelope = object(record.message);
  const native = object(envelope.message ?? record.message ?? value);
  const type = String(record.type ?? envelope.type ?? native.role ?? "");
  if (type === "StreamEvent" || type === "SystemMessage" || type === "ResultMessage") {
    return [];
  }
  const role = type.toLowerCase().includes("user") ? "user" : "assistant";
  const content = native.content ?? envelope.content ?? record.content;
  const values = Array.isArray(content) ? content : [content];
  const blocks = values.flatMap(projectBlock);
  if (!blocks.length) return [];
  return [
    {
      id: String(native.uuid ?? native.id ?? crypto.randomUUID()),
      role,
      blocks,
    },
  ];
}

function projectBlock(value: unknown): MessageBlock[] {
  if (typeof value === "string") return [{ type: "text", text: value }];
  const block = object(value);
  const type = String(block.type ?? "");
  if (typeof block.text === "string") {
    return [{ type: "text", text: block.text }];
  }
  if (typeof block.thinking === "string") {
    return [{ type: "thinking", text: block.thinking }];
  }
  if (type === "tool_use" || type === "ToolUseBlock") {
    return [
      {
        type: "tool",
        name: String(block.name ?? "tool"),
        input: block.input,
      },
    ];
  }
  if (type === "tool_result" || type === "ToolResultBlock") {
    return [
      {
        type: "tool",
        name: "tool result",
        output: block.content,
        error: block.is_error === true,
      },
    ];
  }
  return [];
}

function threadFromEvent(item: SseEvent): string | null {
  if (item.event === "done" && typeof item.data.threadId === "string") {
    return item.data.threadId;
  }
  const message = object(item.data.message);
  if (typeof message.session_id === "string") return message.session_id;
  const data = object(message.data);
  return typeof data.session_id === "string" ? data.session_id : null;
}

async function* readSse(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const event =
          lines.find((line) => line.startsWith("event:"))?.slice(6).trim() ??
          "message";
        const data = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (data) yield { event, data: JSON.parse(data) as JsonObject };
        boundary = buffer.indexOf("\n\n");
      }
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}

function shortTitle(prompt: string): string {
  const title = prompt.trim().split(/\r?\n/u)[0] || "New Thread";
  return title.length > 48 ? `${title.slice(0, 47)}…` : title;
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" ? (value as JsonObject) : {};
}
