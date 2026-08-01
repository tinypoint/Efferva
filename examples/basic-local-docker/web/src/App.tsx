import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Code2,
  LoaderCircle,
  MoreHorizontal,
  Plus,
  Search,
} from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "./api";
import { EffervaChat, EffervaRuntime } from "./EffervaRuntime";
import type { CreateThreadInput, ThreadSummary } from "./types";

const COMPOSER_SETTINGS_KEY = "efferva:composer-settings";

function readComposerSettings(): {
  model?: string;
  reasoningEffort?: string;
} {
  try {
    const saved: unknown = JSON.parse(
      window.localStorage.getItem(COMPOSER_SETTINGS_KEY) ?? "{}",
    );
    if (!saved || typeof saved !== "object") return {};
    const values = saved as Record<string, unknown>;
    return {
      model: typeof values.model === "string" ? values.model : undefined,
      reasoningEffort:
        typeof values.reasoningEffort === "string"
          ? values.reasoningEffort
          : undefined,
    };
  } catch {
    return {};
  }
}

export function App() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { sessionId, threadId } = useParams<{
    sessionId?: string;
    threadId?: string;
  }>();
  const [model, setModel] = useState("");
  const [reasoningEffort, setReasoningEffort] =
    useState<NonNullable<CreateThreadInput["reasoning_effort"]>>("low");
  const [search, setSearch] = useState("");

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
    if (!threadId || !threads.data || selectedThread) return;
    navigate(`/sessions/${sessionId}`, { replace: true });
  }, [navigate, selectedThread, sessionId, threadId, threads.data]);

  useEffect(() => {
    if (!models.data?.length) return;
    const current = models.data.find((item) => item.model === model);
    if (current) {
      if (
        !current.supportedReasoningEfforts.some(
          (item) => item.reasoningEffort === reasoningEffort,
        )
      ) {
        setReasoningEffort(current.defaultReasoningEffort);
      }
      return;
    }
    const saved = readComposerSettings();
    const nextModel =
      models.data.find((item) => item.model === saved.model) ??
      models.data.find((item) => item.isDefault) ??
      models.data[0];
    const savedEffort = nextModel.supportedReasoningEfforts.find(
      (item) => item.reasoningEffort === saved.reasoningEffort,
    );
    setModel(nextModel.model);
    setReasoningEffort(
      savedEffort?.reasoningEffort ?? nextModel.defaultReasoningEffort,
    );
  }, [model, models.data, reasoningEffort]);

  useEffect(() => {
    if (!model) return;
    try {
      window.localStorage.setItem(
        COMPOSER_SETTINGS_KEY,
        JSON.stringify({ model, reasoningEffort }),
      );
    } catch {}
  }, [model, reasoningEffort]);

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
    },
    [navigate, queryClient],
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
  const visibleThreads = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return threads.data ?? [];
    return (threads.data ?? []).filter((thread) =>
      thread.title.toLocaleLowerCase().includes(query),
    );
  }, [search, threads.data]);

  if (
    !sessionId ||
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
      sessionId={sessionId}
      threadId={threadId}
      model={model}
      reasoningEffort={reasoningEffort}
      models={models.data}
      onModelChange={setModel}
      onReasoningEffortChange={setReasoningEffort}
      workspace={selectedThread?.workspace}
      skills={enabledSkills}
      onThreadCreated={handleThreadCreated}
      onRunSettled={handleRunSettled}
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
          <label className="mb-3 flex items-center gap-2 rounded-lg border bg-background px-3 py-2 text-muted-foreground">
            <Search className="size-4" />
            <input
              className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search threads"
              aria-label="Search threads"
            />
          </label>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {visibleThreads.map((thread) => (
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
