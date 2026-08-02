import type { Session } from "./types";

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
};
