import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type UIEvent,
} from "react";
import type { Message } from "@ag-ui/client";
import {
  CopilotChatAssistantMessage,
  CopilotChatReasoningMessage,
  CopilotChatView,
  useAgent,
  useCopilotKit,
  useDefaultRenderTool,
} from "@copilotkit/react-core/v2";

import { CodexAgent } from "../CodexAgent";
import { AGENT_ID, AGENT_UPDATES } from "../runtime/agentConfig";
import { controlFromPrompt } from "../runtime/codexControls";
import { useEffervaRuntime } from "../runtime/RuntimeContext";
import { mergeVisibleMessages } from "../runtime/threadHistory";
import {
  ComposerAddMenuButton,
  ComposerLayout,
  ComposerSuggestionsContext,
  ComposerTextArea,
} from "./Composer";
import {
  CompactToolCall,
  EffervaAssistantMessage,
  EffervaMessageList,
  EffervaReasoningMessage,
} from "./MessageRendering";
import { useComposerSuggestions } from "./useComposerSuggestions";

export function EffervaChat() {
  const runtime = useEffervaRuntime();
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
  const { composerSuggestions, handleInputChange } = useComposerSuggestions({
    input,
    setInput,
    runtime,
  });
  const dispatchingRef = useRef(false);
  const historyAnchorRef = useRef<{
    element: HTMLElement;
    scrollHeight: number;
    scrollTop: number;
    revision: number;
    threadId: string;
  } | null>(null);
  const visibleMessages = runtime.loading
    ? []
    : mergeVisibleMessages(runtime.historyMessages, [
        ...agent.messages,
      ] as Message[]);

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

  const handleHistoryScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const element = event.target;
      if (
        !(element instanceof HTMLElement) ||
        !element.querySelector('[data-testid="copilot-scroll-content"]')
      ) {
        return;
      }
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
  const scrollView = useMemo(
    () =>
      ({
        initial: "instant",
        resize: "instant",
        onScrollCapture: handleHistoryScroll,
      }) as ComponentProps<typeof CopilotChatView.ScrollView> & {
        initial: "instant";
        resize: "instant";
      },
    [handleHistoryScroll],
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
      const control = controlFromPrompt(prompt);
      if (control) {
        const result = await runtime.runControl(control);
        if (!result || control.action === "plan.toggle") return;
        agent.addMessage({
          id: crypto.randomUUID(),
          role: "user",
          content: prompt,
        });
        agent.addMessage({
          id: crypto.randomUUID(),
          role: "assistant",
          content: result.message,
        });
        return;
      }
      agent.addMessage({
        id: crypto.randomUUID(),
        role: "user",
        content: prompt,
      });
      await copilotkit.runAgent({ agent });
    },
    [agent, copilotkit, runtime.loading, runtime.runControl],
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

  const stop = useCallback(() => {
    void (agent as CodexAgent).interrupt();
  }, [agent]);

  const steer = useCallback(() => {
    const prompt = input.trim();
    if (!prompt) return;
    setInput("");
    void (agent as CodexAgent).steer(prompt);
  }, [agent, input]);

  return (
    <div className="relative h-full min-h-0">
      <ComposerSuggestionsContext.Provider value={composerSuggestions}>
        <CopilotChatView
          className="h-full"
          messages={visibleMessages}
          autoScroll="pin-to-bottom"
          isRunning={agent.isRunning}
          inputValue={input}
          onInputChange={handleInputChange}
          onSubmitMessage={(value) => void send(value)}
          onStop={stop}
          scrollView={scrollView}
          input={{
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
      </ComposerSuggestionsContext.Provider>
      {queued.length > 0 && (
        <div className="absolute right-6 bottom-24 left-6 mx-auto max-w-2xl rounded-lg border bg-background/95 px-3 py-2 text-xs shadow-sm">
          Queued: {queued.join(" · ")}
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
