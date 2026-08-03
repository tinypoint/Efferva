import {
  createContext,
  forwardRef,
  useContext,
  type ComponentProps,
  type KeyboardEvent,
} from "react";
import { Plus } from "lucide-react";
import {
  CopilotChatInput,
  type CopilotChatInputProps,
} from "@copilotkit/react-core/v2";

import { useEffervaRuntime } from "../runtime/RuntimeContext";

type ComposerAddMenuButtonProps = ComponentProps<
  typeof CopilotChatInput.AddMenuButton
>;

type TriggerOption = {
  value: string;
  label: string;
  description: string;
};

type ComposerSuggestionsContextValue = {
  active: boolean;
  triggerChar: string;
  options: TriggerOption[];
  selectedIndex: number;
  select: (value: string) => void;
  setSelectedIndex: (index: number) => void;
  handleKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  openFromButton: () => void;
};

const ComposerSuggestionsContext =
  createContext<ComposerSuggestionsContextValue | null>(null);

function ComposerAddMenuButton({
  className,
  disabled,
}: ComposerAddMenuButtonProps) {
  const runtime = useEffervaRuntime();
  const suggestions = useContext(ComposerSuggestionsContext);
  const selectedModel = runtime.models.find(
    (item) => item.model === runtime.model,
  );

  return (
    <div
      className="flex min-w-0 items-center gap-1"
      onClick={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        className={`grid size-8 shrink-0 place-items-center rounded-full text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50 ${className ?? ""}`}
        disabled={disabled}
        aria-label="打开选择面板"
        onClick={(event) => {
          event.preventDefault();
          suggestions?.openFromButton();
        }}
      >
        <Plus className="size-5" />
      </button>
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
      {runtime.collaborationMode === "plan" && (
        <span className="flex h-8 items-center gap-1 rounded-md bg-muted pl-2 text-xs font-medium">
          计划模式
          <button
            type="button"
            className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label="退出计划模式"
            onClick={() => void runtime.setCollaborationMode("default")}
          >
            ×
          </button>
        </span>
      )}
      {runtime.goalMode && (
        <span className="flex h-8 items-center gap-1 rounded-md bg-muted pl-2 text-xs font-medium">
          目标模式
          <button
            type="button"
            className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label="退出目标模式"
            onClick={() => void runtime.setGoalMode(false)}
          >
            ×
          </button>
        </span>
      )}
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
    onKeyDown,
    className: _className,
    ...props
  },
  ref,
) {
  const suggestions = useContext(ComposerSuggestionsContext);
  return (
    <CopilotChatInput.TextArea
      {...props}
      ref={ref}
      className="cpk:w-full cpk:px-5 cpk:py-3"
      onKeyDown={(event) => {
        if (suggestions?.handleKeyDown(event)) return;
        onKeyDown?.(event);
      }}
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
  const suggestions = useContext(ComposerSuggestionsContext);
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
          keyboardHeight > 0 ? `translateY(-${keyboardHeight}px)` : undefined,
        transition: "transform 0.2s ease-out",
        ...(positioning === "absolute" || bottomAnchored
          ? { paddingBottom: "var(--copilotkit-license-banner-offset, 0px)" }
          : {}),
        ...style,
      }}
    >
      {suggestions?.active && suggestions.options.length > 0 && (
        <div
          data-efferva-trigger-menu
          className="cpk:pointer-events-auto cpk:absolute cpk:right-0 cpk:bottom-full cpk:left-0 cpk:pb-2"
        >
          <div className="cpk:mx-auto cpk:max-w-3xl cpk:px-4 cpk:@3xl:px-0 cpk:[div[data-sidebar-chat]_&]:px-8 cpk:[div[data-popup-chat]_&]:px-4">
            <div
              className="cpk:max-h-72 cpk:overflow-y-auto cpk:rounded-xl cpk:border cpk:bg-background cpk:p-1 cpk:shadow-lg"
              role="listbox"
            >
              {suggestions.options.map((option, index) => (
                <button
                  key={`${suggestions.triggerChar}:${option.value}`}
                  type="button"
                  role="option"
                  aria-selected={index === suggestions.selectedIndex}
                  className={`cpk:flex cpk:w-full cpk:items-start cpk:gap-3 cpk:rounded-lg cpk:px-3 cpk:py-2 cpk:text-left ${
                    index === suggestions.selectedIndex ? "cpk:bg-muted" : ""
                  }`}
                  onMouseEnter={() => suggestions.setSelectedIndex(index)}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => suggestions.select(option.value)}
                >
                  <span className="cpk:text-sm cpk:font-medium">
                    {option.label}
                  </span>
                  <span className="cpk:min-w-0 cpk:flex-1 cpk:truncate cpk:text-xs cpk:text-muted-foreground">
                    {option.description}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
      <div className="cpk:max-w-3xl cpk:mx-auto cpk:py-0 cpk:px-4 cpk:@3xl:px-0 cpk:[div[data-sidebar-chat]_&]:px-8 cpk:[div[data-popup-chat]_&]:px-4 cpk:pointer-events-auto">
        <div
          data-testid="copilot-chat-input"
          data-layout="expanded"
          className="copilotKitInput cpk:flex cpk:w-full cpk:flex-col cpk:items-center cpk:justify-center cpk:cursor-text cpk:overflow-visible cpk:bg-clip-padding cpk:contain-inline-size cpk:bg-white cpk:dark:bg-[#303030] cpk:shadow-[0_4px_4px_0_#0000000a,0_0_1px_0_#0000009e] cpk:rounded-[28px]"
          onClick={(event) => {
            const target = event.target;
            if (target instanceof Element && target.closest("button, select")) {
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

export {
  ComposerAddMenuButton,
  ComposerLayout,
  ComposerSuggestionsContext,
  ComposerTextArea,
};
export type { ComposerSuggestionsContextValue, TriggerOption };
