import { useCallback, useEffect, useMemo, useState } from "react";
import { Menu } from "@base-ui/react/menu";
import type { CodexClient } from "@efferva/codex-client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Code2,
  LoaderCircle,
  MoreHorizontal,
  Plus,
  Trash2,
} from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import {
  AlertDialog,
  AlertDialogClose,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { api } from "./api";
import { createCodexClient } from "./CodexAgent";
import { CodexEvents } from "./codexEvents";
import { EffervaChat, EffervaRuntime } from "./EffervaRuntime";
import type {
  CollaborationMode,
  ExecutionSettings,
  ModelOption,
  SkillListEntry,
  ThreadSummary,
} from "./types";

function threadTitle(thread: ThreadSummary): string {
  return thread.name?.trim() || "Untitled thread";
}

export function App() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { sessionId, threadId } = useParams<{
    sessionId?: string;
    threadId?: string;
  }>();
  const [threadToDelete, setThreadToDelete] = useState<ThreadSummary | null>(
    null,
  );
  const [draftSettings, setDraftSettings] =
    useState<Required<ExecutionSettings> | null>(null);
  const [executionSettingsByThread, setExecutionSettingsByThread] = useState<
    Record<string, ExecutionSettings>
  >({});
  const codex = useMemo(
    () => (sessionId ? createCodexClient(sessionId) : null),
    [sessionId],
  );
  const codexEvents = useMemo(
    () => (codex ? new CodexEvents(codex) : null),
    [codex],
  );
  useEffect(
    () => {
      codexEvents?.open();
      return () => {
        codexEvents?.close();
        codex?.close();
      };
    },
    [codex, codexEvents],
  );
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  const threads = useQuery({
    queryKey: ["threads", sessionId],
    queryFn: async () => {
      const response = await codex!.request<{ data: ThreadSummary[] }>(
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
    enabled: Boolean(codex),
  });
  useEffect(() => {
    if (!codexEvents || !sessionId) return;
    const listChangingMethods = new Set([
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
    return codexEvents.subscribe(({ threadId: notifiedThreadId, notification }) => {
      if (!listChangingMethods.has(notification.method)) return;
      if (
        notification.method === "thread/deleted" &&
        notifiedThreadId === threadId
      ) {
        navigate(`/sessions/${sessionId}`, { replace: true });
      }
      void queryClient.invalidateQueries({
        queryKey: ["threads", sessionId],
      });
    });
  }, [codexEvents, navigate, queryClient, sessionId, threadId]);
  const models = useQuery({
    queryKey: ["models", sessionId],
    queryFn: async () => {
      const response = await codex!.request<{ data: ModelOption[] }>(
        "model/list",
        { limit: 100, includeHidden: false },
      );
      return response.data;
    },
    enabled: Boolean(codex),
  });
  const activeSettings = threadId
    ? executionSettingsByThread[threadId]
    : draftSettings;
  const selectedModel =
    models.data?.find(
      (item) => item.model === activeSettings?.model,
    ) ??
    models.data?.find((item) => item.isDefault) ??
    models.data?.[0];
  const model = selectedModel?.model ?? "";
  const reasoningEffort =
    selectedModel?.supportedReasoningEfforts.find(
      (item) =>
        item.reasoningEffort === activeSettings?.reasoning_effort,
    )?.reasoningEffort ??
    selectedModel?.defaultReasoningEffort ??
    "low";
  const collaborationMode: CollaborationMode =
    activeSettings?.collaboration_mode === "plan" ? "plan" : "default";
  const selectedThread = threads.data?.find((item) => item.id === threadId);
  const skills = useQuery({
    queryKey: ["skills", sessionId, selectedThread?.cwd],
    queryFn: async () => {
      const response = await codex!.request<{ data: SkillListEntry[] }>(
        "skills/list",
        {
          cwds: selectedThread?.cwd ? [selectedThread.cwd] : [],
          forceReload: false,
        },
      );
      return response.data;
    },
    enabled: Boolean(codex),
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

  const selectExecutionSettings = useCallback(
    (nextModel: string, nextEffort: string) => {
      if (!threadId) {
        setDraftSettings((current) => ({
          model: nextModel,
          reasoning_effort: nextEffort,
          collaboration_mode: current?.collaboration_mode ?? "default",
        }));
        return;
      }
      setExecutionSettingsByThread((current) => ({
        ...current,
        [threadId]: {
          model: nextModel,
          reasoning_effort: nextEffort,
          collaboration_mode:
            current[threadId]?.collaboration_mode ?? "default",
        },
      }));
    },
    [threadId],
  );

  const handleModelChange = useCallback(
    (nextModelId: string) => {
      const nextModel = models.data?.find(
        (item) => item.model === nextModelId,
      );
      if (!nextModel) return;
      selectExecutionSettings(
        nextModel.model,
        nextModel.defaultReasoningEffort,
      );
    },
    [models.data, selectExecutionSettings],
  );

  const handleReasoningEffortChange = useCallback(
    (nextEffort: string) => {
      selectExecutionSettings(model, nextEffort);
    },
    [model, selectExecutionSettings],
  );

  const handleThreadCreated = useCallback(
    (thread: ThreadSummary) => {
      if (!sessionId) return;
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
      const settings: Required<ExecutionSettings> = {
        model,
        reasoning_effort: reasoningEffort,
        collaboration_mode: collaborationMode,
      };
      setExecutionSettingsByThread((current) => ({
        ...current,
        [thread.id]: settings,
      }));
      setDraftSettings(null);
    },
    [
      collaborationMode,
      model,
      navigate,
      queryClient,
      reasoningEffort,
      sessionId,
    ],
  );

  const handleExecutionSettingsLoaded = useCallback(
    (loadedThreadId: string, settings: ExecutionSettings) => {
      setExecutionSettingsByThread((current) => ({
        ...current,
        [loadedThreadId]: {
          ...settings,
          collaboration_mode: settings.collaboration_mode ?? "default",
        },
      }));
    },
    [],
  );

  const handleCollaborationModeChange = useCallback(
    (updatedThreadId: string, nextMode: CollaborationMode) => {
      if (updatedThreadId === "new") {
        setDraftSettings((current) => ({
          model: current?.model ?? model,
          reasoning_effort: current?.reasoning_effort ?? reasoningEffort,
          collaboration_mode: nextMode,
        }));
        return;
      }
      setExecutionSettingsByThread((current) => ({
        ...current,
        [updatedThreadId]: {
          model: current[updatedThreadId]?.model ?? model,
          reasoning_effort:
            current[updatedThreadId]?.reasoning_effort ?? reasoningEffort,
          collaboration_mode: nextMode,
        },
      }));
    },
    [model, reasoningEffort],
  );

  const handleRunSettled = useCallback(
    (_settledThreadId: string) => {
      void queryClient.invalidateQueries({
        queryKey: ["threads", sessionId],
      });
    },
    [queryClient, sessionId],
  );

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
    mutationFn: async (deletedThreadId: string) => {
      if (!codex) throw new Error("Codex is unavailable");
      return codex.request("thread/delete", { threadId: deletedThreadId });
    },
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
    !sessionId ||
    !codex ||
    threads.isLoading ||
    !threads.data ||
    models.isLoading ||
    !models.data
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
      client={codex as CodexClient}
      events={codexEvents!}
      sessionId={sessionId}
      threadId={threadId}
      model={model}
      reasoningEffort={reasoningEffort}
      collaborationMode={collaborationMode}
      models={models.data}
      onModelChange={handleModelChange}
      onReasoningEffortChange={handleReasoningEffortChange}
      onCollaborationModeChange={handleCollaborationModeChange}
      workspace={selectedThread?.cwd}
      skills={enabledSkills}
      onThreadCreated={handleThreadCreated}
      onThreadNameUpdated={handleThreadNameUpdated}
      onExecutionSettingsLoaded={handleExecutionSettingsLoaded}
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
                  {threadTitle(thread)}
                </button>
                <Menu.Root>
                  <Menu.Trigger
                    className="mr-1 rounded p-1 opacity-0 outline-none hover:bg-background focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 data-popup-open:bg-background data-popup-open:opacity-100"
                    aria-label={`Actions for ${threadTitle(thread)}`}
                  >
                    <MoreHorizontal className="size-4" />
                  </Menu.Trigger>
                  <Menu.Portal>
                    <Menu.Positioner
                      side="bottom"
                      align="end"
                      sideOffset={4}
                      className="z-50"
                    >
                      <Menu.Popup
                        finalFocus={false}
                        className="min-w-32 rounded-lg bg-popover p-1 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-none data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95"
                      >
                        <Menu.Item
                          className="flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-destructive outline-none data-highlighted:bg-destructive/10"
                          onClick={() => {
                            deleteThread.reset();
                            setThreadToDelete(thread);
                          }}
                        >
                          <Trash2 className="size-4" />
                          Delete
                        </Menu.Item>
                      </Menu.Popup>
                    </Menu.Positioner>
                  </Menu.Portal>
                </Menu.Root>
              </div>
            ))}
          </div>
        </aside>
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
      <AlertDialog
        open={threadToDelete !== null}
        onOpenChange={(open) => {
          if (open || deleteThread.isPending) return;
          deleteThread.reset();
          setThreadToDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete thread?</AlertDialogTitle>
            <AlertDialogDescription>
              “{threadToDelete ? threadTitle(threadToDelete) : ""}” will be permanently deleted. This
              action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteThread.error && (
            <p className="text-sm text-destructive" role="alert">
              {deleteThread.error instanceof Error
                ? deleteThread.error.message
                : "Failed to delete the thread"}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogClose
              render={
                <Button variant="outline" disabled={deleteThread.isPending} />
              }
            >
              Cancel
            </AlertDialogClose>
            <Button
              type="button"
              variant="destructive"
              disabled={!threadToDelete || deleteThread.isPending}
              onClick={() => {
                if (threadToDelete) deleteThread.mutate(threadToDelete.id);
              }}
            >
              {deleteThread.isPending ? "Deleting…" : "Delete"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </EffervaRuntime>
  );
}
