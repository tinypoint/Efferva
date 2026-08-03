import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Message } from "@ag-ui/client";
import type { CodexClient } from "@efferva/codex-client";

import { CodexAgent, type CodexRunConfig } from "../CodexAgent";
import { CodexEvents } from "../codexEvents";
import { asObject } from "../codexProjection";
import type {
  CollaborationMode,
  ExecutionSettings,
  FileSearchResult,
  ModelOption,
  SkillMetadata,
  ThreadSummary,
} from "../types";
import type { RuntimeContextValue } from "./RuntimeContext";
import { useThreadControls, type GoalState } from "./useThreadControls";
import {
  isMissingThread,
  loadThreadHistoryPage,
  mergeHistoryMessages,
  prependHistoryMessages,
  restoreMessages,
  type EffervaProcessMessage,
} from "./threadHistory";

export type ThreadRuntimeOptions = {
  client: CodexClient;
  events: CodexEvents;
  sessionId: string;
  threadId?: string;
  model?: string;
  reasoningEffort: string;
  collaborationMode: CollaborationMode;
  models: ModelOption[];
  onModelChange: (model: string) => void;
  onReasoningEffortChange: (effort: string) => void;
  onCollaborationModeChange: (
    threadId: string,
    mode: CollaborationMode,
  ) => void;
  workspace?: string | null;
  skills: SkillMetadata[];
  onThreadCreated: (thread: ThreadSummary) => void;
  onThreadNameUpdated: (threadId: string, threadName: string) => void;
  onExecutionSettingsLoaded: (
    threadId: string,
    settings: ExecutionSettings,
  ) => void;
  onRunSettled: (threadId: string) => void;
  onThreadNotFound: (threadId: string) => void;
};

