import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  unstable_useLiveCompletionAdapter,
  unstable_useMentionAdapter,
  type Unstable_DirectiveFormatter,
  type Unstable_DirectiveSegment,
} from "@assistant-ui/react";
import {
  Code2,
  FileIcon,
  FolderIcon,
  ListTodoIcon,
  LoaderCircle,
  TargetIcon,
} from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { ComposerTriggerPopover } from "@/components/assistant-ui/composer-trigger-popover";
import { Thread } from "@/components/assistant-ui/thread";
import { ThreadList } from "@/components/assistant-ui/thread-list";
import { api } from "./api";
import { EffervaRuntime } from "./EffervaRuntime";
import type {
  CreateThreadInput,
  SkillMetadata,
  ThreadSummary,
} from "./types";

const SKILL_DIRECTIVE_FORMATTER: Unstable_DirectiveFormatter = {
  serialize(item) {
    return `$${item.id}`;
  },
  parse(text) {
    const segments: Unstable_DirectiveSegment[] = [];
    let lastIndex = 0;
    for (const match of text.matchAll(/\$([A-Za-z0-9:_-]+)/gu)) {
      if (match.index > lastIndex) {
        segments.push({
          kind: "text",
          text: text.slice(lastIndex, match.index),
        });
      }
      const name = match[1]!;
      segments.push({
        kind: "mention",
        type: "skill",
        id: name,
        label: name,
      });
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
      segments.push({ kind: "text", text: text.slice(lastIndex) });
    }
    return segments;
  },
};

const FILE_DIRECTIVE_FORMATTER: Unstable_DirectiveFormatter = {
  serialize(item) {
    return `@${item.id}`;
  },
  parse(text) {
    const segments: Unstable_DirectiveSegment[] = [];
    let lastIndex = 0;
    for (const match of text.matchAll(/@([^\s]+)/gu)) {
      if (match.index > lastIndex) {
        segments.push({
          kind: "text",
          text: text.slice(lastIndex, match.index),
        });
      }
      const path = match[1]!;
      segments.push({
        kind: "mention",
        type: "file",
        id: path,
        label: path,
      });
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
      segments.push({ kind: "text", text: text.slice(lastIndex) });
    }
    return segments;
  },
};

const COMMAND_DIRECTIVE_FORMATTER: Unstable_DirectiveFormatter = {
  serialize(item) {
    return `/${item.id} `;
  },
  parse(text) {
    const segments: Unstable_DirectiveSegment[] = [];
    let lastIndex = 0;
    for (const match of text.matchAll(/\/(plan|goal)\b/gu)) {
      if (match.index > lastIndex) {
        segments.push({
          kind: "text",
          text: text.slice(lastIndex, match.index),
        });
      }
      const command = match[1]!;
      segments.push({
        kind: "mention",
        type: "command",
        id: command,
        label: `/${command}`,
      });
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
      segments.push({ kind: "text", text: text.slice(lastIndex) });
    }
    return segments;
  },
};

function SkillPicker({ skills }: { skills: SkillMetadata[] }) {
  const mention = unstable_useMentionAdapter({
    items: skills.map((skill) => ({
      id: skill.name,
      type: "skill",
      label: skill.interface?.displayName || skill.name,
      description:
        skill.interface?.shortDescription ||
        skill.shortDescription ||
        skill.description,
    })),
    includeModelContextTools: false,
    formatter: SKILL_DIRECTIVE_FORMATTER,
  });
  return (
    <ComposerTriggerPopover
      char="$"
      {...mention}
      emptyItemsLabel="No matching skills"
    />
  );
}

function FilePicker({
  sessionId,
  workspace,
}: {
  sessionId: string;
  workspace?: string | null;
}) {
  const fetcher = useCallback(
    async (query: string) =>
      (await api.searchFiles(sessionId, query, workspace ?? undefined)).map(
        (file) => ({
          id: file.path,
          type: "file",
          label: file.file_name,
          description: file.path,
          metadata: { icon: file.match_type },
        }),
      ),
    [sessionId, workspace],
  );
  const files = unstable_useLiveCompletionAdapter({
    fetcher,
    debounceMs: 80,
  });
  return (
    <ComposerTriggerPopover
      char="@"
      adapter={files.adapter}
      isLoading={files.isLoading}
      directive={{ formatter: FILE_DIRECTIVE_FORMATTER }}
      iconMap={{ file: FileIcon, directory: FolderIcon }}
      fallbackIcon={FileIcon}
      emptyItemsLabel="No matching files"
    />
  );
}

