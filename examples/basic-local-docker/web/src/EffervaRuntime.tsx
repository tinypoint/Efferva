import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { HttpAgent } from "@ag-ui/client";
import {
  AssistantRuntimeProvider,
  CompositeAttachmentAdapter,
  ExportedMessageRepository,
  SimpleImageAttachmentAdapter,
  type AttachmentAdapter,
  type ChatModelRunResult,
  type CompleteAttachment,
  type PendingAttachment,
  type ThreadHistoryAdapter,
} from "@assistant-ui/react";
import {
  fromAgUiMessages,
  useAgUiRuntime,
  type UseAgUiThreadListAdapter,
} from "@assistant-ui/react-ag-ui";
import { finalize, tap } from "rxjs";
import type { ReadonlyJSONObject } from "assistant-stream/utils";

import { api } from "./api";
import { RunControlsProvider } from "./RunControls";
import type { CreateThreadInput, ThreadSummary } from "./types";

type ProcessPart =
  | { type: "reasoning"; text: string }
  | { type: "tool-call"; toolCallId: string };

class AudioAttachmentAdapter implements AttachmentAdapter {
  accept = "audio/mpeg,audio/mp3,audio/wav,audio/x-wav";

  async add({ file }: { file: File }): Promise<PendingAttachment> {
    return {
      id: crypto.randomUUID(),
      type: "audio",
      name: file.name,
      contentType: file.type,
      file,
      status: { type: "requires-action", reason: "composer-send" },
    };
  }

  async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
    const format = attachment.contentType?.includes("wav") ? "wav" : "mp3";
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(attachment.file);
    });
    return {
      ...attachment,
      status: { type: "complete" },
      content: [{ type: "audio", audio: { data, format } }],
    };
  }

  async remove() {}
}

const restoreMessages = (messages: readonly unknown[]) => {
  const rawById = new Map(
    messages
      .filter(
        (message): message is Record<string, unknown> =>
          typeof message === "object" && message !== null,
      )
      .map((message) => [String(message.id ?? ""), message]),
  );
  return fromAgUiMessages(messages, { showThinking: false }).map((message) => {
    if (message.role !== "assistant" || !Array.isArray(message.content)) {
      return message;
    }
    const raw = rawById.get(String(message.id ?? ""));
    const process = Array.isArray(raw?.process)
      ? (raw.process as ProcessPart[])
      : [];
    if (process.length === 0) return message;
    const toolCalls = new Map(
      message.content
        .filter(
          (part) => typeof part === "object" && part?.type === "tool-call",
        )
        .map((part) => [part.toolCallId, part]),
    );
    const processContent = process.flatMap((part) => {
      if (part.type === "reasoning") {
        return [{ type: "reasoning" as const, text: part.text }];
      }
      const toolCall = toolCalls.get(part.toolCallId);
      return toolCall ? [toolCall] : [];
    });
    return {
      ...message,
      metadata: {
        ...message.metadata,
        custom: {
          ...message.metadata?.custom,
          ...(typeof raw?.processDurationMs === "number"
            ? { processDurationMs: raw.processDurationMs }
            : {}),
        },
      },
      content: [
        ...processContent,
        ...message.content.filter(
          (part) => !(typeof part === "object" && part?.type === "tool-call"),
        ),
      ],
    };
  });
};

type EffervaRuntimeProps = {
  sessionId: string;
  threadId?: string;
  threads: ThreadSummary[];
  model?: string;
  reasoningEffort: NonNullable<CreateThreadInput["reasoning_effort"]>;
  children: ReactNode;
  onNewThread: () => void;
  onOpenThread: (threadId: string) => void;
  onThreadCreated: (thread: ThreadSummary) => void;
  onThreadDeleted: (threadId: string) => void;
  queuedMessages: string[];
  setQueuedMessages: Dispatch<SetStateAction<string[]>>;
};

