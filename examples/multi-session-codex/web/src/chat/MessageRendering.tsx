import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
} from "react";
import type { Message } from "@ag-ui/client";
import {
  CopilotChatAssistantMessage,
  CopilotChatMessageView,
  CopilotChatReasoningMessage,
  CopilotChatToolCallsView,
  useAgent,
} from "@copilotkit/react-core/v2";

import { AGENT_ID, AGENT_UPDATES } from "../runtime/agentConfig";
import type { AgUiMessage } from "../types";
import type {
  AssistantMessage,
  EffervaProcessMessage,
  ToolCall,
} from "../runtime/threadHistory";

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
    currentMessages.find((candidate) => candidate.id === message.id) ?? message;
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
  const streaming = Boolean(agent.isRunning && messageIndex > lastUserIndex);
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
          ...(messages ?? []).filter(
            (candidate) => candidate.id !== message.id,
          ),
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
            (part) => (part.type === "tool-call" ? [part.toolCallId] : []),
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

export {
  CompactToolCall,
  EffervaAssistantMessage,
  EffervaMessageList,
  EffervaReasoningMessage,
};
