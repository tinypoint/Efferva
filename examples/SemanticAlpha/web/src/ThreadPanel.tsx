import { useEffect, useMemo, useState } from "react";
import {
  Bot,
  CircleUserRound,
  LoaderCircle,
  Terminal,
  Wrench,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type ToolCall = {
  id: string;
  function?: {
    name?: string;
    arguments?: string;
  };
};

type ProcessPart =
  | { type: "reasoning"; text: string }
  | { type: "tool-call"; toolCallId: string };

type ThreadMessage = {
  id: string;
  role: string;
  content?: unknown;
  name?: string;
  toolCallId?: string;
  isError?: boolean;
  process?: ProcessPart[];
  processDurationMs?: number;
  toolCalls?: ToolCall[];
};

type ThreadDetail = {
  id: string;
  messages: ThreadMessage[];
};

type SessionDetail = {
  id: string;
  name: string;
};

type ModelOption = {
  model: string;
  displayName: string;
  defaultReasoningEffort: string;
  isDefault: boolean;
};

type ExecutionSettings = {
  model?: string | null;
  reasoning_effort?: string | null;
};

type ThreadMetadata = {
  sessionName: string;
  model: string;
  modelLabel: string;
  reasoningEffort: string;
};

type ThreadPanelProps = {
  sessionId: string;
  threadId: string;
  model?: string | null;
  reasoningEffort?: string | null;
};

async function requestJson<T>(
  path: string,
  signal: AbortSignal,
): Promise<T> {
  const response = await fetch(path, { cache: "no-store", signal });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(body?.detail ?? `请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content == null ? "" : String(content);
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (part && typeof part === "object" && "text" in part) {
        const text = (part as { text?: unknown }).text;
        return typeof text === "string" ? text : "";
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function formatDuration(value?: number): string {
  if (!value) return "处理过程";
  const seconds = Math.max(1, Math.round(value / 1000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes ? `已处理 ${minutes}分${remainder}秒` : `已处理 ${seconds}秒`;
}

function formatArguments(value?: string): string {
  if (!value) return "";
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function ProcessView({
  message,
  toolResults,
}: {
  message: ThreadMessage;
  toolResults: Map<string, ThreadMessage>;
}) {
  if (!message.process?.length) return null;
  const calls = new Map(
    (message.toolCalls ?? []).map((toolCall) => [toolCall.id, toolCall]),
  );

  return (
    <details className="thread-process">
      <summary>
        <span>{formatDuration(message.processDurationMs)}</span>
        <span className="thread-process-count">
          {message.process.length} 个步骤
        </span>
      </summary>
      <div className="thread-process-body">
        {message.process.map((part, index) => {
          if (part.type === "reasoning") {
            return (
              <div
                key={`${message.id}:reasoning:${index}`}
                className="thread-reasoning"
              >
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {part.text}
                </ReactMarkdown>
              </div>
            );
          }

          const call = calls.get(part.toolCallId);
          const result = toolResults.get(part.toolCallId);
          const toolName = call?.function?.name ?? result?.name ?? "tool";
          return (
            <details
              key={`${message.id}:tool:${part.toolCallId}`}
              className="thread-tool"
            >
              <summary>
                <Wrench aria-hidden="true" />
                <span>{toolName}</span>
                {result?.isError ? (
                  <span className="thread-tool-error">失败</span>
                ) : null}
              </summary>
              <div className="thread-tool-body">
                {call?.function?.arguments ? (
                  <>
                    <div className="thread-tool-label">输入</div>
                    <pre>{formatArguments(call.function.arguments)}</pre>
                  </>
                ) : null}
                {result ? (
                  <>
                    <div className="thread-tool-label">输出</div>
                    <pre>{contentText(result.content)}</pre>
                  </>
                ) : null}
              </div>
            </details>
          );
        })}
      </div>
    </details>
  );
}

function MessageView({
  message,
  toolResults,
}: {
  message: ThreadMessage;
  toolResults: Map<string, ThreadMessage>;
}) {
  const isUser = message.role === "user";
  const body = contentText(message.content);

  return (
    <section
      className={`thread-message ${isUser ? "thread-message-user" : "thread-message-agent"}`}
    >
      <div className="thread-message-label">
        {isUser ? (
          <CircleUserRound aria-hidden="true" />
        ) : (
          <Bot aria-hidden="true" />
        )}
        <span>{isUser ? "研究请求" : "Agent"}</span>
      </div>
      {!isUser ? (
        <ProcessView message={message} toolResults={toolResults} />
      ) : null}
      {body ? (
        <div className="thread-markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        </div>
      ) : null}
    </section>
  );
}

export function ThreadPanel({
  sessionId,
  threadId,
  model,
  reasoningEffort,
}: ThreadPanelProps) {
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [metadata, setMetadata] = useState<ThreadMetadata | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setThread(null);
    setMetadata(null);
    setError(null);

    const sessionPath = `/agent/api/sessions/${encodeURIComponent(sessionId)}`;
    const threadPath = `${sessionPath}/threads/${encodeURIComponent(threadId)}`;
    void requestJson<ThreadDetail>(threadPath, controller.signal)
      .then(setThread)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "线程读取失败");
      });

    void Promise.all([
      requestJson<SessionDetail>(sessionPath, controller.signal),
      requestJson<ModelOption[]>(`${sessionPath}/models`, controller.signal),
      requestJson<ExecutionSettings>(
        `${threadPath}/settings`,
        controller.signal,
      ).catch(() => null),
    ])
      .then(([session, models, settings]) => {
        const selectedModel =
          models.find((item) => item.model === model) ??
          models.find((item) => item.model === settings?.model) ??
          models.find((item) => item.isDefault) ??
          models[0];
        if (!selectedModel) return;
        setMetadata({
          sessionName: session.name,
          model: selectedModel.model,
          modelLabel: selectedModel.displayName,
          reasoningEffort:
            reasoningEffort ??
            settings?.reasoning_effort ??
            selectedModel.defaultReasoningEffort ??
            "—",
        });
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
      });

    return () => controller.abort();
  }, [model, reasoningEffort, sessionId, threadId]);

  const toolResults = useMemo(
    () =>
      new Map(
        (thread?.messages ?? [])
          .filter((message) => message.role === "tool" && message.toolCallId)
          .map((message) => [message.toolCallId!, message]),
      ),
    [thread],
  );
  const conversation = (thread?.messages ?? []).filter(
    (message) => message.role === "user" || message.role === "assistant",
  );

  return (
    <div className="thread-panel">
      <header className="thread-panel-header">
        <div className="thread-panel-title-row">
          <div>
            <div className="thread-panel-eyebrow">生成上下文</div>
            <h2>生成线程</h2>
          </div>
          {thread ? (
            <span className="thread-panel-count">
              {conversation.length} 条对话 · {toolResults.size} 次工具
            </span>
          ) : null}
        </div>
        {metadata ? (
          <div className="thread-panel-metadata">
            <span className="thread-metadata-chip" title={sessionId}>
              <span className="thread-metadata-label">Session</span>
              {metadata.sessionName}
            </span>
            <span className="thread-metadata-chip" title={metadata.model}>
              <span className="thread-metadata-label">模型</span>
              {metadata.modelLabel}
            </span>
            <span className="thread-metadata-chip">
              <span className="thread-metadata-label">推理</span>
              {metadata.reasoningEffort}
            </span>
          </div>
        ) : null}
      </header>

      <div className="thread-panel-content">
        {!thread && !error ? (
          <div className="thread-panel-state">
            <LoaderCircle className="thread-spinner" aria-hidden="true" />
            正在读取生成线程…
          </div>
        ) : null}
        {error ? (
          <div className="thread-panel-state thread-panel-error">{error}</div>
        ) : null}
        {thread && conversation.length === 0 ? (
          <div className="thread-panel-state">这个线程还没有消息。</div>
        ) : null}
        {conversation.map((message) => (
          <MessageView
            key={message.id}
            message={message}
            toolResults={toolResults}
          />
        ))}
      </div>

      <footer className="thread-panel-footer">
        <Terminal aria-hidden="true" />
        <span title={threadId}>{threadId}</span>
      </footer>
    </div>
  );
}
