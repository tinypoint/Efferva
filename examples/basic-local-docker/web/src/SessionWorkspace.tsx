import { useCallback, useEffect, useMemo, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { DeleteThreadDialog } from "./components/DeleteThreadDialog";
import { ThreadSidebar } from "./components/ThreadSidebar";
import { EffervaChat, EffervaRuntime } from "./EffervaRuntime";
import { useCodexConnection } from "./hooks/useCodexConnection";
import { useExecutionSettings } from "./hooks/useExecutionSettings";
import type { ModelOption, SkillListEntry, ThreadSummary } from "./types";

const THREAD_LIST_CHANGING_METHODS = new Set([
  "thread/started",
  "thread/name/updated",
  "thread/status/changed",
  "thread/archived",
  "thread/unarchived",
  "thread/deleted",
  "thread/closed",
  "thread/settings/updated",
  "turn/started",
  "turn/completed",
]);

type SessionWorkspaceProps = {
  sessionId: string;
  threadId?: string;
};

function threadTitle(thread: ThreadSummary): string {
  return thread.name?.trim() || "Untitled thread";
}

function OpeningWorkspace() {
  return (
    <div className="grid h-screen place-items-center bg-background">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 animate-spin" />
        Opening Efferva…
      </div>
    </div>
  );
}

export function SessionWorkspace({
  sessionId,
  threadId,
}: SessionWorkspaceProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [threadToDelete, setThreadToDelete] = useState<ThreadSummary | null>(
    null,
  );
  const { client: codex, events: codexEvents } =
    useCodexConnection(sessionId);
  const threads = useQuery({
    queryKey: ["threads", sessionId],
    queryFn: async () => {
      const response = await codex.request<{ data: ThreadSummary[] }>(
        "thread/list",
        {
          limit: 100,
          sortKey: "updated_at",
          sortDirection: "desc",
          sourceKinds: ["vscode"],
        },
      );
      return response.data;
    },
  });
  const models = useQuery({
    queryKey: ["models", sessionId],
    queryFn: async () => {
      const response = await codex.request<{ data: ModelOption[] }>(
        "model/list",
        { limit: 100, includeHidden: false },
      );
      return response.data;
    },
  });
  const {
    model,
    reasoningEffort,
    collaborationMode,
    onModelChange,
    onReasoningEffortChange,
    onCollaborationModeChange,
    loadThreadSettings,
    applyDraftToThread,
  } = useExecutionSettings({
    threadId,
    models: models.data,
  });
  const selectedThread = threads.data?.find((item) => item.id === threadId);
  const skills = useQuery({
    queryKey: ["skills", sessionId, selectedThread?.cwd],
    queryFn: async () => {
      const response = await codex.request<{ data: SkillListEntry[] }>(
        "skills/list",
        {
          cwds: selectedThread?.cwd ? [selectedThread.cwd] : [],
          forceReload: false,
        },
      );
      return response.data;
    },
  });

  useEffect(() => {
    return codexEvents.subscribe(
      ({ threadId: notifiedThreadId, notification }) => {
        if (!THREAD_LIST_CHANGING_METHODS.has(notification.method)) return;
        if (
          notification.method === "thread/deleted" &&
          notifiedThreadId === threadId
        ) {
          navigate(`/sessions/${sessionId}`, { replace: true });
        }
        void queryClient.invalidateQueries({
          queryKey: ["threads", sessionId],
        });
      },
    );
  }, [codexEvents, navigate, queryClient, sessionId, threadId]);

  const handleThreadCreated = useCallback(
    (thread: ThreadSummary) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", sessionId],
        (current = []) => [
          thread,
          ...current.filter((item) => item.id !== thread.id),
        ],
      );
      navigate(`/sessions/${sessionId}/threads/${thread.id}`, {
        replace: true,
      });
      applyDraftToThread(thread.id);
    },
    [applyDraftToThread, navigate, queryClient, sessionId],
  );

  const handleRunSettled = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: ["threads", sessionId],
    });
  }, [queryClient, sessionId]);

  const handleThreadNameUpdated = useCallback(
    (updatedThreadId: string, threadName: string) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", sessionId],
        (current = []) =>
          current.map((thread) =>
            thread.id === updatedThreadId
              ? { ...thread, name: threadName }
              : thread,
          ),
      );
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

  const deleteThread = useMutation({
    mutationFn: (deletedThreadId: string) =>
      codex.request("thread/delete", { threadId: deletedThreadId }),
    onSuccess: (_, deletedThreadId) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", sessionId],
        (current = []) =>
          current.filter((item) => item.id !== deletedThreadId),
      );
      if (threadId === deletedThreadId) {
        navigate(`/sessions/${sessionId}`);
      }
      setThreadToDelete(null);
      void queryClient.invalidateQueries({
        queryKey: ["threads", sessionId],
      });
    },
  });
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
    threads.isLoading ||
    !threads.data ||
    models.isLoading ||
    !models.data
  ) {
    return <OpeningWorkspace />;
  }

  return (
    <EffervaRuntime
      client={codex}
      events={codexEvents}
      sessionId={sessionId}
      threadId={threadId}
      model={model}
      reasoningEffort={reasoningEffort}
      collaborationMode={collaborationMode}
      models={models.data}
      onModelChange={onModelChange}
      onReasoningEffortChange={onReasoningEffortChange}
      onCollaborationModeChange={onCollaborationModeChange}
      workspace={selectedThread?.cwd}
      skills={enabledSkills}
      onThreadCreated={handleThreadCreated}
      onThreadNameUpdated={handleThreadNameUpdated}
      onExecutionSettingsLoaded={loadThreadSettings}
      onRunSettled={handleRunSettled}
      onThreadNotFound={handleThreadNotFound}
    >
      <div className="grid h-screen grid-cols-[17rem_minmax(0,1fr)] overflow-hidden bg-background max-md:grid-cols-1">
        <ThreadSidebar
          threads={threads.data}
          selectedThreadId={threadId}
          onNewThread={() => navigate(`/sessions/${sessionId}`)}
          onSelectThread={(selectedThreadId) =>
            navigate(`/sessions/${sessionId}/threads/${selectedThreadId}`)
          }
          onDeleteThread={(thread) => {
            deleteThread.reset();
            setThreadToDelete(thread);
          }}
        />
        <main className="grid min-h-0 min-w-0 grid-rows-[3.5rem_minmax(0,1fr)]">
          <header className="flex items-center border-b px-5">
            <h1 className="truncate text-sm font-semibold">
              {selectedThread
                ? threadTitle(selectedThread)
                : threadId
                  ? "Opening thread…"
                  : "New thread"}
            </h1>
          </header>
          <div className="min-h-0">
            <EffervaChat />
          </div>
        </main>
      </div>
      <DeleteThreadDialog
        title={threadToDelete ? threadTitle(threadToDelete) : null}
        pending={deleteThread.isPending}
        error={deleteThread.error}
        onClose={() => {
          deleteThread.reset();
          setThreadToDelete(null);
        }}
        onConfirm={() => {
          if (threadToDelete) deleteThread.mutate(threadToDelete.id);
        }}
      />
    </EffervaRuntime>
  );
}
