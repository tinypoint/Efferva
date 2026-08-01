import { useCallback, useEffect, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Code2,
  LoaderCircle,
  MoreHorizontal,
  Plus,
} from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "./api";
import { EffervaChat, EffervaRuntime } from "./EffervaRuntime";
import type {
  ExecutionSettings,
  ThreadSummary,
} from "./types";

export function App() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { sessionId, threadId } = useParams<{
    sessionId?: string;
    threadId?: string;
  }>();
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  const threads = useQuery({
    queryKey: ["threads", sessionId],
    queryFn: () => api.listThreads(sessionId!),
    enabled: Boolean(sessionId),
  });
  const models = useQuery({
    queryKey: ["models", sessionId],
    queryFn: () => api.listModels(sessionId!),
    enabled: Boolean(sessionId),
  });
  const executionSettings = useQuery({
    queryKey: ["execution-settings", sessionId, threadId ?? null],
    queryFn: () => api.getExecutionSettings(sessionId!, threadId),
    enabled: Boolean(sessionId),
  });
  const selectedModel =
    models.data?.find(
      (item) => item.model === executionSettings.data?.model,
    ) ??
    models.data?.find((item) => item.isDefault) ??
    models.data?.[0];
  const model = selectedModel?.model ?? "";
  const reasoningEffort =
    selectedModel?.supportedReasoningEfforts.find(
      (item) =>
        item.reasoningEffort === executionSettings.data?.reasoning_effort,
    )?.reasoningEffort ??
    selectedModel?.defaultReasoningEffort ??
    "low";
  const selectedThread = threads.data?.find((item) => item.id === threadId);
  const skills = useQuery({
    queryKey: ["skills", sessionId, selectedThread?.workspace],
    queryFn: () =>
      api.listSkills(sessionId!, selectedThread?.workspace ?? undefined),
    enabled: Boolean(sessionId),
  });
  const createDefaultSession = useMutation({
    mutationFn: () => api.createSession("Default"),
    onSuccess: async (session) => {
      await queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/sessions/${session.id}`, { replace: true });
    },
  });

  useEffect(() => {
    if (sessionId || sessions.isLoading || !sessions.data) return;
    const defaultSession = sessions.data[0];
    if (defaultSession) {
      navigate(`/sessions/${defaultSession.id}`, { replace: true });
    } else if (createDefaultSession.isIdle) {
      createDefaultSession.mutate();
    }
  }, [
    createDefaultSession,
    navigate,
    sessionId,
    sessions.data,
    sessions.isLoading,
  ]);

  useEffect(() => {
    if (!selectedModel || !executionSettings.data || !sessionId) return;
    if (
      executionSettings.data.model !== model ||
      executionSettings.data.reasoning_effort !== reasoningEffort
    ) {
      const normalized: Required<ExecutionSettings> = {
        model,
        reasoning_effort: reasoningEffort,
      };
      queryClient.setQueryData(
        ["execution-settings", sessionId, threadId ?? null],
        normalized,
      );
      const queryKey = [
        "execution-settings",
        sessionId,
        threadId ?? null,
      ];
      void api
        .updateExecutionSettings(sessionId, normalized, threadId)
        .catch(() => queryClient.invalidateQueries({ queryKey }));
    }
  }, [
    executionSettings.data,
    model,
    queryClient,
    reasoningEffort,
    selectedModel,
    sessionId,
    threadId,
  ]);

  const persistExecutionSettings = useCallback(
    (nextModel: string, nextEffort: string) => {
      if (!sessionId) return;
      const settings: Required<ExecutionSettings> = {
        model: nextModel,
        reasoning_effort: nextEffort,
      };
      const queryKey = [
        "execution-settings",
        sessionId,
        threadId ?? null,
      ];
      queryClient.setQueryData(queryKey, settings);
      void api
        .updateExecutionSettings(sessionId, settings, threadId)
        .catch(() => queryClient.invalidateQueries({ queryKey }));
    },
    [queryClient, sessionId, threadId],
  );

  const handleModelChange = useCallback(
    (nextModelId: string) => {
      const nextModel = models.data?.find(
        (item) => item.model === nextModelId,
      );
      if (!nextModel) return;
      persistExecutionSettings(
        nextModel.model,
        nextModel.defaultReasoningEffort,
      );
    },
    [models.data, persistExecutionSettings],
  );

  const handleReasoningEffortChange = useCallback(
    (nextEffort: string) => {
      persistExecutionSettings(model, nextEffort);
    },
    [model, persistExecutionSettings],
  );

  const handleThreadCreated = useCallback(
    (thread: ThreadSummary) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", thread.session_id],
        (current = []) => [
          thread,
          ...current.filter((item) => item.id !== thread.id),
        ],
      );
      navigate(`/sessions/${thread.session_id}/threads/${thread.id}`, {
        replace: true,
      });
      void queryClient.invalidateQueries({
        queryKey: ["threads", thread.session_id],
      });
      const settings: Required<ExecutionSettings> = {
        model,
        reasoning_effort: reasoningEffort,
      };
      queryClient.setQueryData(
        ["execution-settings", thread.session_id, thread.id],
        settings,
      );
      void api.updateExecutionSettings(
        thread.session_id,
        settings,
        thread.id,
      );
    },
    [model, navigate, queryClient, reasoningEffort],
  );

  const handleRunSettled = useCallback(
    (settledThreadId: string) => {
      void queryClient.invalidateQueries({
        queryKey: ["threads", sessionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["thread", sessionId, settledThreadId],
      });
    },
    [queryClient, sessionId],
  );

  const handleThreadNotFound = useCallback(
    (missingThreadId: string) => {
      if (threadId !== missingThreadId) return;
      navigate(`/sessions/${sessionId}`, { replace: true });
    },
    [navigate, sessionId, threadId],
  );

  const deleteThread = useCallback(
    async (deletedThreadId: string) => {
      if (!sessionId || !window.confirm("Delete this thread permanently?")) {
        return;
      }
      await api.deleteThread(sessionId, deletedThreadId);
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", sessionId],
        (current = []) =>
          current.filter((item) => item.id !== deletedThreadId),
      );
      if (threadId === deletedThreadId) {
        navigate(`/sessions/${sessionId}`);
      }
      await queryClient.invalidateQueries({
        queryKey: ["threads", sessionId],
      });
    },
    [navigate, queryClient, sessionId, threadId],
  );

  const enabledSkills = useMemo(
    () =>
      Array.from(
        new Map(
          (skills.data ?? [])
            .flatMap((entry) => entry.skills)
            .filter((skill) => skill.enabled)
            .map((skill) => [skill.name, skill]),
        ).values(),
      ),
    [skills.data],
  );
  if (
    !sessionId ||
    threads.isLoading ||
    !threads.data ||
    models.isLoading ||
    !models.data ||
    executionSettings.isLoading ||
    !executionSettings.data
  ) {
    return (
      <div className="grid h-screen place-items-center bg-background">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <LoaderCircle className="size-4 animate-spin" />
          Opening Efferva…
        </div>
      </div>
    );
  }

  return (
    <EffervaRuntime
      key={sessionId}
      sessionId={sessionId}
      threadId={threadId}
      model={model}
      reasoningEffort={reasoningEffort}
      models={models.data}
      onModelChange={handleModelChange}
      onReasoningEffortChange={handleReasoningEffortChange}
      workspace={selectedThread?.workspace}
      skills={enabledSkills}
      onThreadCreated={handleThreadCreated}
      onRunSettled={handleRunSettled}
      onThreadNotFound={handleThreadNotFound}
    >
      <div className="grid h-screen grid-cols-[17rem_minmax(0,1fr)] overflow-hidden bg-background max-md:grid-cols-1">
        <aside className="flex min-h-0 flex-col border-r bg-sidebar p-3 max-md:hidden">
          <div className="mb-4 flex items-center gap-2 px-2 py-1">
            <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
              <Code2 className="size-4" />
            </span>
            <div>
              <div className="text-sm font-semibold">Efferva</div>
              <div className="text-xs text-muted-foreground">
                Local coding agent
              </div>
            </div>
          </div>
          <button
            type="button"
            className="mb-3 flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-medium hover:bg-sidebar-accent"
            onClick={() => navigate(`/sessions/${sessionId}`)}
          >
            <Plus className="size-4" />
            New Thread
          </button>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {threads.data.map((thread) => (
              <div
                key={thread.id}
                className={`group flex items-center rounded-lg ${
                  thread.id === threadId
                    ? "bg-sidebar-accent"
                    : "hover:bg-sidebar-accent/60"
                }`}
              >
                <button
                  type="button"
                  className="min-w-0 flex-1 truncate px-3 py-2 text-left text-sm"
                  onClick={() =>
                    navigate(`/sessions/${sessionId}/threads/${thread.id}`)
                  }
                >
                  {thread.title}
                </button>
                <button
                  type="button"
                  className="mr-1 rounded p-1 opacity-0 hover:bg-background group-hover:opacity-100"
                  aria-label={`Delete ${thread.title}`}
                  onClick={() => void deleteThread(thread.id)}
                >
                  <MoreHorizontal className="size-4" />
                </button>
              </div>
            ))}
          </div>
        </aside>
        <main className="grid min-h-0 min-w-0 grid-rows-[3.5rem_minmax(0,1fr)]">
          <header className="flex items-center border-b px-5">
            <h1 className="truncate text-sm font-semibold">
              {selectedThread?.title || "New thread"}
            </h1>
          </header>
          <div className="min-h-0">
            <EffervaChat />
          </div>
        </main>
      </div>
    </EffervaRuntime>
  );
}