export function useThreadRuntime({
  client,
  events,
  sessionId,
  threadId,
  model,
  reasoningEffort,
  collaborationMode,
  models,
  onModelChange,
  onReasoningEffortChange,
  onCollaborationModeChange,
  workspace,
  skills,
  onThreadCreated,
  onThreadNameUpdated,
  onExecutionSettingsLoaded,
  onRunSettled,
  onThreadNotFound,
}: ThreadRuntimeOptions) {
  const desiredThreadId = threadId ?? "new";
  const [openedThreadId, setOpenedThreadId] = useState<string | null>(
    desiredThreadId === "new" ? "new" : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [goalState, setGoalState] = useState<GoalState>("off");
  const [historyMessages, setHistoryMessages] = useState<Message[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [loadingOlderHistory, setLoadingOlderHistory] = useState(false);
  const olderHistoryRequestRef = useRef<AbortController | null>(null);
  const navigationEpochRef = useRef(0);
  const desiredThreadIdRef = useRef(desiredThreadId);
  const settingsRef = useRef<CodexRunConfig>({
    model,
    reasoningEffort,
    workspace,
    skills,
    collaborationMode,
    setGoalFromPrompt: goalState === "pending",
  });
  const onThreadCreatedRef = useRef(onThreadCreated);
  const onThreadNameUpdatedRef = useRef(onThreadNameUpdated);
  const onExecutionSettingsLoadedRef = useRef(onExecutionSettingsLoaded);
  const onRunSettledRef = useRef(onRunSettled);
  const onThreadNotFoundRef = useRef(onThreadNotFound);
  const createdThreadIdRef = useRef<string | null>(null);
  const mirroredTurnIdRef = useRef<string | null>(null);
  desiredThreadIdRef.current = desiredThreadId;
  settingsRef.current = {
    model,
    reasoningEffort,
    workspace,
    skills,
    collaborationMode,
    setGoalFromPrompt: goalState === "pending",
  };
  onThreadCreatedRef.current = onThreadCreated;
  onThreadNameUpdatedRef.current = onThreadNameUpdated;
  onExecutionSettingsLoadedRef.current = onExecutionSettingsLoaded;
  onRunSettledRef.current = onRunSettled;
  onThreadNotFoundRef.current = onThreadNotFound;

  const agent = useMemo(() => {
    return new CodexAgent(client, events, () => settingsRef.current);
  }, [client, events]);

  useEffect(() => {
    return events.subscribe(
      ({ sequence, threadId: notifiedThreadId, notification }) => {
        const params = asObject(notification.params);
        if (
          notification.method === "thread/name/updated" &&
          params.threadId &&
          params.threadName
        ) {
          onThreadNameUpdatedRef.current(
            String(params.threadId),
            String(params.threadName),
          );
        }
        if (
          notification.method === "thread/settings/updated" &&
          notifiedThreadId
        ) {
          const settings = asObject(params.threadSettings);
          onExecutionSettingsLoadedRef.current(notifiedThreadId, {
            model: settings.model ? String(settings.model) : undefined,
            reasoning_effort: settings.effort
              ? String(settings.effort)
              : undefined,
            collaboration_mode:
              asObject(settings.collaborationMode).mode === "plan"
                ? "plan"
                : "default",
          });
        }
        if (notifiedThreadId === desiredThreadIdRef.current) {
          if (notification.method === "thread/goal/updated") {
            setGoalState("active");
          } else if (notification.method === "thread/goal/cleared") {
            setGoalState("off");
          }
        }
        if (
          notification.method !== "turn/started" ||
          !notifiedThreadId ||
          notifiedThreadId !== desiredThreadIdRef.current ||
          agent.isRunning ||
          agent.isFollowingTurn
        ) {
          return;
        }
        const turn = asObject(params.turn);
        const nativeTurnId = turn.id ? String(turn.id) : "";
        if (!nativeTurnId || mirroredTurnIdRef.current === nativeTurnId) return;
        mirroredTurnIdRef.current = nativeTurnId;
        agent.setResumeSource({
          threadId: notifiedThreadId,
          turnId: nativeTurnId,
          afterSequence: sequence - 1,
          mirrorUserMessages: true,
        });
        void agent.connectAgent().catch((cause: unknown) => {
          if (mirroredTurnIdRef.current === nativeTurnId) {
            mirroredTurnIdRef.current = null;
          }
          if (desiredThreadIdRef.current === notifiedThreadId) {
            setError(
              cause instanceof Error
                ? cause.message
                : "Failed to follow the active turn",
            );
          }
        });
      },
    );
  }, [agent, events]);

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
      const page = await loadThreadHistoryPage(client, currentThreadId, cursor);
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
  }, [client, historyCursor]);

  const { setCollaborationMode, setGoalMode, runControl } = useThreadControls({
    agent,
    desiredThreadIdRef,
    settingsRef,
    goalState,
    setGoalState,
    setError,
    onCollaborationModeChange,
  });

  useEffect(() => {
    let activeRunId: string | null = null;
    const subscription = agent.subscribe({
      onRunStartedEvent({ event }) {
        activeRunId = event.runId;
      },
      onToolCallStartEvent({ event, messages }) {
        const processMessageId = activeRunId ? `${activeRunId}:process` : null;
        let processIndex = processMessageId
          ? messages.findIndex(
              (message) =>
                message.id === processMessageId && message.role === "reasoning",
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

        const nextMessages = messages.map((message) => ({
          ...message,
        })) as Message[];
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
              part.type === "tool-call" && part.toolCallId === event.toolCallId,
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
        if (raw?.method === "efferva/thread-created" && raw.params?.thread) {
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
        mirroredTurnIdRef.current = null;
        if (!settledThreadId || settledThreadId === "new") return;
        if (desiredThreadIdRef.current !== settledThreadId) return;
        onRunSettledRef.current(settledThreadId);
        try {
          const detail = await loadThreadHistoryPage(client, settledThreadId);
          if (
            current.threadId !== settledThreadId ||
            desiredThreadIdRef.current !== settledThreadId
          ) {
            return;
          }
          onExecutionSettingsLoadedRef.current(settledThreadId, {
            model: detail.model,
            reasoning_effort: detail.reasoning_effort,
            collaboration_mode: detail.collaboration_mode,
          });
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
  }, [agent, client]);

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
    setGoalState("off");
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
        const detail = await loadThreadHistoryPage(client, desiredThreadId);
        if (!isCurrentNavigation()) return;
        onExecutionSettingsLoadedRef.current(desiredThreadId, {
          model: detail.model,
          reasoning_effort: detail.reasoning_effort,
          collaboration_mode: detail.collaboration_mode,
        });
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
            threadId: desiredThreadId,
            turnId: detail.active_turn_id,
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
        if (isMissingThread(cause)) {
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
  }, [agent, client, threadId]);

  const openingThread =
    desiredThreadId !== "new" && openedThreadId !== desiredThreadId;

  const searchFiles = useCallback(
    async (query: string): Promise<FileSearchResult[]> => {
      const root = workspace || "/home/node/workspace";
      const response = await client.request<{ files: FileSearchResult[] }>(
        "fuzzyFileSearch",
        {
          query,
          roots: [root],
          cancellationToken: `efferva:${sessionId}`,
        },
      );
      return response.files;
    },
    [client, sessionId, workspace],
  );

  const context = useMemo<RuntimeContextValue>(
    () => ({
      loading: openingThread,
      historyMessages,
      historyRevision,
      hasOlderHistory: Boolean(historyCursor),
      loadingOlderHistory,
      loadOlderHistory,
      searchFiles,
      error,
      sessionId,
      threadId: desiredThreadId,
      workspace,
      clearError: () => setError(null),
      skills,
      models,
      model: model ?? "",
      reasoningEffort,
      collaborationMode,
      onModelChange,
      onReasoningEffortChange,
      setCollaborationMode,
      goalMode: goalState !== "off",
      setGoalMode,
      runControl,
    }),
    [
      error,
      goalState,
      historyCursor,
      historyMessages,
      historyRevision,
      loadOlderHistory,
      loadingOlderHistory,
      collaborationMode,
      model,
      models,
      onModelChange,
      onReasoningEffortChange,
      reasoningEffort,
      runControl,
      setCollaborationMode,
      setGoalMode,
      searchFiles,
      sessionId,
      skills,
      openingThread,
      desiredThreadId,
      workspace,
    ],
  );

  return { agent, context };
}
