"use client";

import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from "@/components/assistant-ui/attachment";
import { ThreadFollowupSuggestions } from "@/components/assistant-ui/follow-up-suggestions";
import { MarkdownText } from "@/components/assistant-ui/markdown-text";
import {
  Reasoning,
  ReasoningContent,
  ReasoningRoot,
  ReasoningText,
  ReasoningTrigger,
} from "@/components/assistant-ui/reasoning";
import { ToolFallback } from "@/components/assistant-ui/tool-fallback";
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from "@/components/assistant-ui/tool-group";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useRunControls } from "@/RunControls";
import {
  ActionBarMorePrimitive,
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  BranchPickerPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartComponent,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  useAgUiInterrupts,
  useAgUiState,
  useAgUiSubmitInterruptResponses,
  type AgUiInterrupt,
} from "@assistant-ui/react-ag-ui";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  CornerDownLeftIcon,
  DownloadIcon,
  ListPlusIcon,
  MicIcon,
  MoreHorizontalIcon,
  PencilIcon,
  RefreshCwIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import {
  createContext,
  useContext,
  useState,
  type ComponentType,
  type FC,
  type KeyboardEvent,
  type PropsWithChildren,
} from "react";

export type ThreadGroupPart = MessagePrimitive.GroupedParts.GroupPart;

/**
 * Optional component overrides for the thread. `AssistantMessage` and
 * `Welcome` replace whole sections; the remaining slots override how the
 * assistant message renders tool calls and part groups. Tool UIs registered
 * by name (toolkit `render`, `useAssistantDataUI`) take precedence over
 * `ToolFallback`.
 */
export type ThreadComponents = {
  AssistantMessage?: ComponentType | undefined;
  Welcome?: ComponentType | undefined;
  ComposerControls?: ComponentType | undefined;
  ComposerTriggers?: ComponentType | undefined;
  ToolFallback?: ToolCallMessagePartComponent | undefined;
  ToolGroup?:
    | ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
    | undefined;
  ReasoningGroup?:
    | ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
    | undefined;
};

export type ThreadProps = {
  components?: ThreadComponents | undefined;
};

const EMPTY_COMPONENTS: ThreadComponents = {};

const ThreadComponentsContext =
  createContext<ThreadComponents>(EMPTY_COMPONENTS);

// Startup exposes a loading placeholder thread; treat it as a new chat so
// the composer mounts centered. Loads after startup keep the docked layout.
const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  (!s.thread.isLoading || s.threads.isLoading);

export const Thread: FC<ThreadProps> = ({ components = EMPTY_COMPONENTS }) => {
  const isEmpty = useAuiState(isNewChatView);

  return (
    <ThreadComponentsContext.Provider value={components}>
      <ThreadRoot isEmpty={isEmpty} />
    </ThreadComponentsContext.Provider>
  );
};

