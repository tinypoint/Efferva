import { FormEvent, useEffect, useMemo, useState } from "react";

type Session = { id: string; name: string };
type Thread = {
  session_id: string;
  summary: string;
  last_modified: number;
};
type Snapshot = {
  thread: Thread;
  messages: unknown[];
  active: boolean;
};
type LiveEvent = { event: string; data: Record<string, unknown> };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/agent${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(String(payload.detail ?? `HTTP ${response.status}`));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<unknown[]>([]);
  const [prompt, setPrompt] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedSession = useMemo(
    () => sessions.find((session) => session.id === sessionId),
    [sessions, sessionId],
  );

  async function loadSessions() {
    const next = await request<Session[]>("/api/sessions");
    setSessions(next);
    setSessionId((current) => current ?? next[0]?.id ?? null);
  }

  async function loadThreads(id: string) {
    const next = await request<Thread[]>(`/api/sessions/${id}/claude/threads`);
    setThreads(next);
  }

  async function loadThread(session: string, thread: string) {
    const snapshot = await request<Snapshot>(
      `/api/sessions/${session}/claude/threads/${thread}`,
    );
    setMessages(snapshot.messages);
  }

  useEffect(() => {
    loadSessions().catch((reason) => setError(String(reason)));
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    setThreadId(null);
    setMessages([]);
    loadThreads(sessionId).catch((reason) => setError(String(reason)));
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId || !threadId) return;
    loadThread(sessionId, threadId).catch((reason) => setError(String(reason)));
  }, [sessionId, threadId]);

  async function createSession() {
    const name = window.prompt("Session name", "Claude workspace");
    if (!name?.trim()) return;
    const created = await request<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadSessions();
    setSessionId(created.id);
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!sessionId || !prompt.trim() || running) return;
    const text = prompt.trim();
    setPrompt("");
    setError(null);
    setRunning(true);
    setMessages((current) => [
      ...current,
      { type: "user", message: { role: "user", content: text } },
    ]);
    try {
      const response = await fetch(
        `/agent/api/sessions/${sessionId}/claude/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ threadId: threadId ?? "new", prompt: text }),
        },
      );
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(String(payload.detail ?? `HTTP ${response.status}`));
      }
      let discoveredThread = threadId;
      for await (const item of readSse(response.body)) {
        if (item.event === "error") {
          throw new Error(String(item.data.message ?? "Claude run failed"));
        }
        discoveredThread = threadFromEvent(item) ?? discoveredThread;
        if (item.event === "message") {
          const type = String(item.data.type ?? "");
          if (type === "StreamEvent") {
            setMessages((current) => applyDelta(current, item.data));
          } else if (type === "AssistantMessage") {
            setMessages((current) => [
              ...current.filter((message) => !object(message)._live),
              item.data,
            ]);
          }
        }
      }
      if (discoveredThread) {
        setThreadId(discoveredThread);
        await loadThread(sessionId, discoveredThread);
      }
      await loadThreads(sessionId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRunning(false);
    }
  }

  async function removeThread(id: string) {
    if (!sessionId || !window.confirm("Delete this Thread transcript?")) return;
    await request(`/api/sessions/${sessionId}/claude/threads/${id}`, {
      method: "DELETE",
    });
    if (threadId === id) {
      setThreadId(null);
      setMessages([]);
    }
    await loadThreads(sessionId);
  }

  return (
    <main className="shell">
      <aside className="sessions">
        <div className="brand">Efferva <span>Claude Code</span></div>
        <button className="primary" onClick={() => createSession().catch(console.error)}>
          + New Session
        </button>
        {sessions.map((session) => (
          <button
            className={session.id === sessionId ? "selected" : ""}
            key={session.id}
            onClick={() => setSessionId(session.id)}
          >
            {session.name}
          </button>
        ))}
      </aside>

      <aside className="threads">
        <h2>{selectedSession?.name ?? "No Session"}</h2>
        <button
          className="primary"
          disabled={!sessionId}
          onClick={() => {
            setThreadId(null);
            setMessages([]);
          }}
        >
          + New Thread
        </button>
        {threads.map((thread) => (
          <div className="thread-row" key={thread.session_id}>
            <button
              className={thread.session_id === threadId ? "selected" : ""}
              onClick={() => setThreadId(thread.session_id)}
            >
              {thread.summary || "Untitled Thread"}
            </button>
            <button className="delete" onClick={() => removeThread(thread.session_id)}>
              ×
            </button>
          </div>
        ))}
      </aside>

      <section className="conversation">
        <header>{threadId ? "Thread" : "New Thread"}</header>
        <div className="messages">
          {messages.length === 0 && (
            <div className="empty">Ask Claude to inspect or change this Session workspace.</div>
          )}
          {messages.map((message, index) => (
            <Message key={index} value={message} />
          ))}
          {running && <div className="status">Claude is working…</div>}
          {error && <div className="error">{error}</div>}
        </div>
        <form onSubmit={send}>
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            disabled={!sessionId || running}
            placeholder="Send a message to Claude Code…"
          />
          <button className="send" disabled={!sessionId || running || !prompt.trim()}>
            ↑
          </button>
        </form>
      </section>
    </main>
  );
}

function Message({ value }: { value: unknown }) {
  const record = object(value);
  const envelope = object(record.message);
  const rawMessage = object(envelope.message ?? record.message);
  const type = String(record.type ?? envelope.type ?? rawMessage.role ?? "message");
  const role = type.toLowerCase().includes("user") ? "user" : "assistant";
  const content = rawMessage.content ?? envelope.content ?? record.content;
  const blocks = Array.isArray(content) ? content : [content];
  return (
    <article className={`message ${role}`}>
      <div className="role">{role === "user" ? "You" : "Claude"}</div>
      {blocks.filter(Boolean).map((block, index) => (
        <Block key={index} value={block} />
      ))}
    </article>
  );
}

function Block({ value }: { value: unknown }) {
  if (typeof value === "string") return <p>{value}</p>;
  const block = object(value);
  const type = String(block.type ?? "");
  if (typeof block.text === "string") return <p>{block.text}</p>;
  if (typeof block.thinking === "string") {
    return <details><summary>Thinking</summary><pre>{block.thinking}</pre></details>;
  }
  if (type === "tool_use" || type === "ToolUseBlock") {
    return <details open><summary>Tool · {String(block.name ?? "unknown")}</summary><pre>{pretty(block.input)}</pre></details>;
  }
  if (type === "tool_result" || type === "ToolResultBlock") {
    return <details><summary>Tool result</summary><pre>{pretty(block.content)}</pre></details>;
  }
  return <pre>{pretty(value)}</pre>;
}

function object(value: unknown): Record<string, any> {
  return value && typeof value === "object" ? value as Record<string, any> : {};
}

function pretty(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function threadFromEvent(item: LiveEvent): string | null {
  if (item.event === "done" && typeof item.data.threadId === "string") {
    return item.data.threadId;
  }
  const message = object(item.data.message);
  if (typeof message.session_id === "string") return message.session_id;
  const data = object(message.data);
  return typeof data.session_id === "string" ? data.session_id : null;
}

function applyDelta(
  messages: unknown[],
  envelope: Record<string, unknown>,
): unknown[] {
  const event = object(object(envelope.message).event);
  if (event.type !== "content_block_delta") return messages;
  const delta = object(event.delta);
  const field = typeof delta.text === "string"
    ? "text"
    : typeof delta.thinking === "string"
      ? "thinking"
      : null;
  if (!field) return messages;
  const chunk = String(delta[field]);
  const last = object(messages.at(-1));
  if (!last._live) {
    return [
      ...messages,
      {
        _live: true,
        type: "AssistantMessage",
        message: { content: [{ type: field, [field]: chunk }] },
      },
    ];
  }
  const content = [...(object(last.message).content as unknown[] ?? [])];
  const block = object(content.at(-1));
  if (block.type === field) {
    content[content.length - 1] = {
      ...block,
      [field]: String(block[field] ?? "") + chunk,
    };
  } else {
    content.push({ type: field, [field]: chunk });
  }
  return [
    ...messages.slice(0, -1),
    { ...last, message: { ...object(last.message), content } },
  ];
}

async function* readSse(stream: ReadableStream<Uint8Array>): AsyncGenerator<LiveEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim() ?? "message";
        const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
        if (data) yield { event, data: JSON.parse(data) };
        boundary = buffer.indexOf("\n\n");
      }
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}
