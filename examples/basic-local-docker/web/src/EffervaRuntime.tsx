import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import {
  HttpAgent,
  type BaseEvent,
  type Message,
  type RunAgentInput,
} from "@ag-ui/client";
import {
  CopilotChatConfigurationProvider,
  CopilotChatInput,
  CopilotChatReasoningMessage,
  CopilotChatToolCallsView,
  CopilotChatView,
  CopilotKitProvider,
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
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
    const {
      process,
      processDurationMs,
      toolCalls,
      ...assistant
    } = message;
    const processMessage: EffervaProcessMessage = {
      id: `${message.id}:process`,
      role: "reasoning",
      content: reasoningText || " ",
      process,
      processDurationMs,
      toolCalls,
    };
    return [processMessage, assistant as Message];
  });
}

type AssistantMessage = Extract<Message, { role: "assistant" }>;
type EffervaProcessMessage = Extract<Message, { role: "reasoning" }> & {
  process?: AgUiMessage["process"];
  processDurationMs?: number;
  toolCalls?: AssistantMessage["toolCalls"];
};

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

function EffervaReasoningMessage({
  message,
  messages = [],
  isRunning,
  header: _header,
  contentView: _contentView,
  toggle: _toggle,
  children: _children,
  ...props
}: EffervaReasoningMessageProps) {
  const { agent } = useAgent({
    agentId: AGENT_ID,
    updates: [UseAgentUpdate.OnStateChanged],
  });
  const processMessage = message as EffervaProcessMessage;
  const messageIndex = messages.findIndex((item) => item.id === message.id);
  let lastUserIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const streaming = Boolean(isRunning && messageIndex > lastUserIndex);
  const fallbackStartedAtRef = useRef(Date.now());
  const state =
    typeof agent.state === "object" && agent.state
      ? (agent.state as Record<string, unknown>)
      : {};
  const startedAt =
    timestampMs(state.startedAt) ?? fallbackStartedAtRef.current;
  const [now, setNow] = useState(Date.now());
  const [isOpen, setIsOpen] = useState(streaming);
  const userToggledRef = useRef(false);

  useEffect(() => {
    if (!streaming) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [streaming]);

  useEffect(() => {
    if (streaming) {
      userToggledRef.current = false;
      setIsOpen(true);
    } else if (!userToggledRef.current) {
      setIsOpen(false);
    }
  }, [streaming]);

  const durationMs = streaming
    ? Math.max(0, now - startedAt)
    : Math.max(0, processMessage.processDurationMs ?? 0);
  const process =
    processMessage.process ??
    (message.content.trim()
      ? [{ type: "reasoning" as const, text: message.content }]
      : []);
  const hasContent = process.length > 0;
  const label = `${streaming ? "处理中" : "已处理"} ${formatElapsed(durationMs)}`;

  return (
    <div
      className="my-1"
      data-message-id={message.id}
      {...props}
    >
      <CopilotChatReasoningMessage.Header
        isOpen={isOpen}
        label={label}
        hasContent={hasContent}
        isStreaming={streaming}
        onClick={
          hasContent
            ? () => {
                userToggledRef.current = true;
                setIsOpen((current) => !current);
              }
            : undefined
        }
      />
      <CopilotChatReasoningMessage.Toggle isOpen={isOpen}>
        <div className="border-l pl-3">
          {process.map((part, index) => {
            if (part.type === "reasoning") {
              return (
                <CopilotChatReasoningMessage.Content
                  key={`${message.id}:reasoning:${index}`}
                  hasContent={Boolean(part.text.trim())}
                  isStreaming={streaming && index === process.length - 1}
                >
                  {part.text}
                </CopilotChatReasoningMessage.Content>
              );
            }
            const toolCall = processMessage.toolCalls?.find(
              (candidate) => candidate.id === part.toolCallId,
            );
            if (!toolCall) return null;
            const toolMessage: AssistantMessage = {
              id: `${message.id}:tool:${toolCall.id}`,
              role: "assistant",
              content: "",
              toolCalls: [toolCall],
            };
            return (
              <CopilotChatToolCallsView
                key={`${message.id}:tool:${toolCall.id}`}
                message={toolMessage}
                messages={messages}
              />
            );
          })}
        </div>
      </CopilotChatReasoningMessage.Toggle>
    </div>
  );
}

type RuntimeContextValue = {
  sessionId: string;
  workspace?: string | null;
  loading: boolean;
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
  onRunSettled,
  onThreadNotFound,
}: EffervaRuntimeProps) {
  const [loading, setLoading] = useState(Boolean(threadId));
  const [error, setError] = useState<string | null>(null);
  const settingsRef = useRef({ model, reasoningEffort });
  const onThreadCreatedRef = useRef(onThreadCreated);
  const onRunSettledRef = useRef(onRunSettled);
  const onThreadNotFoundRef = useRef(onThreadNotFound);
  const createdThreadIdRef = useRef<string | null>(null);
  settingsRef.current = { model, reasoningEffort };
  onThreadCreatedRef.current = onThreadCreated;
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

  useEffect(() => {
    const subscription = agent.subscribe({
      onRawEvent({ event, agent: current }) {
        const raw = event.event as
          | {
              method?: string;
              params?: { thread?: ThreadSummary; turnId?: string };
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
      },
      async onRunFinalized({ agent: current, state }) {
        const settledThreadId = current.threadId;
        if (!settledThreadId || settledThreadId === "new") return;
        onRunSettledRef.current(settledThreadId);
        try {
          const detail = await api.readThread(sessionId, settledThreadId);
          if (current.threadId !== settledThreadId) return;
          setError(detail.last_run_error ?? null);
          return {
            messages: restoreMessages(detail.messages),
            state: {
              ...(typeof state === "object" && state ? state : {}),
              threadId: settledThreadId,
              turnId: null,
              startedAt: null,
              status: "idle",
            },
          };
        } catch (cause) {
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
    const controller = new AbortController();
    let cancelled = false;

    if (
      agent.isRunning &&
      createdThreadIdRef.current === desiredThreadId &&
      agent.threadId === desiredThreadId
    ) {
      setLoading(false);
      return () => controller.abort();
    }

    const load = async () => {
      setError(null);
      if (desiredThreadId === "new") {
        if (agent.isRunning) await agent.detachActiveRun();
        createdThreadIdRef.current = null;
        agent.threadId = "new";
        agent.setMessages([]);
        agent.setState({});
        setLoading(false);
        return;
      }

      setLoading(true);
      if (agent.isRunning) await agent.detachActiveRun();
      try {
        const detail = await api.readThread(sessionId, desiredThreadId);
        if (cancelled) return;
        agent.threadId = desiredThreadId;
        agent.setMessages(restoreMessages(detail.messages));
        agent.setState({
          threadId: desiredThreadId,
          turnId: detail.active_turn_id ?? null,
          startedAt: detail.active_turn_started_at ?? null,
          status: detail.active_turn_id ? "running" : "idle",
          activities: {},
        });
        setError(detail.last_run_error ?? null);
        setLoading(false);
        if (detail.active_turn_id) {
          agent.setResumeSource({
            sessionId,
            threadId: desiredThreadId,
            turnId: detail.active_turn_id,
            signal: controller.signal,
          });
          void agent.connectAgent().catch((cause: unknown) => {
            if (!cancelled) {
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Failed to resume the active turn",
              );
            }
          });
        }
      } catch (cause) {
        if (cancelled) return;
        setLoading(false);
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
      cancelled = true;
      controller.abort();
    };
  }, [agent, sessionId, threadId]);

  const agents = useMemo(() => ({ [AGENT_ID]: agent }), [agent]);
  const context = useMemo<RuntimeContextValue>(
    () => ({
      loading,
      error,
      sessionId,
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
      loading,
      model,
      models,
      onModelChange,
      onReasoningEffortChange,
      reasoningEffort,
      sessionId,
      skills,
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

export function EffervaChat() {
  const runtime = useContext(RuntimeContext);
  if (!runtime) throw new Error("EffervaChat must be inside EffervaRuntime");
  const { agent } = useAgent({ agentId: AGENT_ID, updates: AGENT_UPDATES });
  const { copilotkit } = useCopilotKit();
  const [input, setInput] = useState("");
  const [queued, setQueued] = useState<string[]>([]);
  const [fileOptions, setFileOptions] = useState<
    Array<{ value: string; label: string; description: string }>
  >([]);
  const dispatchingRef = useRef(false);

  const send = useCallback(
    async (value: string) => {
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
    [agent, copilotkit],
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
    <div className="relative h-full min-h-0">
      <CopilotChatView
        className="h-full"
        messages={[...agent.messages]}
        isRunning={agent.isRunning}
        inputValue={input}
        onInputChange={setInput}
        onSubmitMessage={(value) => void send(value)}
        onStop={stop}
        input={{ toolsMenu, addMenuButton: ComposerAddMenuButton }}
        messageView={{
          reasoningMessage:
            EffervaReasoningMessage as typeof CopilotChatReasoningMessage,
        }}
        welcomeScreen={agent.messages.length === 0}
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
        <div className="absolute inset-0 grid place-items-center bg-background/75 text-sm text-muted-foreground">
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
