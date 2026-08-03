import type { Message } from "@ag-ui/client";
import { CodexRpcError, type CodexClient } from "@efferva/codex-client";

import { asObject, projectTurnMessages } from "../codexProjection";
import type { AgUiMessage, ThreadHistoryPage, ThreadSummary } from "../types";

type NativeTurnPage = {
  data: unknown[];
  nextCursor?: string | null;
};

type NativeThreadResume = {
  thread: ThreadSummary;
  model: string;
  reasoningEffort?: string | null;
  initialTurnsPage?: NativeTurnPage | null;
};

async function loadThreadHistoryPage(
  client: CodexClient,
  threadId: string,
  cursor?: string,
): Promise<ThreadHistoryPage> {
  let page: NativeTurnPage;
  let resume: NativeThreadResume | null = null;
  if (cursor) {
    page = await client.request<NativeTurnPage>("thread/turns/list", {
      threadId,
      cursor,
      limit: 20,
      sortDirection: "desc",
      itemsView: "full",
    });
  } else {
    resume = await client.request<NativeThreadResume>("thread/resume", {
      threadId,
      excludeTurns: true,
      initialTurnsPage: {
        limit: 20,
        sortDirection: "desc",
        itemsView: "full",
      },
    });
    if (!resume.initialTurnsPage) {
      throw new Error("thread/resume did not return initialTurnsPage");
    }
    page = resume.initialTurnsPage;
  }

  const turns = [...page.data].reverse().map(asObject);
  const detail: ThreadHistoryPage = {
    messages: projectTurnMessages(turns),
    next_cursor: page.nextCursor ?? null,
  };
  if (!resume) return detail;

  const activeTurn = [...turns]
    .reverse()
    .find((turn) => turn.status === "inProgress");
  const latestTurn = turns.at(-1);
  const extra = asObject(asObject(resume.thread).extra);
  const collaborationMode = String(
    asObject(extra.collaborationMode).mode ?? "",
  ).toLocaleLowerCase();
  detail.model = resume.model;
  detail.reasoning_effort = resume.reasoningEffort ?? null;
  detail.collaboration_mode = collaborationMode
    ? collaborationMode === "plan"
      ? "plan"
      : "default"
    : null;
  detail.active_turn_id = activeTurn ? String(activeTurn.id ?? "") : null;
  detail.active_turn_started_at = activeTurn?.startedAt as
    | string
    | number
    | null
    | undefined;
  if (latestTurn?.status === "failed") {
    detail.last_run_error =
      String(asObject(latestTurn.error).message ?? "") || "Turn failed";
  }
  return detail;
}

function isMissingThread(error: unknown): boolean {
  return (
    error instanceof CodexRpcError &&
    /not found|no rollout found/iu.test(error.message)
  );
}

function restoreMessages(messages: AgUiMessage[]): Message[] {
  return messages.flatMap((message) => {
    if (message.role !== "assistant" || !message.process?.length) {
      return [message as Message];
    }
    const reasoningText = message.process
      .flatMap((part) =>
        part.type === "reasoning" && part.text.trim() ? [part.text] : [],
      )
      .join("\n\n");
    const { process, processDurationMs, ...assistant } = message;
    const processMessage: EffervaProcessMessage = {
      id: `${message.id}:process`,
      role: "reasoning",
      content: reasoningText || " ",
      process,
      processDurationMs,
    };
    return [processMessage, assistant as Message];
  });
}

function mergeHistoryMessages(
  current: Message[],
  incoming: Message[],
): Message[] {
  const incomingById = new Map(
    incoming.map((message) => [message.id, message]),
  );
  const currentIds = new Set(current.map((message) => message.id));
  return [
    ...current.map((message) => incomingById.get(message.id) ?? message),
    ...incoming.filter((message) => !currentIds.has(message.id)),
  ];
}

function prependHistoryMessages(
  current: Message[],
  older: Message[],
): Message[] {
  const olderById = new Map(older.map((message) => [message.id, message]));
  const currentIds = new Set(current.map((message) => message.id));
  return [
    ...older.filter((message) => !currentIds.has(message.id)),
    ...current.map((message) => olderById.get(message.id) ?? message),
  ];
}

function mergeVisibleMessages(history: Message[], live: Message[]): Message[] {
  const liveById = new Map(live.map((message) => [message.id, message]));
  const historyIds = new Set(history.map((message) => message.id));
  return [
    ...history.map((message) => liveById.get(message.id) ?? message),
    ...live.filter((message) => !historyIds.has(message.id)),
  ];
}

type AssistantMessage = Extract<Message, { role: "assistant" }>;
type EffervaProcessMessage = Extract<Message, { role: "reasoning" }> & {
  process?: AgUiMessage["process"];
  processDurationMs?: number;
  processTextOffset?: number;
};
type ToolCall = NonNullable<AssistantMessage["toolCalls"]>[number];

export {
  isMissingThread,
  loadThreadHistoryPage,
  mergeHistoryMessages,
  mergeVisibleMessages,
  prependHistoryMessages,
  restoreMessages,
};
export type { AssistantMessage, EffervaProcessMessage, ToolCall };
