import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
  type UIEvent,
} from "react";
import {
  HttpAgent,
  type BaseEvent,
  type Message,
  type RunAgentInput,
} from "@ag-ui/client";
import {
  CopilotChatConfigurationProvider,
  CopilotChatAssistantMessage,
  CopilotChatInput,
  CopilotChatMessageView,
  CopilotChatReasoningMessage,
  CopilotChatToolCallsView,
  CopilotChatView,
  CopilotKitProvider,
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
  useDefaultRenderTool,
  type CopilotChatInputProps,
  type ToolsMenuItem,
} from "@copilotkit/react-core/v2";
import { from, type Observable } from "rxjs";

import { api, ApiError } from "./api";
import type {
  AgUiMessage,
  CreateThreadInput,
  ModelOption,
  SkillMetadata,
  ThreadSummary,
} from "./types";

const AGENT_ID = "efferva";
const AGENT_UPDATES = [
  UseAgentUpdate.OnMessagesChanged,
  UseAgentUpdate.OnStateChanged,
  UseAgentUpdate.OnRunStatusChanged,
];

type ResumeSource = {
  sessionId: string;
  threadId: string;
  turnId: string;
  signal: AbortSignal;
};

class EffervaAgent extends HttpAgent {
  private resumeSource?: ResumeSource;

  setResumeSource(source: ResumeSource) {
    this.resumeSource = source;
  }

