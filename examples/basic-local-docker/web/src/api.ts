import type {
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
};
