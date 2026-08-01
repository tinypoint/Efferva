import type {
  CreateThreadInput,
  FileSearchResult,
  ModelOption,
  Session,
  SkillListEntry,
  ThreadDetail,
  ThreadSummary,
} from "./types";

const API_ROOT = "/agent/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
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
    throw new Error(body.detail || response.statusText);
  }
  return response.json() as Promise<T>;
}

async function* streamEvents(
  path: string,
  signal?: AbortSignal,
): AsyncGenerator<Record<string, unknown>> {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
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
  readThread: (sessionId: string, threadId: string) =>
    request<ThreadDetail>(`/sessions/${sessionId}/threads/${threadId}`),
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
