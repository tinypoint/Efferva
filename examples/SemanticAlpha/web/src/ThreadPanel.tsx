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

import { cn } from "@/lib/utils";

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
  live?: boolean;
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
    <details className="mb-3 border-l-2 border-[#cfd7cf] pl-3">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-[#657067] select-none [&::-webkit-details-marker]:hidden">
        <span>{formatDuration(message.processDurationMs)}</span>
        <span className="font-normal text-[#929890]">
          {message.process.length} 个步骤
        </span>
      </summary>
      <div className="pt-3">
        {message.process.map((part, index) => {
          if (part.type === "reasoning") {
            return (
              <div
                key={`${message.id}:reasoning:${index}`}
                className="rich-text rich-text-sm mt-3 text-[#697169] first:mt-0"
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
              className="mt-3 rounded-lg border border-[#e1e2dc] bg-[#fafaf7] first:mt-0"
            >
              <summary className="flex min-h-9 cursor-pointer list-none items-center gap-2 px-2.5 py-2 text-xs font-semibold text-[#657067] select-none [&::-webkit-details-marker]:hidden">
                <Wrench aria-hidden="true" className="size-3.5" />
                <span>{toolName}</span>
                {result?.isError ? (
                  <span className="ml-auto text-[0.68rem] text-[#a6382d]">
                    失败
                  </span>
                ) : null}
              </summary>
              <div className="border-t border-[#e1e2dc] p-3">
                {call?.function?.arguments ? (
                  <>
                    <div className="my-1 text-[0.68rem] font-semibold text-[#747a72] uppercase">
                      输入
                    </div>
                    <pre className="mb-3 max-h-96 overflow-auto rounded-md bg-[#172018] p-2.5 font-mono text-[0.68rem] leading-relaxed whitespace-pre-wrap text-[#dfe9e1]">
                      {formatArguments(call.function.arguments)}
                    </pre>
                  </>
                ) : null}
                {result ? (
                  <>
                    <div className="my-1 text-[0.68rem] font-semibold text-[#747a72] uppercase">
                      输出
                    </div>
                    <pre className="max-h-96 overflow-auto rounded-md bg-[#172018] p-2.5 font-mono text-[0.68rem] leading-relaxed whitespace-pre-wrap text-[#dfe9e1]">
                      {contentText(result.content)}
                    </pre>
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
      className={cn(
        "mt-6 [overflow-wrap:anywhere] first:mt-0",
        isUser
          ? "ml-10 rounded-xl bg-[#edf4ee] p-4"
          : "py-1",
      )}
    >
      <div className="mb-2.5 flex items-center gap-1.5 text-xs font-semibold text-[#5f685f] [&_svg]:size-4">
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
        <div className="rich-text rich-text-sm">
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
  live = false,
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
    const threadAgUiPath = `${threadPath}/ag-ui`;
    const loadThread = () => {
      void requestJson<ThreadDetail>(threadAgUiPath, controller.signal)
        .then((nextThread) => {
          setThread(nextThread);
          setError(null);
        })
        .catch((cause: unknown) => {
          if (cause instanceof DOMException && cause.name === "AbortError") return;
          setError(cause instanceof Error ? cause.message : "线程读取失败");
        });
    };
    loadThread();
    const refreshTimer = live ? window.setInterval(loadThread, 3000) : null;

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

    return () => {
      if (refreshTimer !== null) window.clearInterval(refreshTimer);
      controller.abort();
    };
  }, [live, model, reasoningEffort, sessionId, threadId]);

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
    <div className="grid h-full min-h-0 text-[#263128] [grid-template-rows:auto_minmax(0,1fr)_auto]">
      <header className="min-h-20 border-b border-[#deddd5] px-5 py-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="mb-0.5 text-[0.68rem] font-semibold tracking-[0.08em] text-[#747a72] uppercase">
              生成上下文
            </div>
            <h2 className="m-0 text-base font-semibold tracking-tight text-[#172018]">
              生成线程
            </h2>
          </div>
          {thread ? (
            <span className="shrink-0 text-xs text-[#747a72]">
              {conversation.length} 条对话 · {toolResults.size} 次工具
            </span>
          ) : null}
        </div>
        {metadata ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span
              className="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-[#e1e2dc] bg-[#fafaf7] px-2 py-1 text-[0.68rem] leading-tight text-[#475148]"
              title={sessionId}
            >
              <span className="font-semibold text-[#8a9189]">Session</span>
              {metadata.sessionName}
            </span>
            <span
              className="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-[#e1e2dc] bg-[#fafaf7] px-2 py-1 text-[0.68rem] leading-tight text-[#475148]"
              title={metadata.model}
            >
              <span className="font-semibold text-[#8a9189]">模型</span>
              {metadata.modelLabel}
            </span>
            <span className="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-[#e1e2dc] bg-[#fafaf7] px-2 py-1 text-[0.68rem] leading-tight text-[#475148]">
              <span className="font-semibold text-[#8a9189]">推理</span>
              {metadata.reasoningEffort}
            </span>
          </div>
        ) : null}
      </header>

      <div className="min-h-0 overflow-y-auto p-5">
        {!thread && !error ? (
          <div className="flex h-full min-h-40 items-center justify-center gap-2 p-8 text-center text-sm text-[#747a72]">
            <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
            正在读取生成线程…
          </div>
        ) : null}
        {error ? (
          <div className="flex h-full min-h-40 items-center justify-center p-8 text-center text-sm text-[#a6382d]">
            {error}
          </div>
        ) : null}
        {thread && conversation.length === 0 ? (
          <div className="flex h-full min-h-40 items-center justify-center p-8 text-center text-sm text-[#747a72]">
            这个线程还没有消息。
          </div>
        ) : null}
        {conversation.map((message) => (
          <MessageView
            key={message.id}
            message={message}
            toolResults={toolResults}
          />
        ))}
      </div>

      <footer className="flex min-w-0 items-center gap-2 border-t border-[#deddd5] px-5 py-3 font-mono text-[0.65rem] text-[#929890]">
        <Terminal aria-hidden="true" className="size-3 shrink-0" />
        <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" title={threadId}>
          {threadId}
        </span>
      </footer>
    </div>
  );
}
