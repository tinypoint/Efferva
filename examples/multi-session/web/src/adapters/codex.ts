import {
  CodexClient,
  type CodexNotification,
  type CodexServerRequest,
} from "@efferva/codex-client";

import { createSession, listSessions } from "../api";
import type {
  AgentAdapter,
  AgentEvent,
  ChatMessage,
  MessageBlock,
  Thread,
  ThreadSnapshot,
} from "../types";
import { StreamQueue } from "./streamQueue";

type JsonObject = Record<string, unknown>;

export class CodexAdapter implements AgentAdapter {
  readonly id = "codex" as const;
  readonly label = "Codex";
  private readonly prefix = "/codex";
  private client?: CodexClient;
  private sessionId?: string;

  listSessions() {
    return listSessions(this.prefix);
  }

  createSession(name: string) {
    return createSession(this.prefix, name);
  }

  async listThreads(sessionId: string): Promise<Thread[]> {
    const response = await this.connection(sessionId).request<{
      data: JsonObject[];
    }>("thread/list", {
      limit: 100,
      sortKey: "updated_at",
      sortDirection: "desc",
      sourceKinds: ["vscode"],
    });
    return response.data.map(projectThread);
  }

  async readThread(
    sessionId: string,
    threadId: string,
  ): Promise<ThreadSnapshot> {
    const response = await this.connection(sessionId).request<JsonObject>(
      "thread/resume",
      {
        threadId,
        excludeTurns: true,
        initialTurnsPage: {
          limit: 100,
          sortDirection: "desc",
          itemsView: "full",
        },
      },
    );
    const thread = object(response.thread);
    const page = object(response.initialTurnsPage);
    const turns = Array.isArray(page.data) ? [...page.data].reverse() : [];
    return {
      thread: projectThread(thread),
      messages: turns.flatMap(projectTurn),
      active: object(thread.status).type === "active",
    };
  }

  async deleteThread(sessionId: string, threadId: string): Promise<void> {
    await this.connection(sessionId).request("thread/delete", { threadId });
  }

  async *sendMessage(
    sessionId: string,
    requestedThreadId: string | "new",
    prompt: string,
  ): AsyncIterable<AgentEvent> {
    const client = this.connection(sessionId);
    await client.connect();
    let threadId = requestedThreadId;
    if (threadId === "new") {
      const workspace = "/home/node/workspace";
      await client.request("fs/createDirectory", {
        path: workspace,
        recursive: true,
      });
      const response = await client.request<{ thread: JsonObject }>(
        "thread/start",
        {
          cwd: workspace,
          approvalPolicy: "never",
          sandbox: "danger-full-access",
          historyMode: "paginated",
        },
      );
      const thread = projectThread(response.thread);
      thread.title = shortTitle(prompt);
      threadId = thread.id;
      await client.request("thread/name/set", {
        threadId,
        name: thread.title,
      });
      yield { type: "thread", thread };
    } else {
      await client.request("thread/resume", { threadId, excludeTurns: true });
    }

    const queue = new StreamQueue<AgentEvent>();
    let turnId: string | undefined;
    const unsubscribe = client.onNotification((notification) => {
      const params = object(notification.params);
      const notificationThread = String(params.threadId ?? "");
      const notificationTurn = String(params.turnId ?? "");
      if (notificationThread && notificationThread !== threadId) return;
      if (turnId && notificationTurn && notificationTurn !== turnId) return;
      projectNotification(notification, queue, threadId);
    });
    const unsubscribeClose = client.onClose((event) => {
      queue.push({
        type: "error",
        message: event.reason || `Codex WebSocket closed (${event.code})`,
      });
      queue.close();
    });

    try {
      const started = await client.request<{ turn: JsonObject }>("turn/start", {
        threadId,
        input: [{ type: "text", text: prompt, textElements: [] }],
      });
      turnId = String(started.turn.id ?? "") || undefined;
      for await (const event of queue) yield event;
    } catch (error) {
      yield {
        type: "error",
        message: error instanceof Error ? error.message : String(error),
      };
    } finally {
      unsubscribe();
      unsubscribeClose();
      queue.close();
    }
  }

  dispose(): void {
    this.client?.close();
    this.client = undefined;
    this.sessionId = undefined;
  }

  private connection(sessionId: string): CodexClient {
    if (this.client && this.sessionId === sessionId) return this.client;
    this.dispose();
    this.sessionId = sessionId;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.client = new CodexClient({
      url: `${protocol}//${window.location.host}${this.prefix}/api/sessions/${encodeURIComponent(sessionId)}/codex`,
      serverRequestHandler: defaultServerRequest,
    });
    return this.client;
  }
}

