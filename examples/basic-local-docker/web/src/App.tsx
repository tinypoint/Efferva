import { useCallback, useEffect, useMemo, useState } from "react";
import { Menu } from "@base-ui/react/menu";
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
  const [threadToDelete, setThreadToDelete] = useState<ThreadSummary | null>(
    null,
  );
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
      const settings: Required<ExecutionSettings> = {
        model,
        reasoning_effort: reasoningEffort,
      };
      queryClient.setQueryData(
        ["execution-settings", thread.session_id, thread.id],
        settings,
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

  const deleteThread = useMutation({
    mutationFn: async (deletedThreadId: string) => {
      if (!sessionId) throw new Error("Session is unavailable");
      return api.deleteThread(sessionId, deletedThreadId);
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
                <Menu.Root>
                  <Menu.Trigger
                    className="mr-1 rounded p-1 opacity-0 outline-none hover:bg-background focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 data-popup-open:bg-background data-popup-open:opacity-100"
                    aria-label={`Actions for ${thread.title}`}
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
              {selectedThread?.title ?? (threadId ? "Opening thread…" : "New thread")}
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
              “{threadToDelete?.title}” will be permanently deleted. This
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