  protected override connect(_input: RunAgentInput): Observable<BaseEvent> {
    const source = this.resumeSource;
    this.resumeSource = undefined;
    if (!source) return from([]);
    return from(
      api.resumeThread(
        source.sessionId,
        source.threadId,
        source.turnId,
        source.signal,
      ),
    ) as Observable<BaseEvent>;
  }
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
  const incomingById = new Map(incoming.map((message) => [message.id, message]));
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

function mergeVisibleMessages(
  history: Message[],
  live: Message[],
): Message[] {
  const liveById = new Map(live.map((message) => [message.id, message]));
  const historyIds = new Set(history.map((message) => message.id));
  return [
    ...history.map((message) => liveById.get(message.id) ?? message),
    ...live.filter((message) => !historyIds.has(message.id)),
  ];
}

function findChatScrollElement(root: HTMLElement): HTMLElement | null {
  const content = root.querySelector<HTMLElement>(
    '[data-testid="copilot-scroll-content"]',
  );
  let element = content?.parentElement ?? null;
  while (element && element !== root) {
    const overflowY = getComputedStyle(element).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return element;
    element = element.parentElement;
  }
  return null;
}

type AssistantMessage = Extract<Message, { role: "assistant" }>;
type EffervaProcessMessage = Extract<Message, { role: "reasoning" }> & {
  process?: AgUiMessage["process"];
  processDurationMs?: number;
  processTextOffset?: number;
};
type ToolCall = NonNullable<AssistantMessage["toolCalls"]>[number];

type ProcessRenderContextValue = {
  messageId: string;
  messages: Message[];
  process: NonNullable<AgUiMessage["process"]>;
  streaming: boolean;
  toolCalls: Map<string, ToolCall>;
};

const ProcessRenderContext = createContext<ProcessRenderContextValue | null>(
  null,
);

function timestampMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 1_000_000_000_000 ? value * 1000 : value;
  }
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function formatElapsed(durationMs: number): string {
  const seconds = Math.max(1, Math.round(durationMs / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}时${minutes}分${remainder}秒`;
  if (minutes) return `${minutes}分${remainder}秒`;
  return `${remainder}秒`;
}

type EffervaReasoningMessageProps = ComponentProps<
  typeof CopilotChatReasoningMessage
>;

function EffervaProcessContent() {
  const value = useContext(ProcessRenderContext);
  if (!value) return null;

  let lastReasoningIndex = -1;
  for (let index = value.process.length - 1; index >= 0; index -= 1) {
    if (value.process[index]?.type === "reasoning") {
      lastReasoningIndex = index;
      break;
    }
  }

  return value.process.map((part, index) => {
    if (part.type === "reasoning") {
      return (
        <CopilotChatReasoningMessage.Content
          key={`${value.messageId}:reasoning:${index}`}
          hasContent={Boolean(part.text.trim())}
          isStreaming={value.streaming && index === lastReasoningIndex}
        >
          {part.text}
        </CopilotChatReasoningMessage.Content>
      );
    }

    const toolCall = value.toolCalls.get(part.toolCallId);
    if (!toolCall) return null;
    const toolMessage: AssistantMessage = {
      id: `${value.messageId}:tool:${toolCall.id}`,
      role: "assistant",
      content: "",
      toolCalls: [toolCall],
    };
    return (
      <CopilotChatToolCallsView
        key={`${value.messageId}:tool:${toolCall.id}`}
        message={toolMessage}
        messages={value.messages}
      />
    );
  });
}

function EffervaReasoningMessage({
  message,
  messages,
  isRunning: _isRunning,
  header: _header,
  contentView: _contentView,
  toggle: _toggle,
  children: _children,
  ...props
}: EffervaReasoningMessageProps) {
  const { agent } = useAgent({
    agentId: AGENT_ID,
    updates: AGENT_UPDATES,
  });
  const currentMessages = [...agent.messages] as Message[];
  const visibleMessages = messages ? [...messages] : currentMessages;
  const currentMessage =
    currentMessages.find((candidate) => candidate.id === message.id) ??
    message;
  const processMessage = currentMessage as EffervaProcessMessage;
  const messageIndex = currentMessages.findIndex(
    (item) => item.id === message.id,
  );
  let lastUserIndex = -1;
  for (let index = currentMessages.length - 1; index >= 0; index -= 1) {
    if (currentMessages[index]?.role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const streaming = Boolean(
    agent.isRunning && messageIndex > lastUserIndex,
  );
  const fallbackStartedAtRef = useRef(Date.now());
  const state =
    typeof agent.state === "object" && agent.state
      ? (agent.state as Record<string, unknown>)
      : {};
  const startedAt =
    timestampMs(state.startedAt) ?? fallbackStartedAtRef.current;
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!streaming) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [streaming]);

  const durationMs = streaming
    ? Math.max(0, now - startedAt)
    : Math.max(0, processMessage.processDurationMs ?? 0);
  const process = [...(processMessage.process ?? [])];
  if (processMessage.processTextOffset !== undefined) {
    const trailingText = processMessage.content.slice(
      processMessage.processTextOffset,
    );
    if (trailingText.trim()) {
      process.push({ type: "reasoning", text: trailingText });
    }
  } else if (process.length === 0 && processMessage.content.trim()) {
    process.push({ type: "reasoning", text: processMessage.content });
  }
  const hasContent = process.length > 0;
  const label = `${streaming ? "处理中" : "已处理"} ${formatElapsed(durationMs)}`;
  const toolCalls = new Map(
    visibleMessages
      .filter(
        (candidate): candidate is AssistantMessage =>
          candidate.role === "assistant",
      )
      .flatMap((candidate) => candidate.toolCalls ?? [])
      .map((toolCall) => [toolCall.id, toolCall] as const),
  );
  const sdkMessage: EffervaProcessMessage =
    hasContent && !processMessage.content
      ? { ...processMessage, content: " " }
      : processMessage;
  const messagesThroughProcess =
    messageIndex >= 0
      ? currentMessages.slice(0, messageIndex + 1)
      : [
          ...(messages ?? []).filter((candidate) => candidate.id !== message.id),
          sdkMessage,
        ];

  return (
    <ProcessRenderContext.Provider
      value={{
        messageId: message.id,
        messages: visibleMessages,
        process,
        streaming,
        toolCalls,
      }}
    >
      <CopilotChatReasoningMessage
        {...props}
        message={sdkMessage}
        messages={messagesThroughProcess}
        isRunning={streaming}
        header={{ label, isStreaming: streaming }}
        contentView={EffervaProcessContent}
      />
    </ProcessRenderContext.Provider>
  );
}

function HiddenToolCallsView() {
  return null;
}

type EffervaAssistantMessageProps = ComponentProps<
  typeof CopilotChatAssistantMessage
>;

function EffervaAssistantMessage(props: EffervaAssistantMessageProps) {
  const { message, messages = [] } = props;
  const processToolCallIds = new Set(
    messages.flatMap((candidate) =>
      candidate.role === "reasoning"
        ? ((candidate as EffervaProcessMessage).process ?? []).flatMap(
            (part) =>
              part.type === "tool-call" ? [part.toolCallId] : [],
          )
        : [],
    ),
  );
  const toolCallsAreInProcess = Boolean(
    message.toolCalls?.some((toolCall) => processToolCallIds.has(toolCall.id)),
  );

  return (
    <CopilotChatAssistantMessage
      {...props}
      toolCallsView={
        toolCallsAreInProcess ? HiddenToolCallsView : props.toolCallsView
      }
    />
  );
}

type MessageListProps = Parameters<
  NonNullable<ComponentProps<typeof CopilotChatMessageView>["children"]>
>[0];

function EffervaMessageList({
  isRunning,
  messages,
  messageElements,
  interruptElement,
}: MessageListProps) {
  const lastMessage = messages[messages.length - 1];
  const showCursor = isRunning && lastMessage?.role !== "reasoning";
  return (
    <div
      data-copilotkit
      data-testid="copilot-message-list"
      className="copilotKitMessages cpk:flex cpk:flex-col"
    >
      {messageElements}
      {interruptElement}
      {showCursor && (
        <div className="cpk:mt-2">
          <CopilotChatMessageView.Cursor />
        </div>
      )}
    </div>
  );
}

type CompactToolCallProps = {
  name: string;
  parameters: unknown;
  status: "inProgress" | "executing" | "complete";
  result: string | undefined;
};

function CompactToolCall({
  name,
  parameters,
  status,
  result,
}: CompactToolCallProps) {
  const [isOpen, setIsOpen] = useState(false);
  const complete = status === "complete";

  return (
    <div className="pt-1 pb-2 text-sm text-muted-foreground">
      <div className="overflow-hidden rounded-lg border bg-background">
        <button
          type="button"
          className="flex w-full items-center gap-2 px-3 py-2 text-left text-inherit"
          aria-expanded={isOpen}
          onClick={() => setIsOpen((current) => !current)}
        >
          <span
            className={`transition-transform ${isOpen ? "rotate-90" : ""}`}
            aria-hidden="true"
          >
            ›
          </span>
          <code className="min-w-0 flex-1 truncate bg-transparent p-0 text-sm text-inherit">
            {name}
          </code>
          <span>{complete ? "完成" : "运行中"}</span>
        </button>
        {isOpen && (
          <div className="grid gap-2 border-t px-3 py-2 text-inherit">
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words text-sm text-inherit">
              {JSON.stringify(parameters ?? {}, null, 2)}
            </pre>
            {result !== undefined && (
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words border-t pt-2 text-sm text-inherit">
                {result}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

type RuntimeContextValue = {
  sessionId: string;
  threadId: string;
  workspace?: string | null;
  loading: boolean;
  historyMessages: Message[];
  historyRevision: number;
  hasOlderHistory: boolean;
  loadingOlderHistory: boolean;
  loadOlderHistory: () => Promise<boolean>;
  error: string | null;
  clearError: () => void;
  skills: SkillMetadata[];
  models: ModelOption[];
  model: string;
  reasoningEffort: NonNullable<CreateThreadInput["reasoning_effort"]>;
  onModelChange: (model: string) => void;
  onReasoningEffortChange: (
    effort: NonNullable<CreateThreadInput["reasoning_effort"]>,
  ) => void;
};

const RuntimeContext = createContext<RuntimeContextValue | null>(null);

type EffervaRuntimeProps = {
  sessionId: string;
  threadId?: string;
  model?: string;
  reasoningEffort: NonNullable<CreateThreadInput["reasoning_effort"]>;
  models: ModelOption[];
  onModelChange: (model: string) => void;
  onReasoningEffortChange: (
    effort: NonNullable<CreateThreadInput["reasoning_effort"]>,
  ) => void;
  workspace?: string | null;
  skills: SkillMetadata[];
  children: ReactNode;
  onThreadCreated: (thread: ThreadSummary) => void;
  onThreadNameUpdated: (threadId: string, threadName: string) => void;
  onRunSettled: (threadId: string) => void;
  onThreadNotFound: (threadId: string) => void;
};

export function EffervaRuntime({
  sessionId,
  threadId,
  model,
  reasoningEffort,
  models,
  onModelChange,
  onReasoningEffortChange,
  workspace,
  skills,
  children,
  onThreadCreated,
  onThreadNameUpdated,
  onRunSettled,
  onThreadNotFound,
}: EffervaRuntimeProps) {
  const desiredThreadId = threadId ?? "new";
  const [openedThreadId, setOpenedThreadId] = useState<string | null>(
    desiredThreadId === "new" ? "new" : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [historyMessages, setHistoryMessages] = useState<Message[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [loadingOlderHistory, setLoadingOlderHistory] = useState(false);
  const olderHistoryRequestRef = useRef<AbortController | null>(null);
  const navigationEpochRef = useRef(0);
  const desiredThreadIdRef = useRef(desiredThreadId);
  const settingsRef = useRef({ model, reasoningEffort });
  const onThreadCreatedRef = useRef(onThreadCreated);
  const onThreadNameUpdatedRef = useRef(onThreadNameUpdated);
  const onRunSettledRef = useRef(onRunSettled);
  const onThreadNotFoundRef = useRef(onThreadNotFound);
  const createdThreadIdRef = useRef<string | null>(null);
  desiredThreadIdRef.current = desiredThreadId;
  settingsRef.current = { model, reasoningEffort };
  onThreadCreatedRef.current = onThreadCreated;
  onThreadNameUpdatedRef.current = onThreadNameUpdated;
  onRunSettledRef.current = onRunSettled;
  onThreadNotFoundRef.current = onThreadNotFound;

  const agent = useMemo(() => {
    const current = new EffervaAgent({
      agentId: AGENT_ID,
      threadId: threadId ?? "new",
      url: "/agent/api/ag-ui",
    });
    current.use((input, next) => {
      const forwarded =
        typeof input.forwardedProps === "object" && input.forwardedProps
          ? input.forwardedProps
          : {};
      const settings = settingsRef.current;
      return next.run({
        ...input,
        threadId: current.threadId,
        forwardedProps: {
          ...forwarded,
          sessionId,
          ...(settings.model?.trim()
            ? { model: settings.model.trim() }
            : {}),
          reasoningEffort: settings.reasoningEffort,
        },
      });
    });
    return current;
  }, [sessionId]);

  const loadOlderHistory = useCallback(async (): Promise<boolean> => {
    const cursor = historyCursor;
    const currentThreadId = desiredThreadIdRef.current;
    if (
      !cursor ||
      currentThreadId === "new" ||
      olderHistoryRequestRef.current
    ) {
      return false;
    }
    const controller = new AbortController();
    olderHistoryRequestRef.current = controller;
    setLoadingOlderHistory(true);
    try {
      const page = await api.loadThreadHistoryPage(
        sessionId,
        currentThreadId,
        { cursor, signal: controller.signal },
      );
      if (
        controller.signal.aborted ||
        desiredThreadIdRef.current !== currentThreadId
      ) {
        return false;
      }
      const olderMessages = restoreMessages(page.messages);
      setHistoryMessages((current) =>
        prependHistoryMessages(current, olderMessages),
      );
      setHistoryCursor(
        page.next_cursor && page.next_cursor !== cursor
          ? page.next_cursor
          : null,
      );
      if (olderMessages.length > 0) {
        setHistoryRevision((current) => current + 1);
        return true;
      }
      return false;
    } catch (cause) {
      if (
        controller.signal.aborted ||
        (cause instanceof DOMException && cause.name === "AbortError")
      ) {
        return false;
      }
      setError(
        cause instanceof Error
          ? cause.message
          : "Failed to load older messages",
      );
      return false;
    } finally {
      if (olderHistoryRequestRef.current === controller) {
        olderHistoryRequestRef.current = null;
        setLoadingOlderHistory(false);
      }
    }
  }, [historyCursor, sessionId]);

  useEffect(() => {
    let activeRunId: string | null = null;
    const subscription = agent.subscribe({
      onRunStartedEvent({ event }) {
        activeRunId = event.runId;
      },
      onToolCallStartEvent({ event, messages }) {
        const processMessageId = activeRunId
          ? `${activeRunId}:process`
          : null;
        let processIndex = processMessageId
          ? messages.findIndex(
              (message) =>
                message.id === processMessageId &&
                message.role === "reasoning",
            )
          : -1;
        if (processIndex < 0) {
          for (let index = messages.length - 1; index >= 0; index -= 1) {
            const message = messages[index];
            if (message?.role === "user") break;
            if (message?.role === "reasoning") {
              processIndex = index;
              break;
            }
          }
        }

        const nextMessages = messages.map((message) => ({ ...message })) as Message[];
        let processMessage: EffervaProcessMessage;
        if (processIndex < 0) {
          processMessage = {
            id: processMessageId ?? `${event.toolCallId}:process`,
            role: "reasoning",
            content: "",
            process: [],
            processTextOffset: 0,
          };
          nextMessages.push(processMessage);
          processIndex = nextMessages.length - 1;
        } else {
          processMessage = nextMessages[processIndex] as EffervaProcessMessage;
        }

        const process = [...(processMessage.process ?? [])];
        if (
          process.some(
            (part) =>
              part.type === "tool-call" &&
              part.toolCallId === event.toolCallId,
          )
        ) {
          return;
        }
        const content =
          typeof processMessage.content === "string"
            ? processMessage.content
            : "";
        const textOffset = processMessage.processTextOffset ?? 0;
        const pendingText = content.slice(textOffset);
        if (pendingText.trim()) {
          process.push({ type: "reasoning", text: pendingText });
        }
        process.push({
          type: "tool-call",
          toolCallId: event.toolCallId,
        });
        nextMessages[processIndex] = {
          ...processMessage,
          process,
          processTextOffset: content.length,
        } as EffervaProcessMessage;
        return { messages: nextMessages };
      },
      onRawEvent({ event, agent: current }) {
        const raw = event.event as
          | {
              method?: string;
              params?: {
                thread?: ThreadSummary;
                turnId?: string;
                threadId?: string;
                threadName?: string;
              };
            }
          | undefined;
        if (
          raw?.method === "efferva/thread-created" &&
          raw.params?.thread
        ) {
          const created = raw.params.thread;
          createdThreadIdRef.current = created.id;
          current.threadId = created.id;
          onThreadCreatedRef.current(created);
        }
        if (
          raw?.method === "thread/name/updated" &&
          raw.params?.threadId &&
          raw.params.threadName
        ) {
          onThreadNameUpdatedRef.current(
            raw.params.threadId,
            raw.params.threadName,
          );
        }
      },
      async onRunFinalized({ agent: current, state }) {
        const settledThreadId = current.threadId;
        if (!settledThreadId || settledThreadId === "new") return;
        if (desiredThreadIdRef.current !== settledThreadId) return;
        onRunSettledRef.current(settledThreadId);
        try {
          const detail = await api.loadThreadHistoryPage(
            sessionId,
            settledThreadId,
          );
          if (
            current.threadId !== settledThreadId ||
            desiredThreadIdRef.current !== settledThreadId
          ) {
            return;
          }
          const refreshedMessages = restoreMessages(detail.messages);
          setHistoryMessages((current) =>
            mergeHistoryMessages(current, refreshedMessages),
          );
          setHistoryRevision((current) => current + 1);
          setError(detail.last_run_error ?? null);
          return {
            messages: refreshedMessages,
            state: {
              ...(typeof state === "object" && state ? state : {}),
              threadId: settledThreadId,
              turnId: null,
              startedAt: null,
              status: "idle",
            },
          };
        } catch (cause) {
          if (desiredThreadIdRef.current !== settledThreadId) return;
          setError(
            cause instanceof Error
              ? cause.message
              : "Failed to refresh the completed turn",
          );
        }
      },
      onRunFailed({ error: cause }) {
        setError(cause.message);
      },
    });
    return subscription.unsubscribe;
  }, [agent, sessionId]);

  useEffect(() => {
    const desiredThreadId = threadId ?? "new";
    const navigationEpoch = ++navigationEpochRef.current;
    const controller = new AbortController();
    olderHistoryRequestRef.current?.abort();
    olderHistoryRequestRef.current = null;
    setLoadingOlderHistory(false);
    setHistoryMessages([]);
    setHistoryCursor(null);
    setHistoryRevision((current) => current + 1);
    const isCurrentNavigation = () =>
      navigationEpochRef.current === navigationEpoch &&
      !controller.signal.aborted;

    if (
      agent.isRunning &&
      createdThreadIdRef.current === desiredThreadId &&
      agent.threadId === desiredThreadId
    ) {
      setOpenedThreadId(desiredThreadId);
      return () => {
        if (navigationEpochRef.current === navigationEpoch) {
          navigationEpochRef.current += 1;
        }
        controller.abort();
      };
    }

    const load = async () => {
      setError(null);
      if (desiredThreadId === "new") {
        if (agent.isRunning) {
          void agent.detachActiveRun().catch((cause: unknown) => {
            if (isCurrentNavigation()) {
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Failed to leave the active thread",
              );
            }
          });
        }
        createdThreadIdRef.current = null;
        agent.threadId = "new";
        agent.setMessages([]);
        agent.setState({});
        setOpenedThreadId("new");
        return;
      }

      try {
        if (agent.isRunning) await agent.detachActiveRun();
        if (!isCurrentNavigation()) return;
        const detail = await api.loadThreadHistoryPage(
          sessionId,
          desiredThreadId,
          { signal: controller.signal },
        );
        if (!isCurrentNavigation()) return;
        const restoredMessages = restoreMessages(detail.messages);
        agent.threadId = desiredThreadId;
        agent.setMessages(restoredMessages);
        setHistoryMessages(restoredMessages);
        setHistoryCursor(detail.next_cursor ?? null);
        setHistoryRevision((current) => current + 1);
        agent.setState({
          threadId: desiredThreadId,
          turnId: detail.active_turn_id ?? null,
          startedAt: detail.active_turn_started_at ?? null,
          status: detail.active_turn_id ? "running" : "idle",
          activities: {},
        });
        setError(detail.last_run_error ?? null);
        setOpenedThreadId(desiredThreadId);
        if (detail.active_turn_id) {
          agent.setResumeSource({
            sessionId,
            threadId: desiredThreadId,
            turnId: detail.active_turn_id,
            signal: controller.signal,
          });
          void agent.connectAgent().catch((cause: unknown) => {
            if (isCurrentNavigation()) {
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Failed to resume the active turn",
              );
            }
          });
        }
      } catch (cause) {
        if (!isCurrentNavigation()) return;
        setOpenedThreadId(desiredThreadId);
        if (cause instanceof ApiError && cause.status === 404) {
          onThreadNotFoundRef.current(desiredThreadId);
          return;
        }
        setError(
          cause instanceof Error ? cause.message : "Failed to load the thread",
        );
      }
    };
    void load();
    return () => {
      if (navigationEpochRef.current === navigationEpoch) {
        navigationEpochRef.current += 1;
      }
      controller.abort();
    };
  }, [agent, sessionId, threadId]);

  const openingThread =
    desiredThreadId !== "new" && openedThreadId !== desiredThreadId;

  const agents = useMemo(() => ({ [AGENT_ID]: agent }), [agent]);
  const context = useMemo<RuntimeContextValue>(
    () => ({
      loading: openingThread,
      historyMessages,
      historyRevision,
      hasOlderHistory: Boolean(historyCursor),
      loadingOlderHistory,
      loadOlderHistory,
      error,
      sessionId,
      threadId: desiredThreadId,
      workspace,
      clearError: () => setError(null),
      skills,
      models,
      model: model ?? "",
      reasoningEffort,
      onModelChange,
      onReasoningEffortChange,
    }),
    [
      error,
      historyCursor,
      historyMessages,
      historyRevision,
      loadOlderHistory,
      loadingOlderHistory,
      model,
      models,
      onModelChange,
      onReasoningEffortChange,
      reasoningEffort,
      sessionId,
      skills,
      openingThread,
      desiredThreadId,
      workspace,
    ],
  );

  return (
    <CopilotKitProvider agents__unsafe_dev_only={agents} showDevConsole={false}>
      <CopilotChatConfigurationProvider
        agentId={AGENT_ID}
        threadId={threadId ?? "new"}
        hasExplicitThreadId={false}
        labels={{
          chatInputPlaceholder:
            "Send a message… Use @ files, $ skills, or / commands",
          welcomeMessageText: "How can I help you today?",
        }}
      >
        <RuntimeContext.Provider value={context}>
          {children}
        </RuntimeContext.Provider>
      </CopilotChatConfigurationProvider>
    </CopilotKitProvider>
  );
}

type ComposerAddMenuButtonProps = ComponentProps<
  typeof CopilotChatInput.AddMenuButton
>;

function ComposerAddMenuButton(props: ComposerAddMenuButtonProps) {
  const runtime = useContext(RuntimeContext);
  if (!runtime) return <CopilotChatInput.AddMenuButton {...props} />;
  const selectedModel = runtime.models.find(
    (item) => item.model === runtime.model,
  );

  return (
    <div
      className="flex min-w-0 items-center gap-1"
      onClick={(event) => event.stopPropagation()}
    >
      <CopilotChatInput.AddMenuButton {...props} />
      <select
        className="h-8 max-w-40 truncate rounded-md border-0 bg-transparent px-1.5 text-xs font-medium outline-none hover:bg-muted"
        value={runtime.model}
        onChange={(event) => {
          runtime.onModelChange(event.target.value);
        }}
        aria-label="Model"
      >
        {runtime.models.map((item) => (
          <option key={item.id} value={item.model}>
            {item.displayName}
          </option>
        ))}
      </select>
      <select
        className="h-8 max-w-28 truncate rounded-md border-0 bg-transparent px-1.5 text-xs outline-none hover:bg-muted"
        value={runtime.reasoningEffort}
        onChange={(event) =>
          runtime.onReasoningEffortChange(event.target.value)
        }
        aria-label="Reasoning effort"
      >
        {selectedModel?.supportedReasoningEfforts.map((item) => (
          <option key={item.reasoningEffort} value={item.reasoningEffort}>
            {item.reasoningEffort}
          </option>
        ))}
      </select>
    </div>
  );
}

const ComposerTextArea = forwardRef<
  HTMLTextAreaElement,
  ComponentProps<typeof CopilotChatInput.TextArea>
>(function ComposerTextArea(
  {
    onCompositionStart: _onCompositionStart,
    onCompositionEnd: _onCompositionEnd,
    className: _className,
    ...props
  },
  ref,
) {
  return (
    <CopilotChatInput.TextArea
      {...props}
      ref={ref}
      className="cpk:w-full cpk:px-5 cpk:py-3"
    />
  );
});

type ComposerLayoutProps = Parameters<
  NonNullable<CopilotChatInputProps["children"]>
>[0];

function ComposerLayout({
  textArea,
  audioRecorder,
  sendButton,
  startTranscribeButton,
  cancelTranscribeButton,
  finishTranscribeButton,
  addMenuButton,
  disclaimer,
  mode = "input",
  onStartTranscribe,
  onCancelTranscribe,
  onFinishTranscribe,
  positioning = "static",
  keyboardHeight = 0,
  containerRef,
  showDisclaimer = false,
  bottomAnchored = false,
  className,
  style,
}: ComposerLayoutProps) {
  return (
    <div
      data-copilotkit
      ref={containerRef}
      className={[
        "cpk:pointer-events-none cpk:relative cpk:z-20",
        positioning === "absolute" &&
          "cpk:absolute cpk:bottom-0 cpk:left-0 cpk:right-0",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      style={{
        transform:
          keyboardHeight > 0
            ? `translateY(-${keyboardHeight}px)`
            : undefined,
        transition: "transform 0.2s ease-out",
        ...(positioning === "absolute" || bottomAnchored
          ? { paddingBottom: "var(--copilotkit-license-banner-offset, 0px)" }
          : {}),
        ...style,
      }}
    >
      <div className="cpk:max-w-3xl cpk:mx-auto cpk:py-0 cpk:px-4 cpk:@3xl:px-0 cpk:[div[data-sidebar-chat]_&]:px-8 cpk:[div[data-popup-chat]_&]:px-4 cpk:pointer-events-auto">
        <div
          data-testid="copilot-chat-input"
          data-layout="expanded"
          className="copilotKitInput cpk:flex cpk:w-full cpk:flex-col cpk:items-center cpk:justify-center cpk:cursor-text cpk:overflow-visible cpk:bg-clip-padding cpk:contain-inline-size cpk:bg-white cpk:dark:bg-[#303030] cpk:shadow-[0_4px_4px_0_#0000000a,0_0_1px_0_#0000009e] cpk:rounded-[28px]"
          onClick={(event) => {
            const target = event.target;
            if (
              target instanceof Element &&
              target.closest("button, select")
            ) {
              return;
            }
            event.currentTarget.querySelector("textarea")?.focus();
          }}
        >
          <div
            data-layout="expanded"
            className="cpk:grid cpk:w-full cpk:gap-x-3 cpk:gap-y-3 cpk:px-3 cpk:py-2 cpk:grid-cols-[auto_minmax(0,1fr)_auto] cpk:grid-rows-[auto_auto]"
          >
            <div className="cpk:flex cpk:items-center cpk:col-start-1 cpk:row-start-2">
              {addMenuButton}
            </div>
            <div className="cpk:relative cpk:flex cpk:min-w-0 cpk:min-h-[50px] cpk:flex-col cpk:justify-center cpk:col-span-3 cpk:row-start-1">
              {mode === "transcribe" ? audioRecorder : textArea}
            </div>
            <div className="cpk:flex cpk:items-center cpk:justify-end cpk:gap-2 cpk:col-start-3 cpk:row-start-2">
              {mode === "transcribe" ? (
                <>
                  {onCancelTranscribe && cancelTranscribeButton}
                  {onFinishTranscribe && finishTranscribeButton}
                </>
              ) : (
                <>
                  {onStartTranscribe && startTranscribeButton}
                  {sendButton}
                </>
              )}
            </div>
          </div>
        </div>
      </div>
      {showDisclaimer && disclaimer}
    </div>
  );
}

export function EffervaChat() {
  const runtime = useContext(RuntimeContext);
  if (!runtime) throw new Error("EffervaChat must be inside EffervaRuntime");
  useDefaultRenderTool(
    {
      render: (props) => <CompactToolCall {...props} />,
    },
    [],
  );
  const { agent } = useAgent({ agentId: AGENT_ID, updates: AGENT_UPDATES });
  const { copilotkit } = useCopilotKit();
  const [input, setInput] = useState("");
  const [queued, setQueued] = useState<string[]>([]);
  const [fileOptions, setFileOptions] = useState<
    Array<{ value: string; label: string; description: string }>
  >([]);
  const dispatchingRef = useRef(false);
  const chatRootRef = useRef<HTMLDivElement>(null);
  const scrollElementRef = useRef<HTMLElement | null>(null);
  const isAtBottomRef = useRef(true);
  const positionedThreadRef = useRef<string | null>(null);
  const historyAnchorRef = useRef<{
    element: HTMLElement;
    scrollHeight: number;
    scrollTop: number;
    revision: number;
    threadId: string;
  } | null>(null);
  const visibleMessages = runtime.loading
    ? []
    : mergeVisibleMessages(
        runtime.historyMessages,
        [...agent.messages] as Message[],
      );

  useLayoutEffect(() => {
    const anchor = historyAnchorRef.current;
    if (!anchor) return;
    if (anchor.threadId !== runtime.threadId || !anchor.element.isConnected) {
      historyAnchorRef.current = null;
      return;
    }
    if (anchor.revision === runtime.historyRevision) return;
    historyAnchorRef.current = null;
    anchor.element.scrollTop =
      anchor.scrollTop + anchor.element.scrollHeight - anchor.scrollHeight;
  }, [runtime.historyRevision, runtime.threadId]);

  useLayoutEffect(() => {
    if (runtime.loading) return;
    const resolveScrollElement = () => {
      const current = scrollElementRef.current;
      if (current?.isConnected) return current;
      const resolved = chatRootRef.current
        ? findChatScrollElement(chatRootRef.current)
        : null;
      scrollElementRef.current = resolved;
      return resolved;
    };
    const element = resolveScrollElement();
    if (!element) return;
    if (positionedThreadRef.current !== runtime.threadId) {
      positionedThreadRef.current = runtime.threadId;
      historyAnchorRef.current = null;
      element.scrollTop = element.scrollHeight;
      isAtBottomRef.current = true;
      const frame = requestAnimationFrame(() => {
        scrollElementRef.current = null;
        const mountedElement = resolveScrollElement();
        if (mountedElement) {
          mountedElement.scrollTop = mountedElement.scrollHeight;
        }
      });
      return () => cancelAnimationFrame(frame);
    }
    if (isAtBottomRef.current && !historyAnchorRef.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [runtime.loading, runtime.threadId, visibleMessages]);

  const handleHistoryScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const element = event.currentTarget;
      scrollElementRef.current = element;
      isAtBottomRef.current =
        element.scrollHeight - element.scrollTop - element.clientHeight < 10;
      if (
        element.scrollTop > 160 ||
        runtime.loading ||
        runtime.loadingOlderHistory ||
        !runtime.hasOlderHistory ||
        historyAnchorRef.current
      ) {
        return;
      }
      const revision = runtime.historyRevision;
      historyAnchorRef.current = {
        element,
        scrollHeight: element.scrollHeight,
        scrollTop: element.scrollTop,
        revision,
        threadId: runtime.threadId,
      };
      void runtime.loadOlderHistory().then((loaded) => {
        const anchor = historyAnchorRef.current;
        if (
          !loaded &&
          anchor?.revision === revision &&
          anchor.threadId === runtime.threadId
        ) {
          historyAnchorRef.current = null;
        }
      });
    },
    [
      runtime.hasOlderHistory,
      runtime.historyRevision,
      runtime.loadOlderHistory,
      runtime.loading,
      runtime.loadingOlderHistory,
      runtime.threadId,
    ],
  );

  const send = useCallback(
    async (value: string) => {
      if (runtime.loading) return;
      const prompt = value.trim();
      if (!prompt) return;
      if (agent.isRunning) {
        setQueued((current) => [...current, prompt]);
        setInput("");
        return;
      }
      setInput("");
      agent.addMessage({
        id: crypto.randomUUID(),
        role: "user",
        content: prompt,
      });
      await copilotkit.runAgent({ agent });
    },
    [agent, copilotkit, runtime.loading],
  );

  useEffect(() => {
    if (agent.isRunning) {
      dispatchingRef.current = false;
      return;
    }
    if (dispatchingRef.current || queued.length === 0) return;
    dispatchingRef.current = true;
    const next = queued[0]!;
    setQueued((current) => current.slice(1));
    void send(next).finally(() => {
      dispatchingRef.current = false;
    });
  }, [agent.isRunning, queued, send]);

  const appendDirective = useCallback((directive: string) => {
    setInput((current) => `${current}${current && !current.endsWith(" ") ? " " : ""}${directive} `);
  }, []);
  const trigger = useMemo(() => {
    const match = input.match(/(^|\s)([$/@])([^\s]*)$/u);
    if (!match) return null;
    return {
      char: match[2]!,
      query: match[3]!,
      tokenLength: match[2]!.length + match[3]!.length,
    };
  }, [input]);

  useEffect(() => {
    if (trigger?.char !== "@") {
      setFileOptions([]);
      return;
    }
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      void api
        .searchFiles(runtime.sessionId, trigger.query, runtime.workspace ?? undefined)
        .then((files) => {
          if (cancelled) return;
          setFileOptions(
            files.slice(0, 8).map((file) => ({
              value: file.path,
              label: file.file_name,
              description: file.path,
            })),
          );
        })
        .catch(() => {
          if (!cancelled) setFileOptions([]);
        });
    }, 80);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [runtime.sessionId, runtime.workspace, trigger]);

  const triggerOptions = useMemo(() => {
    if (!trigger) return [];
    const query = trigger.query.toLocaleLowerCase();
    if (trigger.char === "$") {
      return runtime.skills
        .filter((skill) => skill.name.toLocaleLowerCase().includes(query))
        .slice(0, 8)
        .map((skill) => ({
          value: skill.name,
          label: `$${skill.name}`,
          description:
            skill.interface?.shortDescription ||
            skill.shortDescription ||
            skill.description,
        }));
    }
    if (trigger.char === "/") {
      return [
        { value: "plan", label: "/plan", description: "Enter Plan mode" },
        { value: "goal", label: "/goal", description: "Manage the thread goal" },
      ].filter((item) => item.value.includes(query));
    }
    return fileOptions;
  }, [fileOptions, runtime.skills, trigger]);

  const selectTriggerOption = useCallback(
    (value: string) => {
      if (!trigger) return;
      setInput(
        (current) =>
          `${current.slice(0, current.length - trigger.tokenLength)}${trigger.char}${value} `,
      );
    },
    [trigger],
  );
  const toolsMenu = useMemo<(ToolsMenuItem | "-")[]>(
    () => [
      {
        label: "Commands",
        items: [
          { label: "/plan", action: () => appendDirective("/plan") },
          { label: "/goal", action: () => appendDirective("/goal") },
        ],
      },
      ...(runtime.skills.length
        ? [
            "-" as const,
            {
              label: "Skills",
              items: runtime.skills.map((skill) => ({
                label: `$${skill.name}`,
                action: () => appendDirective(`$${skill.name}`),
              })),
            },
          ]
        : []),
    ],
    [appendDirective, runtime.skills],
  );

  const stop = useCallback(() => {
    const state =
      typeof agent.state === "object" && agent.state
        ? (agent.state as Record<string, unknown>)
        : {};
    const activeThreadId = String(state.threadId ?? agent.threadId ?? "");
    const turnId = String(state.turnId ?? "");
    if (activeThreadId && activeThreadId !== "new" && turnId) {
      void api.interruptTurn(runtime.sessionId, activeThreadId, turnId);
    }
    agent.abortRun();
  }, [agent, runtime.sessionId]);

  const steer = useCallback(() => {
    const prompt = input.trim();
    if (!prompt) return;
    const state = agent.state as Record<string, unknown>;
    const activeThreadId = String(state.threadId ?? agent.threadId ?? "");
    const turnId = String(state.turnId ?? "");
    if (!activeThreadId || !turnId) return;
    setInput("");
    void api.steerTurn(runtime.sessionId, activeThreadId, turnId, prompt);
  }, [agent, input, runtime.sessionId]);

  return (
    <div ref={chatRootRef} className="relative h-full min-h-0">
      <CopilotChatView
        className="h-full"
        messages={visibleMessages}
        autoScroll="none"
        isRunning={agent.isRunning}
        inputValue={input}
        onInputChange={setInput}
        onSubmitMessage={(value) => void send(value)}
        onStop={stop}
        scrollView={{ onScroll: handleHistoryScroll }}
        input={{
          toolsMenu,
          addMenuButton: ComposerAddMenuButton,
          textArea: ComposerTextArea,
          children: ComposerLayout,
        }}
        messageView={{
          assistantMessage:
            EffervaAssistantMessage as typeof CopilotChatAssistantMessage,
          reasoningMessage:
            EffervaReasoningMessage as typeof CopilotChatReasoningMessage,
          children: EffervaMessageList,
        }}
        welcomeScreen={!runtime.loading && visibleMessages.length === 0}
      />
      {queued.length > 0 && (
        <div className="absolute right-6 bottom-24 left-6 mx-auto max-w-2xl rounded-lg border bg-background/95 px-3 py-2 text-xs shadow-sm">
          Queued: {queued.join(" · ")}
        </div>
      )}
      {trigger && triggerOptions.length > 0 && (
        <div className="absolute right-6 bottom-24 left-6 z-20 mx-auto max-h-72 max-w-2xl overflow-y-auto rounded-xl border bg-background p-1 shadow-lg">
          {triggerOptions.map((option) => (
            <button
              key={`${trigger.char}:${option.value}`}
              type="button"
              className="flex w-full items-start gap-3 rounded-lg px-3 py-2 text-left hover:bg-muted"
              onClick={() => selectTriggerOption(option.value)}
            >
              <span className="text-sm font-medium">{option.label}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                {option.description}
              </span>
            </button>
          ))}
        </div>
      )}
      {agent.isRunning && input.trim() && (
        <div className="absolute right-8 bottom-24 flex gap-2">
          <button
            className="rounded-md border bg-background px-3 py-1.5 text-xs"
            onClick={() => void send(input)}
          >
            Queue
          </button>
          <button
            className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground"
            onClick={steer}
          >
            Steer
          </button>
        </div>
      )}
      {runtime.loading && (
        <div className="absolute inset-0 z-30 grid cursor-progress place-items-center bg-background/75 text-sm text-muted-foreground">
          Opening thread…
        </div>
      )}
      {runtime.error && (
        <button
          className="absolute right-4 bottom-4 max-w-md rounded-lg border border-destructive/25 bg-background px-3 py-2 text-left text-xs text-destructive shadow-lg"
          onClick={runtime.clearError}
        >
          {runtime.error}
        </button>
      )}
    </div>
  );
}