function CommandPicker() {
  const commands = unstable_useMentionAdapter({
    items: [
      {
        id: "plan",
        type: "command",
        label: "/plan",
        description: "Enter Codex Plan mode, optionally with a prompt",
        icon: "plan",
      },
      {
        id: "goal",
        type: "command",
        label: "/goal",
        description: "Set, view, pause, resume, or clear the thread goal",
        icon: "goal",
      },
    ],
    includeModelContextTools: false,
    formatter: COMMAND_DIRECTIVE_FORMATTER,
    iconMap: { plan: ListTodoIcon, goal: TargetIcon },
  });
  return (
    <ComposerTriggerPopover
      char="/"
      {...commands}
      emptyItemsLabel="No matching commands"
    />
  );
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
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);

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
    enabled: Boolean(sessionId && !threadId),
  });
  const skillWorkspace = threads.data?.find(
    (item) => item.id === threadId,
  )?.workspace;
  const skills = useQuery({
    queryKey: ["skills", sessionId, skillWorkspace],
    queryFn: () => api.listSkills(sessionId!, skillWorkspace ?? undefined),
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
    if (threadId || !models.data?.length) return;
    const current = models.data.find((item) => item.model === model);
    if (current) {
      const supportsCurrentEffort = current.supportedReasoningEfforts.some(
        (item) => item.reasoningEffort === reasoningEffort,
      );
      if (!supportsCurrentEffort) {
        setReasoningEffort(current.defaultReasoningEffort);
      }
      return;
    }
    const defaultModel =
      models.data.find((item) => item.isDefault) ?? models.data[0];
    setModel(defaultModel.model);
    setReasoningEffort(defaultModel.defaultReasoningEffort);
  }, [model, models.data, reasoningEffort, threadId]);

  const handleThreadCreated = useCallback(
    (thread: ThreadSummary) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", thread.session_id],
        (current = []) => [
          thread,
          ...current.filter((item) => item.id !== thread.id),
        ],
      );
      void queryClient.invalidateQueries({
        queryKey: ["threads", thread.session_id],
      });
      navigate(`/sessions/${thread.session_id}/threads/${thread.id}`, {
        replace: true,
      });
    },
    [navigate, queryClient],
  );

  const handleThreadDeleted = useCallback(
    (deletedThreadId: string) => {
      queryClient.setQueryData<ThreadSummary[]>(
        ["threads", sessionId],
        (current = []) =>
          current.filter((item) => item.id !== deletedThreadId),
      );
      if (threadId === deletedThreadId) {
        setQueuedMessages([]);
        navigate(`/sessions/${sessionId}`);
      }
    },
    [navigate, queryClient, sessionId, threadId],
  );

  const selectedModel = models.data?.find((item) => item.model === model);
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
  const selectedThread = threads.data?.find((item) => item.id === threadId);
  const ComposerTriggers = useCallback(
    () => (
      <>
        <FilePicker
          sessionId={sessionId!}
          workspace={selectedThread?.workspace}
        />
        <SkillPicker skills={enabledSkills} />
        <CommandPicker />
      </>
    ),
    [enabledSkills, selectedThread?.workspace, sessionId],
  );

  const ComposerControls = useCallback(
    () => (
      <>
        <select
          className="h-7 max-w-40 rounded-md border bg-background px-2 text-xs outline-none"
          value={model}
          onChange={(event) => {
            const nextModel = models.data?.find(
              (item) => item.model === event.target.value,
            );
            setModel(event.target.value);
            if (nextModel) {
              setReasoningEffort(nextModel.defaultReasoningEffort);
            }
          }}
          aria-label="Model"
        >
          {models.data?.map((item) => (
            <option key={item.id} value={item.model}>
              {item.displayName}
            </option>
          ))}
        </select>
        <select
          className="h-7 rounded-md border bg-background px-2 text-xs outline-none"
          value={reasoningEffort}
          onChange={(event) => setReasoningEffort(event.target.value)}
          aria-label="Reasoning effort"
        >
          {selectedModel?.supportedReasoningEfforts.map((item) => (
            <option
              key={item.reasoningEffort}
              value={item.reasoningEffort}
              title={item.description}
            >
              {item.reasoningEffort}
            </option>
          ))}
        </select>
      </>
    ),
    [model, models.data, reasoningEffort, selectedModel],
  );

  if (
    !sessionId ||
    threads.isLoading ||
    !threads.data ||
    (!threadId && (models.isLoading || !models.data))
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
      threads={threads.data}
      model={model}
      reasoningEffort={reasoningEffort}
      queuedMessages={queuedMessages}
      setQueuedMessages={setQueuedMessages}
      onNewThread={() => {
        setQueuedMessages([]);
        navigate(`/sessions/${sessionId}`);
      }}
      onOpenThread={(nextThreadId) => {
        setQueuedMessages([]);
        navigate(`/sessions/${sessionId}/threads/${nextThreadId}`);
      }}
      onThreadCreated={handleThreadCreated}
      onThreadDeleted={handleThreadDeleted}
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
          <div className="min-h-0 flex-1 overflow-y-auto">
            <ThreadList />
          </div>
        </aside>
        <main className="grid min-h-0 min-w-0 grid-rows-[3.5rem_minmax(0,1fr)]">
          <header className="flex items-center border-b px-5">
            <h1 className="truncate text-sm font-semibold">
              {selectedThread?.title || "New thread"}
            </h1>
          </header>
          <div className="min-h-0">
            <Thread
              components={{
                ...(!threadId ? { ComposerControls } : {}),
                ComposerTriggers,
              }}
            />
          </div>
        </main>
      </div>
    </EffervaRuntime>
  );
}