const ThreadRoot: FC<{ isEmpty: boolean }> = ({ isEmpty }) => {
  const { Welcome = ThreadWelcome } = useContext(ThreadComponentsContext);

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background @container flex h-full flex-col"
      style={{
        ["--thread-max-width" as string]: "44rem",
        ["--composer-bg" as string]:
          "color-mix(in oklab, var(--color-muted) 30%, var(--color-background))",
        ["--composer-radius" as string]: "1.5rem",
        ["--composer-padding" as string]: "8px",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
            isEmpty && "justify-center",
          )}
        >
          <AuiIf condition={isNewChatView}>
            <Welcome />
          </AuiIf>

          <div
            data-slot="aui_message-group"
            className="mb-14 flex flex-col gap-y-6 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
          </div>

          <ThreadPrimitive.ViewportFooter
            className={cn(
              "aui-thread-viewport-footer bg-background flex flex-col gap-4 overflow-visible pb-4 md:pb-6",
              !isEmpty &&
                "sticky bottom-0 mt-auto rounded-t-(--composer-radius)",
            )}
          >
            <ThreadScrollToBottom />
            <RunActivityBar />
            <InterruptPanel />
            <ThreadFollowupSuggestions />
            <Composer />
            <AuiIf condition={(s) => isNewChatView(s) && s.composer.isEmpty}>
              <ThreadSuggestions />
            </AuiIf>
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

type InterruptMetadata = {
  method?: string;
  params?: Record<string, unknown>;
};

type EffervaAgUiState = {
  activities?: Record<string, Record<string, unknown>>;
};

const RunActivityBar: FC = () => {
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const state = useAgUiState<EffervaAgUiState>();
  const activities = state?.activities ?? {};
  const notice = activities.error ?? activities.notice;
  const usage = activities.usage;
  const plan = activities.plan;
  const labels = [
    plan ? "计划已更新" : "",
    activities.diff || activities["file-change"] ? "文件变更已更新" : "",
    usage ? "用量已更新" : "",
    notice
      ? String(notice.message ?? notice.error ?? notice.method ?? "Codex 提示")
      : "",
  ].filter(Boolean);
  if (!isRunning || labels.length === 0) return null;
  return (
    <div className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 px-2 text-xs">
      {labels.map((label) => (
        <span key={label}>{label}</span>
      ))}
    </div>
  );
};

const InterruptPanel: FC = () => {
  const interrupts = useAgUiInterrupts();
  return (
    <>
      {interrupts.map((interrupt) => (
        <InterruptCard key={interrupt.id} interrupt={interrupt} />
      ))}
    </>
  );
};

const InterruptCard: FC<{ interrupt: AgUiInterrupt }> = ({ interrupt }) => {
  const submit = useAgUiSubmitInterruptResponses();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [jsonInput, setJsonInput] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const metadata = (interrupt.metadata ?? {}) as InterruptMetadata;
  const method = metadata.method ?? "";
  const params = metadata.params ?? {};
  const questions = Array.isArray(params.questions)
    ? (params.questions as Array<Record<string, unknown>>)
    : [];

  const respond = async (payload: Record<string, unknown> | null) => {
    setSubmitting(true);
    try {
      await submit([
        payload === null
          ? { interruptId: interrupt.id, status: "cancelled" }
          : { interruptId: interrupt.id, status: "resolved", payload },
      ]);
    } finally {
      setSubmitting(false);
    }
  };

  const approval = method.endsWith("requestApproval");
  const userInput = method === "item/tool/requestUserInput";
  const elicitation = method === "mcpServer/elicitation/request";

  return (
    <div className="border-border bg-background rounded-xl border p-4 shadow-sm">
      <div className="text-sm font-medium">{interrupt.message}</div>
      {typeof params.command === "string" && (
        <pre className="bg-muted mt-3 overflow-x-auto rounded-md p-3 text-xs">
          {params.command}
        </pre>
      )}
      {userInput && (
        <div className="mt-3 flex flex-col gap-3">
          {questions.map((question) => {
            const id = String(question.id ?? "");
            const options = Array.isArray(question.options)
              ? (question.options as Array<Record<string, unknown>>)
              : [];
            return (
              <label key={id} className="flex flex-col gap-1.5 text-sm">
                <span>{String(question.question ?? question.header ?? id)}</span>
                {options.length > 0 ? (
                  <select
                    className="border-input bg-background h-9 rounded-md border px-3"
                    value={answers[id] ?? ""}
                    onChange={(event) =>
                      setAnswers((current) => ({
                        ...current,
                        [id]: event.target.value,
                      }))
                    }
                  >
                    <option value="">请选择</option>
                    {options.map((option) => (
                      <option
                        key={String(option.label ?? "")}
                        value={String(option.label ?? "")}
                      >
                        {String(option.label ?? "")}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type={question.isSecret ? "password" : "text"}
                    className="border-input bg-background h-9 rounded-md border px-3"
                    value={answers[id] ?? ""}
                    onChange={(event) =>
                      setAnswers((current) => ({
                        ...current,
                        [id]: event.target.value,
                      }))
                    }
                  />
                )}
              </label>
            );
          })}
        </div>
      )}
      {elicitation && params.mode !== "url" && (
        <textarea
          className="border-input bg-background mt-3 min-h-24 w-full rounded-md border p-3 font-mono text-xs"
          value={jsonInput}
          onChange={(event) => setJsonInput(event.target.value)}
        />
      )}
      {params.mode === "url" && typeof params.url === "string" && (
        <a
          className="mt-3 block text-sm underline"
          href={params.url}
          target="_blank"
          rel="noreferrer"
        >
          打开授权页面
        </a>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          disabled={submitting}
          onClick={() => void respond(null)}
        >
          拒绝
        </Button>
        <Button
          type="button"
          disabled={submitting}
          onClick={() => {
            if (userInput) {
              void respond({
                answers: Object.fromEntries(
                  questions.map((question) => {
                    const id = String(question.id ?? "");
                    return [id, { answers: [answers[id] ?? ""] }];
                  }),
                ),
              });
              return;
            }
            if (elicitation) {
              let content: unknown = null;
              try {
                content = params.mode === "url" ? null : JSON.parse(jsonInput);
              } catch {
                return;
              }
              void respond({ action: "accept", content, _meta: null });
              return;
            }
            if (method === "item/permissions/requestApproval") {
              void respond({ permissions: params.permissions ?? {}, scope: "turn" });
              return;
            }
            if (approval) void respond({ decision: "accept" });
          }}
        >
          允许并继续
        </Button>
      </div>
    </div>
  );
};

const ThreadMessage: FC = () => {
  const { AssistantMessage: AssistantMessageComponent = AssistantMessage } =
    useContext(ThreadComponentsContext);
  const role = useAuiState((s) => s.message.role);
  const isEditing = useAuiState((s) => s.message.composer.isEditing);

  if (isEditing) return <EditComposer />;
  if (role === "user") return <UserMessage />;
  return <AssistantMessageComponent />;
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom render={<TooltipIconButton tooltip="Scroll to bottom" variant="outline" className="aui-thread-scroll-to-bottom dark:border-border dark:bg-background dark:hover:bg-accent absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible" />}><ArrowDownIcon /></ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: FC = () => {
  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col items-center px-4 text-center">
      <h1 className="aui-thread-welcome-message-inner fade-in slide-in-from-bottom-1 animate-in fill-mode-both text-2xl font-semibold duration-200">
        How can I help you today?
      </h1>
    </div>
  );
};

const ThreadSuggestions: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestions flex w-full flex-wrap items-center justify-center gap-2 px-4">
      <ThreadPrimitive.Suggestions>
        {() => <ThreadSuggestionItem />}
      </ThreadPrimitive.Suggestions>
    </div>
  );
};

const ThreadSuggestionItem: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestion-display fade-in slide-in-from-bottom-2 animate-in fill-mode-both duration-200">
      <SuggestionPrimitive.Trigger send render={<Button variant="ghost" className="aui-thread-welcome-suggestion text-foreground hover:bg-muted border-border/60 h-auto gap-1.5 rounded-full border px-3.5 py-1.5 text-sm font-normal whitespace-nowrap transition-colors" />}><SuggestionPrimitive.Title className="aui-thread-welcome-suggestion-text-1" /><SuggestionPrimitive.Description className="aui-thread-welcome-suggestion-text-2 empty:hidden" /></SuggestionPrimitive.Trigger>
    </div>
  );
};

const Composer: FC = () => {
  const { ComposerTriggers } = useContext(ThreadComponentsContext);
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const runControls = useRunControls();

  const takeComposerText = () => {
    const text = aui.composer.getState().text.trim();
    if (text) aui.composer.setText("");
    return text;
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      !isRunning ||
      event.key !== "Enter" ||
      event.nativeEvent.isComposing ||
      event.currentTarget.getAttribute("aria-expanded") === "true"
    ) {
      return;
    }
    if (event.shiftKey && !(event.metaKey || event.ctrlKey)) return;
    event.preventDefault();
    event.stopPropagation();
    const text = takeComposerText();
    if (!text) return;
    if (event.shiftKey && (event.metaKey || event.ctrlKey)) {
      void runControls.steer(text);
    } else {
      runControls.enqueue(text);
    }
  };

  return (
    <ComposerPrimitive.Unstable_TriggerPopoverRoot>
      <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
        {runControls.queuedMessages.length > 0 && (
          <div className="mb-2 flex flex-col gap-1.5">
            {runControls.queuedMessages.map((message, index) => (
              <div
                key={`${index}:${message}`}
                className="flex items-center gap-2 rounded-lg border bg-muted/40 px-2.5 py-1.5 text-xs"
              >
                <span className="text-muted-foreground">Queued</span>
                <span className="min-w-0 flex-1 truncate">{message}</span>
                <button
                  type="button"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => runControls.removeQueued(index)}
                  aria-label="Remove queued message"
                >
                  <XIcon className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
        <ComposerPrimitive.AttachmentDropzone render={<div data-slot="aui_composer-shell" className="border-border/60 data-[dragging=true]:border-ring focus-within:border-border dark:border-muted-foreground/15 dark:focus-within:border-muted-foreground/30 flex w-full flex-col gap-2 rounded-(--composer-radius) border bg-(--composer-bg) p-(--composer-padding) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] transition-[border-color,box-shadow] focus-within:shadow-[0_6px_24px_-8px_rgba(0,0,0,0.12),0_1px_2px_rgba(0,0,0,0.05)] data-[dragging=true]:border-dashed data-[dragging=true]:bg-[color-mix(in_oklab,var(--color-accent)_50%,var(--color-background))] dark:shadow-none" />}><ComposerAttachments /><ComposerPrimitive.Input
                        placeholder="Send a message… Use @ files, $ skills, or / commands"
                        className="aui-composer-input caret-primary placeholder:text-muted-foreground/80 max-h-32 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base outline-none"
                        rows={1}
                        onKeyDown={handleKeyDown}
                        autoFocus
                        enterKeyHint="send"
                        aria-label="Message input"
                      /><ComposerAction /></ComposerPrimitive.AttachmentDropzone>
        {ComposerTriggers && <ComposerTriggers />}
      </ComposerPrimitive.Root>
    </ComposerPrimitive.Unstable_TriggerPopoverRoot>
  );
};

const ComposerAction: FC = () => {
  const { ComposerControls } = useContext(ThreadComponentsContext);
  const aui = useAui();
  const composerText = useAuiState((state) => state.composer.text);
  const runControls = useRunControls();

  const takeComposerText = () => {
    const text = aui.composer.getState().text.trim();
    if (text) aui.composer.setText("");
    return text;
  };

  return (
    <div className="aui-composer-action-wrapper relative flex items-center justify-between">
      <div className="flex min-w-0 items-center gap-1.5">
        <ComposerAddAttachment />
        {ComposerControls && <ComposerControls />}
      </div>
      <div className="flex items-center gap-1.5">
        <AuiIf condition={(s) => s.thread.capabilities.dictation}>
          <AuiIf condition={(s) => s.composer.dictation == null}>
            <ComposerPrimitive.Dictate render={<TooltipIconButton tooltip="Voice input" side="bottom" type="button" variant="ghost" size="icon" className="aui-composer-dictate size-7 rounded-full" aria-label="Start voice input" />}><MicIcon className="aui-composer-dictate-icon size-4" /></ComposerPrimitive.Dictate>
          </AuiIf>
          <AuiIf condition={(s) => s.composer.dictation != null}>
            <ComposerPrimitive.StopDictation render={<TooltipIconButton tooltip="Stop dictation" side="bottom" type="button" variant="ghost" size="icon" className="aui-composer-stop-dictation text-destructive size-7 rounded-full" aria-label="Stop voice input" />}><SquareIcon className="aui-composer-stop-dictation-icon size-3.5 animate-pulse fill-current" /></ComposerPrimitive.StopDictation>
          </AuiIf>
        </AuiIf>
        <AuiIf condition={(s) => !s.thread.isRunning}>
          <ComposerPrimitive.Send render={<TooltipIconButton tooltip="Send message" side="bottom" type="button" variant="default" size="icon" className="aui-composer-send size-7 rounded-full" aria-label="Send message" />}><ArrowUpIcon className="aui-composer-send-icon size-4.5" /></ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(s) => s.thread.isRunning}>
          <TooltipIconButton
            tooltip="Queue message"
            side="bottom"
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 rounded-full"
            aria-label="Queue message"
            disabled={!composerText.trim()}
            onClick={() => {
              const text = takeComposerText();
              if (text) runControls.enqueue(text);
            }}
          >
            <ListPlusIcon className="size-4" />
          </TooltipIconButton>
          <TooltipIconButton
            tooltip="Steer current turn"
            side="bottom"
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 rounded-full"
            aria-label="Steer current turn"
            disabled={!composerText.trim()}
            onClick={() => {
              const text = takeComposerText();
              if (text) void runControls.steer(text);
            }}
          >
            <CornerDownLeftIcon className="size-4" />
          </TooltipIconButton>
          <ComposerPrimitive.Cancel render={<Button type="button" variant="default" size="icon" className="aui-composer-cancel size-7 rounded-full" aria-label="Stop generating" />}><SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" /></ComposerPrimitive.Cancel>
        </AuiIf>
      </div>
    </div>
  );
};

const MessageError: FC = () => {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="aui-message-error-root border-destructive bg-destructive/10 text-destructive dark:bg-destructive/5 mt-2 rounded-md border p-3 text-sm dark:text-red-200">
        <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

const formatElapsedTime = (durationMs: number) => {
  const totalSeconds = Math.max(0, Math.round(durationMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [
    hours > 0 ? `${hours}小时` : "",
    minutes > 0 ? `${minutes}分` : "",
    `${seconds}秒`,
  ].join("");
};

const ProcessGroup: FC<PropsWithChildren<{ group: ThreadGroupPart }>> = ({
  group,
  children,
}) => {
  const durationMs = useAuiState((state) => {
    const restored = state.message.metadata.custom.processDurationMs;
    return typeof restored === "number"
      ? restored
      : state.message.metadata.timing?.totalStreamTime;
  });
  const running = group.status.type === "running";
  const label = running
    ? "处理中"
    : durationMs === undefined
      ? "已处理"
      : `已处理 ${formatElapsedTime(durationMs)}`;

  return (
    <ToolGroupRoot variant="ghost">
      <ToolGroupTrigger
        count={group.indices.length}
        label={label}
        active={running}
      />
      <ToolGroupContent>{children}</ToolGroupContent>
    </ToolGroupRoot>
  );
};

const AssistantMessage: FC = () => {
  const {
    ToolFallback: ToolFallbackComponent = ToolFallback,
    ToolGroup,
    ReasoningGroup,
  } = useContext(ThreadComponentsContext);

  const ACTION_BAR_PT = "pt-1.5";
  // Keep the action bar inside the contained root's paint box, then cancel its reserved space in flow.
  const ACTION_BAR_HEIGHT = `min-h-7.5 ${ACTION_BAR_PT}`;

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="fade-in slide-in-from-bottom-1 animate-in relative -mb-7.5 pb-7.5 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto]"
    >
      <div
        data-slot="aui_assistant-message-content"
        className="text-foreground px-2 leading-relaxed wrap-break-word"
      >
        <MessagePrimitive.GroupedParts
          groupBy={groupPartByType({
            reasoning: ["group-process"],
            "tool-call": ["group-process"],
            "standalone-tool-call": [],
          })}
        >
          {({ part, children }) => {
            switch (part.type) {
              case "group-process":
                return <ProcessGroup group={part}>{children}</ProcessGroup>;
              case "group-tool":
                if (ToolGroup) {
                  return <ToolGroup group={part}>{children}</ToolGroup>;
                }
                return (
                  <ToolGroupRoot variant="ghost">
                    <ToolGroupTrigger
                      count={part.indices.length}
                      active={part.status.type === "running"}
                    />
                    <ToolGroupContent>{children}</ToolGroupContent>
                  </ToolGroupRoot>
                );
              case "group-reasoning": {
                if (ReasoningGroup) {
                  return (
                    <ReasoningGroup group={part}>{children}</ReasoningGroup>
                  );
                }
                const running = part.status.type === "running";
                return (
                  <ReasoningRoot streaming={running}>
                    <ReasoningTrigger active={running} />
                    <ReasoningContent aria-busy={running}>
                      <ReasoningText>{children}</ReasoningText>
                    </ReasoningContent>
                  </ReasoningRoot>
                );
              }
              case "text":
                return <MarkdownText />;
              case "reasoning":
                return <Reasoning {...part} />;
              case "tool-call":
                return part.toolUI ?? <ToolFallbackComponent {...part} />;
              case "data":
                return part.dataRendererUI;
              case "indicator":
                return (
                  <span
                    data-slot="aui_assistant-message-indicator"
                    className="animate-pulse font-sans"
                    aria-label="Assistant is working"
                  >
                    {"●"}
                  </span>
                );
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>

      <div
        data-slot="aui_assistant-message-footer"
        className={cn("ms-2 flex items-center", ACTION_BAR_HEIGHT)}
      >
        <BranchPicker />
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-assistant-action-bar-root text-muted-foreground animate-in fade-in col-start-3 row-start-2 -ms-1 flex gap-1 duration-200"
    >
      <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="Copy" />}><AuiIf condition={(s) => s.message.isCopied}>
                      <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
                    </AuiIf><AuiIf condition={(s) => !s.message.isCopied}>
                      <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
                    </AuiIf></ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload render={<TooltipIconButton tooltip="Refresh" />}><RefreshCwIcon /></ActionBarPrimitive.Reload>
      <ActionBarMorePrimitive.Root>
        <ActionBarMorePrimitive.Trigger render={<TooltipIconButton tooltip="More" className="data-[state=open]:bg-accent" />}><MoreHorizontalIcon /></ActionBarMorePrimitive.Trigger>
        <ActionBarMorePrimitive.Content
          side="bottom"
          align="start"
          sideOffset={6}
          className="aui-action-bar-more-content bg-popover/95 text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-[8rem] overflow-hidden rounded-xl border p-1.5 shadow-lg backdrop-blur-sm"
        >
          <ActionBarPrimitive.ExportMarkdown render={<ActionBarMorePrimitive.Item className="aui-action-bar-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none" />}><DownloadIcon className="size-4" />Export as Markdown
                              </ActionBarPrimitive.ExportMarkdown>
        </ActionBarMorePrimitive.Content>
      </ActionBarMorePrimitive.Root>
    </ActionBarPrimitive.Root>
  );
};

const UserMessage: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      className="fade-in slide-in-from-bottom-1 animate-in grid auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <UserMessageAttachments />

      <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
        <div className="aui-user-message-content peer bg-muted text-foreground rounded-xl px-4 py-2 wrap-break-word empty:hidden">
          <MessagePrimitive.Parts />
        </div>
        <div className="aui-user-action-bar-wrapper absolute start-0 top-1/2 -translate-x-full -translate-y-1/2 pe-2 peer-empty:hidden rtl:translate-x-full">
          <UserActionBar />
        </div>
      </div>

      <BranchPicker
        data-slot="aui_user-branch-picker"
        className="col-span-full col-start-1 row-start-3 -me-1 justify-end"
      />
    </MessagePrimitive.Root>
  );
};

const UserActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-user-action-bar-root flex flex-col items-end"
    >
      <ActionBarPrimitive.Edit render={<TooltipIconButton tooltip="Edit" className="aui-user-action-edit" />}><PencilIcon /></ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  );
};

const EditComposer: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_edit-composer-wrapper"
      className="flex flex-col px-2 [contain-intrinsic-size:auto_200px] [content-visibility:auto]"
    >
      <ComposerPrimitive.Root className="aui-edit-composer-root border-border/60 dark:border-muted-foreground/15 ms-auto flex w-full max-w-[85%] flex-col rounded-(--composer-radius) border bg-(--composer-bg) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] dark:shadow-none">
        <ComposerPrimitive.Input
          className="aui-edit-composer-input text-foreground min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base outline-none"
          autoFocus
        />
        <div className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
          <ComposerPrimitive.Cancel render={<Button variant="ghost" size="sm" className="h-8 rounded-full px-3.5" />}>Cancel
                              </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send render={<Button size="sm" className="h-8 rounded-full px-3.5" />}>Update
                              </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const BranchPicker: FC<BranchPickerPrimitive.Root.Props> = ({
  className,
  ...rest
}) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn(
        "aui-branch-picker-root text-muted-foreground -ms-2 me-2 inline-flex items-center text-xs",
        className,
      )}
      {...rest}
    >
      <BranchPickerPrimitive.Previous render={<TooltipIconButton tooltip="Previous" />}><ChevronLeftIcon /></BranchPickerPrimitive.Previous>
      <span className="aui-branch-picker-state font-medium">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next render={<TooltipIconButton tooltip="Next" />}><ChevronRightIcon /></BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};
