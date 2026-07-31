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
  ExportedMessageRepository,
  type ChatModelRunResult,
  type ThreadHistoryAdapter,
} from "@assistant-ui/react";
import {
  fromAgUiMessages,
  useAgUiRuntime,
  type UseAgUiThreadListAdapter,
} from "@assistant-ui/react-ag-ui";
import { finalize, tap } from "rxjs";

import { api } from "./api";
import { RunControlsProvider } from "./RunControls";
import type { CreateThreadInput, ThreadSummary } from "./types";

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
  queuedMessages,
  setQueuedMessages,
}: EffervaRuntimeProps) {
  const [error, setError] = useState<string | null>(null);
  const activeTurn = useRef<{ threadId: string; turnId: string } | null>(null);

  const agent = useMemo(() => {
    let pendingCreatedThread: ThreadSummary | undefined;
    let createdThread: ThreadSummary | undefined;
    let notified = false;
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
            createdThread = pendingCreatedThread;
            activeTurn.current = {
              threadId: activeThreadId,
              turnId: raw.event.params.turnId,
            };
          }
          if (
            raw.type === "RUN_FINISHED" ||
            raw.type === "RUN_CANCELLED" ||
            raw.type === "RUN_ERROR"
          ) {
            activeTurn.current = null;
          }
        }),
        finalize(() => {
          if (createdThread && !notified) {
            notified = true;
            onThreadCreated(createdThread);
          }
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
          fromAgUiMessages(thread.messages, { showThinking: false }),
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
        const partOrder: string[] = [];
        const textByPart = new Map<string, string>();
        const snapshot = (): ChatModelRunResult["content"] =>
          partOrder.map((id) => ({
            type: "text" as const,
            text: textByPart.get(id) ?? "",
          }));
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
              partOrder.push(messageId);
              textByPart.set(messageId, "");
            }
            continue;
          }
          if (type === "TEXT_MESSAGE_CONTENT") {
            const messageId = String(event.messageId ?? "");
            if (!textByPart.has(messageId)) partOrder.push(messageId);
            textByPart.set(
              messageId,
              `${textByPart.get(messageId) ?? ""}${String(event.delta ?? "")}`,
            );
            yield { content: snapshot(), status: { type: "running" } };
            continue;
          }
          if (type === "RUN_STARTED") {
            yield { status: { type: "running" } };
            continue;
          }
          if (type === "RUN_FINISHED") {
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
          fromAgUiMessages(thread.messages, { showThinking: false }),
        );
        return {
          messages: repository.messages.map((item) => item.message),
        };
      },
    }),
    [onNewThread, onOpenThread, sessionId, threadId, threads],
  );

  const runtime = useAgUiRuntime({
    agent,
    showThinking: false,
    adapters: { history, threadList },
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