export function EffervaRuntime({
  sessionId,
  threadId,
  threads,
  model,
  reasoningEffort,
  children,
  onNewThread,
  onOpenThread,
  onThreadCreated,
  onThreadDeleted,
  queuedMessages,
  setQueuedMessages,
}: EffervaRuntimeProps) {
  const [error, setError] = useState<string | null>(null);
  const attachmentAdapter = useMemo(
    () =>
      new CompositeAttachmentAdapter([
        new SimpleImageAttachmentAdapter(),
        new AudioAttachmentAdapter(),
      ]),
    [],
  );
  const activeTurn = useRef<{ threadId: string; turnId: string } | null>(null);
  const visibleThreadId = useRef(threadId);
  const runtimeRef = useRef<ReturnType<typeof useAgUiRuntime> | null>(null);
  visibleThreadId.current = threadId;

  const agent = useMemo(() => {
    let pendingCreatedThread: ThreadSummary | undefined;
    let notified = false;
    let settledThreadId: string | undefined;
    const current = new HttpAgent({
      url: "/agent/api/ag-ui",
      threadId: threadId ?? "new",
      headers: { Accept: "text/event-stream" },
    });

    current.use((input, next) => {
      let activeThreadId = threadId ?? "new";
      return next.run({
        ...input,
        threadId: activeThreadId,
        forwardedProps: {
          ...(input.forwardedProps ?? {}),
          sessionId,
          ...(!threadId
            ? {
                ...(model?.trim() ? { model: model.trim() } : {}),
                reasoningEffort,
              }
            : {}),
        },
      }).pipe(
        tap((event) => {
          const raw = event as {
            type?: string;
            outcome?: { type?: string };
            event?: {
              method?: string;
              params?: { turnId?: string; thread?: ThreadSummary };
            };
          };
          if (
            raw.type === "RAW" &&
            raw.event?.method === "efferva/thread-created" &&
            raw.event.params?.thread
          ) {
            pendingCreatedThread = raw.event.params.thread;
            activeThreadId = pendingCreatedThread.id;
          }
          if (
            raw.type === "RAW" &&
            raw.event?.method === "efferva/turn-started" &&
            raw.event.params?.turnId
          ) {
            activeTurn.current = {
              threadId: activeThreadId,
              turnId: raw.event.params.turnId,
            };
            if (pendingCreatedThread && !notified) {
              notified = true;
              onThreadCreated(pendingCreatedThread);
            }
          }
          if (
            (raw.type === "RUN_FINISHED" && raw.outcome?.type !== "interrupt") ||
            raw.type === "RUN_CANCELLED" ||
            raw.type === "RUN_ERROR"
          ) {
            settledThreadId =
              activeThreadId === "new" ? undefined : activeThreadId;
            activeTurn.current = null;
          }
        }),
        finalize(() => {
          if (!settledThreadId) return;
          const completedThreadId = settledThreadId;
          void api
            .readThread(sessionId, completedThreadId)
            .then((thread) => {
              if (visibleThreadId.current !== completedThreadId) return;
              const repository = ExportedMessageRepository.fromArray(
                restoreMessages(thread.messages),
              );
              const currentRuntime = runtimeRef.current;
              if (!currentRuntime) return;
              currentRuntime.thread.reset([]);
              currentRuntime.thread.reset(
                repository.messages.map((item) => item.message),
              );
            })
            .catch((cause: unknown) =>
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Failed to refresh the completed turn",
              ),
            );
        }),
      );
    });
    return current;
  }, [model, onThreadCreated, reasoningEffort, sessionId, threadId]);

  const history = useMemo<ThreadHistoryAdapter>(
    () => ({
      async load() {
        if (!threadId) return ExportedMessageRepository.fromArray([]);
        const thread = await api.readThread(sessionId, threadId);
        activeTurn.current = thread.active_turn_id
          ? { threadId, turnId: thread.active_turn_id }
          : null;
        const repository = ExportedMessageRepository.fromArray(
          restoreMessages(thread.messages),
        );
        return {
          ...repository,
          unstable_resume: Boolean(thread.active_turn_id),
        };
      },
      async *resume(options) {
        if (!threadId) return;
        const resumedTurnId = activeTurn.current?.turnId;
        if (!resumedTurnId) return;
        const partOrder: Array<{ type: "text" | "tool"; id: string }> = [];
        const textByPart = new Map<string, string>();
        const toolByPart = new Map<
          string,
          {
            type: "tool-call";
            toolCallId: string;
            toolName: string;
            argsText: string;
            args: ReadonlyJSONObject;
            result?: unknown;
            isError?: boolean;
          }
        >();
        const snapshot = (): ChatModelRunResult["content"] =>
          partOrder.map((part) => {
            if (part.type === "tool") return toolByPart.get(part.id)!;
            return {
              type: "text" as const,
              text: textByPart.get(part.id) ?? "",
            };
          });
        for await (const event of api.resumeThread(
          sessionId,
          threadId,
          resumedTurnId,
          options.abortSignal,
        )) {
          const type = String(event.type ?? "");
          if (type === "RAW") {
            const raw = event.event as
              | { method?: string; params?: { turnId?: string } }
              | undefined;
            if (
              raw?.method === "efferva/turn-started" &&
              raw.params?.turnId
            ) {
              activeTurn.current = {
                threadId,
                turnId: raw.params.turnId,
              };
            }
            continue;
          }
          if (type === "TEXT_MESSAGE_START") {
            const messageId = String(event.messageId ?? "");
            if (messageId && !textByPart.has(messageId)) {
              partOrder.push({ type: "text", id: messageId });
              textByPart.set(messageId, "");
            }
            continue;
          }
          if (type === "TEXT_MESSAGE_CONTENT") {
            const messageId = String(event.messageId ?? "");
            if (!textByPart.has(messageId)) {
              partOrder.push({ type: "text", id: messageId });
            }
            textByPart.set(
              messageId,
              `${textByPart.get(messageId) ?? ""}${String(event.delta ?? "")}`,
            );
            yield { content: snapshot(), status: { type: "running" } };
            continue;
          }
          if (type === "TOOL_CALL_START") {
            const toolCallId = String(event.toolCallId ?? "");
            if (toolCallId && !toolByPart.has(toolCallId)) {
              partOrder.push({ type: "tool", id: toolCallId });
              toolByPart.set(toolCallId, {
                type: "tool-call",
                toolCallId,
                toolName: String(event.toolCallName ?? "tool"),
                argsText: "",
                args: {},
              });
            }
            continue;
          }
          if (type === "TOOL_CALL_ARGS" || type === "TOOL_CALL_CHUNK") {
            const toolCallId = String(event.toolCallId ?? "");
            const tool = toolByPart.get(toolCallId);
            if (!tool) continue;
            tool.argsText += String(event.delta ?? "");
            try {
              tool.args = JSON.parse(tool.argsText) as ReadonlyJSONObject;
            } catch {
              // Arguments can be incomplete while streaming.
            }
            yield { content: snapshot(), status: { type: "running" } };
            continue;
          }
          if (type === "TOOL_CALL_RESULT") {
            const toolCallId = String(event.toolCallId ?? "");
            const tool = toolByPart.get(toolCallId);
            if (!tool) continue;
            const content = event.content;
            try {
              tool.result =
                typeof content === "string" ? JSON.parse(content) : content;
            } catch {
              tool.result = content;
            }
            tool.isError = event.isError === true;
            yield { content: snapshot(), status: { type: "running" } };
            continue;
          }
          if (type === "RUN_STARTED") {
            yield { status: { type: "running" } };
            continue;
          }
          if (type === "RUN_FINISHED") {
            const outcome = event.outcome as
              | { type?: string; interrupts?: unknown[] }
              | undefined;
            if (outcome?.type === "interrupt" && outcome.interrupts?.length) {
              yield {
                content: snapshot(),
                status: { type: "requires-action", reason: "interrupt" },
                metadata: {
                  custom: { agui: { interrupts: outcome.interrupts } },
                },
              };
              return;
            }
            activeTurn.current = null;
            yield {
              content: snapshot(),
              status: { type: "complete", reason: "unknown" },
            };
            return;
          }
          if (type === "RUN_CANCELLED") {
            activeTurn.current = null;
            yield {
              content: snapshot(),
              status: { type: "incomplete", reason: "cancelled" },
            };
            return;
          }
          if (type === "RUN_ERROR") {
            activeTurn.current = null;
            yield {
              content: snapshot(),
              status: {
                type: "incomplete",
                reason: "error",
                error: String(event.message ?? "Unable to resume the turn"),
              },
            };
            return;
          }
        }
      },
      async append() {
        // Codex persists the native thread; the history adapter restores it.
      },
    }),
    [sessionId, threadId],
  );

  const threadList = useMemo<UseAgUiThreadListAdapter>(
    () => ({
      threadId: threadId ?? "new",
      threads: threads.map((thread) => ({
        id: thread.id,
        title: thread.title,
        status: "regular" as const,
        custom: {
          workspace: thread.workspace,
          updatedAt: thread.updated_at,
        },
      })),
      onSwitchToNewThread() {
        onNewThread();
      },
      async onSwitchToThread(nextThreadId) {
        const thread = await api.readThread(sessionId, nextThreadId);
        onOpenThread(nextThreadId);
        const repository = ExportedMessageRepository.fromArray(
          restoreMessages(thread.messages),
        );
        return {
          messages: repository.messages.map((item) => item.message),
        };
      },
      async onDelete(deletedThreadId) {
        if (!window.confirm("Delete this thread permanently?")) return;
        await api.deleteThread(sessionId, deletedThreadId);
        onThreadDeleted(deletedThreadId);
      },
    }),
    [
      onNewThread,
      onOpenThread,
      onThreadDeleted,
      sessionId,
      threadId,
      threads,
    ],
  );

  const runtime = useAgUiRuntime({
    agent,
    showThinking: false,
    adapters: {
      history,
      threadList,
      attachments: attachmentAdapter,
    },
    onCancel: () => {
      const active = activeTurn.current;
      if (!active) return;
      void api
        .interruptTurn(sessionId, active.threadId, active.turnId)
        .catch((cause: unknown) =>
          setError(
            cause instanceof Error ? cause.message : "Failed to stop the turn",
          ),
        );
    },
    onError: (cause) => setError(cause.message),
  });
  runtimeRef.current = runtime;

  const steer = useCallback(
    async (prompt: string) => {
      const active = activeTurn.current;
      const normalized = prompt.trim();
      if (!normalized) return;
      if (!active) {
        setQueuedMessages((current) => [...current, normalized]);
        return;
      }
      try {
        await api.steerTurn(
          sessionId,
          active.threadId,
          active.turnId,
          normalized,
        );
      } catch {
        setQueuedMessages((current) => [...current, normalized]);
      }
    },
    [sessionId, setQueuedMessages],
  );

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RunControlsProvider
        queuedMessages={queuedMessages}
        setQueuedMessages={setQueuedMessages}
        steer={steer}
      >
        {children}
        {error && (
          <button
            className="fixed right-4 bottom-4 z-50 max-w-md rounded-lg border border-destructive/25 bg-background px-3 py-2 text-left text-xs text-destructive shadow-lg"
            onClick={() => setError(null)}
          >
            {error}
          </button>
        )}
      </RunControlsProvider>
    </AssistantRuntimeProvider>
  );
}
