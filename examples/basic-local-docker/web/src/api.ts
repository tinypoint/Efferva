import type {
  CreateThreadInput,
  ExecutionSettings,
  FileSearchResult,
  ModelOption,
  Session,
  SkillListEntry,
  ThreadHistoryPage,
  ThreadSummary,
} from "./types";

const API_ROOT = "/agent/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
    cache: "no-store",
    ...init,
    headers: {
      "content-type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new ApiError(body.detail || response.statusText, response.status);
  }
  return response.json() as Promise<T>;
}

async function* streamEvents(
  path: string,
  signal?: AbortSignal,
): AsyncGenerator<Record<string, unknown>> {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(response.statusText || "Unable to resume the turn");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = block
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) yield JSON.parse(data) as Record<string, unknown>;
      boundary = buffer.indexOf("\n\n");
    }
    if (done) return;
  }
}

export const api = {
  listSessions: () => request<Session[]>("/sessions"),
  createSession: (name: string) =>
    request<Session>("/sessions", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  listThreads: (sessionId: string) =>
    request<ThreadSummary[]>(`/sessions/${sessionId}/threads`),
  listModels: (sessionId: string) =>
    request<ModelOption[]>(`/sessions/${sessionId}/models`),
  getExecutionSettings: (sessionId: string, threadId?: string) =>
    request<ExecutionSettings>(
      threadId
        ? `/sessions/${sessionId}/threads/${threadId}/settings`
        : `/sessions/${sessionId}/settings`,
    ),
  updateExecutionSettings: (
    sessionId: string,
    settings: Required<ExecutionSettings>,
    threadId?: string,
  ) =>
    request<ExecutionSettings>(
      threadId
        ? `/sessions/${sessionId}/threads/${threadId}/settings`
        : `/sessions/${sessionId}/settings`,
      {
        method: "PUT",
        body: JSON.stringify(settings),
      },
    ),
  listSkills: (sessionId: string, workspace?: string) => {
    const query = workspace
      ? `?workspace=${encodeURIComponent(workspace)}`
      : "";
    return request<SkillListEntry[]>(
      `/sessions/${sessionId}/skills${query}`,
    );
  },
  searchFiles: (sessionId: string, query: string, workspace?: string) => {
    const params = new URLSearchParams({ query });
    if (workspace) params.set("workspace", workspace);
    return request<FileSearchResult[]>(
      `/sessions/${sessionId}/files?${params.toString()}`,
    );
  },
  createThread: (sessionId: string, input: CreateThreadInput) =>
    request<ThreadSummary>(`/sessions/${sessionId}/threads`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  loadThreadHistoryPage: (
    sessionId: string,
    threadId: string,
    options?: { cursor?: string; signal?: AbortSignal },
  ) => {
    const params = new URLSearchParams();
    if (options?.cursor) params.set("cursor", options.cursor);
    const encodedParams = params.toString();
    const query = encodedParams ? `?${encodedParams}` : "";
    return request<ThreadHistoryPage>(
      `/sessions/${sessionId}/threads/${threadId}/ag-ui${query}`,
      { signal: options?.signal },
    );
  },
  deleteThread: (sessionId: string, threadId: string) =>
    request<{ deleted: boolean }>(
      `/sessions/${sessionId}/threads/${threadId}`,
      { method: "DELETE" },
    ),
  resumeThread: (
    sessionId: string,
    threadId: string,
    turnId: string,
    signal?: AbortSignal,
  ) =>
    streamEvents(
      `/sessions/${sessionId}/threads/${threadId}/resume?turn_id=${encodeURIComponent(turnId)}`,
      signal,
    ),
  interruptTurn: (
    sessionId: string,
    threadId: string,
    turnId: string,
  ) =>
    request<{ interrupted: boolean }>(
      `/sessions/${sessionId}/threads/${threadId}/turns/${turnId}/interrupt`,
      { method: "POST" },
    ),
  steerTurn: (
    sessionId: string,
    threadId: string,
    turnId: string,
    prompt: string,
  ) =>
    request<{ turnId: string }>(
      `/sessions/${sessionId}/threads/${threadId}/turns/${turnId}/steer`,
      {
        method: "POST",
        body: JSON.stringify({ prompt }),
      },
    ),
};
