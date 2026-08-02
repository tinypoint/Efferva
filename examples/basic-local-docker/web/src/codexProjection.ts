import type { Message } from "@ag-ui/client";

import type { AgUiMessage } from "./types";

export type JsonObject = Record<string, unknown>;

export type ProjectedToolCall = {
  id: string;
  name: string;
  arguments: string;
  result: string;
  isError: boolean;
};

const TOOL_ITEM_TYPES = new Set([
  "commandExecution",
  "fileChange",
  "mcpToolCall",
  "dynamicToolCall",
  "collabToolCall",
  "webSearch",
  "imageView",
  "sleep",
]);

export function asObject(value: unknown): JsonObject {
  return typeof value === "object" && value !== null
    ? (value as JsonObject)
    : {};
}

function json(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value ?? null);
}

export function projectToolCall(value: unknown): ProjectedToolCall | null {
  const item = asObject(value);
  const type = String(item.type ?? "");
  if (!TOOL_ITEM_TYPES.has(type)) return null;
  const id = String(item.id ?? crypto.randomUUID());
  let name: string;
  let parameters: unknown;
  let result: unknown;
  let isError = false;

  if (type === "commandExecution") {
    name = "exec_command";
    parameters = { command: item.command ?? "", cwd: item.cwd ?? null };
    result =
      item.aggregatedOutput ??
      { status: item.status ?? null, exitCode: item.exitCode ?? null };
    isError = item.status === "failed" || item.status === "declined";
  } else if (type === "fileChange") {
    name = "apply_patch";
    parameters = { changes: item.changes ?? [] };
    result = { status: item.status ?? null };
    isError = item.status === "failed" || item.status === "declined";
  } else if (type === "mcpToolCall") {
    name = `mcp__${String(item.server ?? "mcp")}__${String(item.tool ?? "tool")}`;
    parameters = item.arguments ?? {};
    result = item.result ?? item.error ?? { status: item.status ?? null };
    isError = item.status === "failed" || item.error != null;
  } else if (type === "dynamicToolCall") {
    name = String(item.tool ?? "tool");
    parameters = item.arguments ?? {};
    result =
      item.contentItems ??
      { status: item.status ?? null, success: item.success ?? null };
    isError = item.status === "failed" || item.success === false;
  } else if (type === "collabToolCall") {
    name = String(item.tool ?? "collaboration");
    parameters = {
      prompt: item.prompt ?? null,
      receiverThreadId: item.receiverThreadId ?? null,
    };
    result = {
      status: item.status ?? null,
      newThreadId: item.newThreadId ?? null,
      agentStatus: item.agentStatus ?? null,
    };
    isError = item.status === "failed";
  } else if (type === "webSearch") {
    name = "web_search";
    parameters = { query: item.query ?? null, action: item.action ?? null };
    result = item.results ?? { status: item.status ?? null };
  } else if (type === "imageView") {
    name = "view_image";
    parameters = { path: item.path ?? null };
    result = { status: item.status ?? "completed" };
  } else {
    name = "wait";
    parameters = { durationMs: item.durationMs ?? null };
    result = { status: item.status ?? "completed" };
  }

  return {
    id,
    name,
    arguments: JSON.stringify(parameters),
    result: json(result),
    isError,
  };
}

function userMessageContent(parts: unknown): string | JsonObject[] {
  const content = Array.isArray(parts) ? parts.map(asObject) : [];
  const text = content
    .filter((part) => part.type === "text")
    .map((part) => String(part.text ?? ""))
    .join("");
  const projected: JsonObject[] = text ? [{ type: "text", text }] : [];
  for (const part of content) {
    if (part.type !== "image" && part.type !== "audio") continue;
    const url = String(part.url ?? "");
    if (!url) continue;
    projected.push({
      type: part.type,
      source: { type: "url", value: url },
    });
  }
  return projected.some((part) => part.type !== "text") ? projected : text;
}

export function projectTurnMessages(turns: unknown[]): AgUiMessage[] {
  const messages: Message[] = [];
  const projectedTurns = turns.map(asObject);
  const activeTurn = [...projectedTurns]
    .reverse()
    .find((turn) => turn.status === "inProgress");
  const activeTurnId = activeTurn ? String(activeTurn.id ?? "") : null;

  for (const turn of projectedTurns) {
    const items = Array.isArray(turn.items) ? turn.items.map(asObject) : [];
    for (const item of items) {
      if (item.type !== "userMessage") continue;
      messages.push({
        id: String(item.id ?? crypto.randomUUID()),
        role: "user",
        content: userMessageContent(item.content),
      } as Message);
    }
    if (String(turn.id ?? "") === activeTurnId) continue;

    const finalMessage = [...items]
      .reverse()
      .find((item) => item.type === "agentMessage");
    const toolCalls: ProjectedToolCall[] = [];
    const process: NonNullable<AgUiMessage["process"]> = [];
    for (const item of items) {
      if (item.type === "agentMessage" && item !== finalMessage) {
        const text = String(item.text ?? "");
        if (text) process.push({ type: "reasoning", text });
        continue;
      }
      if (item.type === "reasoning") {
        const text = [
          ...(Array.isArray(item.summary) ? item.summary : []),
          ...(Array.isArray(item.content) ? item.content : []),
        ]
          .map(String)
          .filter((part) => part.trim())
          .join("\n\n");
        if (text) process.push({ type: "reasoning", text });
        continue;
      }
      if (item.type === "plan") {
        const text = String(item.text ?? "").trim();
        if (text) process.push({ type: "reasoning", text: `计划\n\n${text}` });
        continue;
      }
      const toolCall = projectToolCall(item);
      if (!toolCall) continue;
      toolCalls.push(toolCall);
      process.push({ type: "tool-call", toolCallId: toolCall.id });
    }

    if (finalMessage || toolCalls.length) {
      const assistant: AgUiMessage = {
        id: String(finalMessage?.id ?? `${String(turn.id ?? "")}:assistant`),
        role: "assistant",
        content: String(finalMessage?.text ?? ""),
        ...(toolCalls.length
          ? {
              toolCalls: toolCalls.map((toolCall) => ({
                id: toolCall.id,
                type: "function" as const,
                function: {
                  name: toolCall.name,
                  arguments: toolCall.arguments,
                },
              })),
            }
          : {}),
        ...(process.length ? { process } : {}),
      } as AgUiMessage;
      if (process.length) {
        const durationMs =
          typeof turn.durationMs === "number"
            ? turn.durationMs
            : typeof turn.startedAt === "number" &&
                typeof turn.completedAt === "number"
              ? Math.max(0, (turn.completedAt - turn.startedAt) * 1000)
              : null;
        if (durationMs !== null) assistant.processDurationMs = Math.round(durationMs);
      }
      messages.push(assistant as Message);
    }

    for (const toolCall of toolCalls) {
      messages.push({
        id: `${toolCall.id}:result`,
        role: "tool",
        name: toolCall.name,
        toolCallId: toolCall.id,
        content: toolCall.result,
        isError: toolCall.isError,
      } as Message);
    }
  }

  return messages as AgUiMessage[];
}
