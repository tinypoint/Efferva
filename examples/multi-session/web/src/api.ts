import type { Session } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function request<T>(
  prefix: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${prefix}${path}`, {
    credentials: "same-origin",
    cache: "no-store",
    ...init,
    headers: {
      "content-type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new ApiError(payload.detail ?? `HTTP ${response.status}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listSessions(prefix: string): Promise<Session[]> {
  return request(prefix, "/api/sessions");
}

export function createSession(prefix: string, name: string): Promise<Session> {
  return request(prefix, "/api/sessions", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}