function projectNotification(
  notification: CodexNotification,
  queue: StreamQueue<AgentEvent>,
  threadId: string,
): void {
  const params = object(notification.params);
  if (notification.method === "item/agentMessage/delta") {
    queue.push({ type: "text", delta: String(params.delta ?? "") });
    return;
  }
  if (
    notification.method === "item/reasoning/summaryTextDelta" ||
    notification.method === "item/reasoning/textDelta"
  ) {
    queue.push({ type: "thinking", delta: String(params.delta ?? "") });
    return;
  }
  if (notification.method === "item/completed") {
    const block = projectTool(object(params.item));
    if (block) queue.push({ type: "tool", block });
    return;
  }
  if (notification.method === "turn/completed") {
    queue.push({ type: "done", threadId });
    queue.close();
    return;
  }
  if (notification.method === "error") {
    const error = object(params.error);
    queue.push({
      type: "error",
      message: String(error.message ?? params.message ?? "Codex run failed"),
    });
    queue.close();
  }
}

function projectThread(value: JsonObject): Thread {
  return {
    id: String(value.id ?? value.sessionId ?? ""),
    title: String(value.name ?? value.preview ?? "Untitled Thread"),
    updatedAt: (value.updatedAt ?? value.createdAt ?? null) as
      | number
      | string
      | null,
    active: object(value.status).type === "active",
  };
}

function projectTurn(value: unknown): ChatMessage[] {
  const turn = object(value);
  const items = Array.isArray(turn.items) ? turn.items.map(object) : [];
  const messages: ChatMessage[] = [];
  for (const item of items) {
    if (item.type !== "userMessage") continue;
    const content = Array.isArray(item.content) ? item.content.map(object) : [];
    const text = content
      .filter((part) => part.type === "text")
      .map((part) => String(part.text ?? ""))
      .join("");
    if (text) {
      messages.push({
        id: String(item.id ?? crypto.randomUUID()),
        role: "user",
        blocks: [{ type: "text", text }],
      });
    }
  }

  const blocks: MessageBlock[] = [];
  const finalAgent = [...items]
    .reverse()
    .find((item) => item.type === "agentMessage");
  for (const item of items) {
    if (item.type === "reasoning") {
      const text = [
        ...(Array.isArray(item.summary) ? item.summary : []),
        ...(Array.isArray(item.content) ? item.content : []),
      ]
        .map(String)
        .filter(Boolean)
        .join("\n\n");
      if (text) blocks.push({ type: "thinking", text });
    }
    const tool = projectTool(item);
    if (tool) blocks.push(tool);
  }
  if (finalAgent?.text) {
    blocks.push({ type: "text", text: String(finalAgent.text) });
  }
  if (blocks.length) {
    messages.push({
      id: String(finalAgent?.id ?? turn.id ?? crypto.randomUUID()),
      role: "assistant",
      blocks,
    });
  }
  return messages;
}

function projectTool(item: JsonObject): Extract<MessageBlock, { type: "tool" }> | null {
  const type = String(item.type ?? "");
  if (type === "commandExecution") {
    return {
      type: "tool",
      name: "exec_command",
      input: { command: item.command, cwd: item.cwd },
      output: item.aggregatedOutput ?? item.status,
      error: item.status === "failed",
    };
  }
  if (type === "fileChange") {
    return {
      type: "tool",
      name: "apply_patch",
      input: item.changes,
      output: item.status,
      error: item.status === "failed",
    };
  }
  if (type === "mcpToolCall" || type === "dynamicToolCall") {
    return {
      type: "tool",
      name: String(item.tool ?? "tool"),
      input: item.arguments,
      output: item.result ?? item.error ?? item.status,
      error: item.status === "failed" || item.error != null,
    };
  }
  return null;
}

async function defaultServerRequest(request: CodexServerRequest): Promise<unknown> {
  if (request.method === "currentTime/read") {
    return { currentTimeAt: Math.floor(Date.now() / 1000) };
  }
  if (request.method === "item/tool/requestUserInput") return { answers: {} };
  if (
    request.method === "item/commandExecution/requestApproval" ||
    request.method === "item/fileChange/requestApproval"
  ) {
    return { decision: "decline" };
  }
  if (request.method === "item/permissions/requestApproval") {
    return { permissions: {}, scope: "turn" };
  }
  if (request.method === "mcpServer/elicitation/request") {
    return { action: "decline", content: null, _meta: null };
  }
  throw new Error(`Unsupported Codex server request: ${request.method}`);
}

function shortTitle(prompt: string): string {
  const title = prompt.trim().split(/\r?\n/u)[0] || "New Thread";
  return title.length > 48 ? `${title.slice(0, 47)}…` : title;
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" ? (value as JsonObject) : {};
}
