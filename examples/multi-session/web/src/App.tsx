import {
  FormEvent,
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { useNavigate, useParams } from "react-router-dom";

import { createAdapter } from "./adapters";
import type {
  AgentEvent,
  ChatMessage,
  EngineId,
  MessageBlock,
  Session,
  Thread,
} from "./types";

export function App() {
  const navigate = useNavigate();
  const params = useParams<{
    engine?: string;
    sessionId?: string;
    threadId?: string;
  }>();
  const engine: EngineId = params.engine === "claude" ? "claude" : "codex";
  const sessionId = params.sessionId;
  const threadId = params.threadId;
  const adapter = useMemo(() => createAdapter(engine), [engine]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => () => adapter.dispose(), [adapter]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    adapter
      .listSessions()
      .then((items) => {
        if (!active) return;
        setSessions(items);
        if (!sessionId && items[0]) {
          navigate(`/${engine}/sessions/${items[0].id}`, { replace: true });
        }
      })
      .catch(showError)
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [adapter, engine, navigate, sessionId]);

  useEffect(() => {
    if (!sessionId) {
      setThreads([]);
      setMessages([]);
      return;
    }
    let active = true;
    adapter
      .listThreads(sessionId)
      .then((items) => active && setThreads(items))
      .catch(showError);
    return () => {
      active = false;
    };
  }, [adapter, sessionId]);

  useEffect(() => {
    if (!sessionId || !threadId || running) {
      if (!threadId && !running) setMessages([]);
      return;
    }
    let active = true;
    setLoading(true);
    adapter
      .readThread(sessionId, threadId)
      .then((snapshot) => active && setMessages(snapshot.messages))
      .catch(showError)
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [adapter, running, sessionId, threadId]);

  function showError(reason: unknown): void {
    setError(reason instanceof Error ? reason.message : String(reason));
  }

  async function newSession(): Promise<void> {
    const name = window.prompt("Session name", `${adapter.label} workspace`);
    if (!name?.trim()) return;
    try {
      const session = await adapter.createSession(name.trim());
      setSessions((current) => [session, ...current]);
      navigate(`/${engine}/sessions/${session.id}`);
    } catch (reason) {
      showError(reason);
    }
  }

  async function removeThread(thread: Thread): Promise<void> {
    if (!sessionId || !window.confirm(`Delete “${thread.title}”?`)) return;
    try {
      await adapter.deleteThread(sessionId, thread.id);
      setThreads((current) => current.filter((item) => item.id !== thread.id));
      if (thread.id === threadId) {
        setMessages([]);
        navigate(`/${engine}/sessions/${sessionId}`);
      }
    } catch (reason) {
      showError(reason);
    }
  }

  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    const text = prompt.trim();
    if (!sessionId || !text || running) return;
    setPrompt("");
    setError(null);
    setRunning(true);
    setMessages((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        role: "user",
        blocks: [{ type: "text", text }],
      },
      {
        id: crypto.randomUUID(),
        role: "assistant",
        blocks: [],
        live: true,
      },
    ]);
    let activeThread = threadId ?? "new";
    try {
      for await (const item of adapter.sendMessage(sessionId, activeThread, text)) {
        if (item.type === "thread") {
          activeThread = item.thread.id;
          setThreads((current) => [
            item.thread,
            ...current.filter((thread) => thread.id !== item.thread.id),
          ]);
          navigate(`/${engine}/sessions/${sessionId}/threads/${item.thread.id}`, {
            replace: true,
          });
          continue;
        }
        applyEvent(item, setMessages, setError);
        if (item.type === "done") activeThread = item.threadId;
      }
      if (activeThread !== "new") {
        const snapshot = await adapter.readThread(sessionId, activeThread);
        setMessages(snapshot.messages);
      }
      setThreads(await adapter.listThreads(sessionId));
    } catch (reason) {
      showError(reason);
    } finally {
      setRunning(false);
    }
  }

  const selectedThread = threads.find((thread) => thread.id === threadId);

  return (
    <main className="shell">
      <aside className="sessions">
        <div className="brand">
          Efferva <span>Multi-engine agent</span>
        </div>
        <div className="engine-switcher">
          {(["codex", "claude"] as EngineId[]).map((id) => (
            <button
              className={id === engine ? "selected" : ""}
              key={id}
              onClick={() => navigate(`/${id}`)}
            >
              {id === "codex" ? "Codex" : "Claude"}
            </button>
          ))}
        </div>
        <button className="primary" onClick={() => void newSession()}>
          + New Session
        </button>
        {sessions.map((session) => (
          <button
            className={session.id === sessionId ? "selected" : ""}
            key={session.id}
            onClick={() => navigate(`/${engine}/sessions/${session.id}`)}
          >
            {session.name}
          </button>
        ))}
      </aside>

      <aside className="threads">
        <h2>{sessions.find((session) => session.id === sessionId)?.name ?? adapter.label}</h2>
        <button
          className="primary"
          disabled={!sessionId}
          onClick={() => {
            setMessages([]);
            navigate(`/${engine}/sessions/${sessionId}`);
          }}
        >
          + New Thread
        </button>
        {threads.map((thread) => (
          <div className="thread-row" key={thread.id}>
            <button
              className={thread.id === threadId ? "selected" : ""}
              onClick={() =>
                navigate(`/${engine}/sessions/${sessionId}/threads/${thread.id}`)
              }
            >
              {thread.title}
            </button>
            <button
              className="delete"
              aria-label={`Delete ${thread.title}`}
              onClick={() => void removeThread(thread)}
            >
              ×
            </button>
          </div>
        ))}
      </aside>

      <section className="conversation">
        <header>
          <strong>{selectedThread?.title ?? (threadId ? "Thread" : "New Thread")}</strong>
          <span>{adapter.label}</span>
        </header>
        <div className="messages">
          {!messages.length && !loading && (
            <div className="empty">
              {sessionId
                ? `Ask ${adapter.label} to inspect or change this workspace.`
                : "Create a Session to start."}
            </div>
          )}
          {messages.map((message) => (
            <Message key={message.id} message={message} />
          ))}
          {running && <div className="status">{adapter.label} is working…</div>}
          {error && <div className="error">{error}</div>}
        </div>
        <form onSubmit={(event) => void send(event)}>
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
            placeholder={`Send a message to ${adapter.label}…`}
          />
          <button
            className="send"
            disabled={!sessionId || running || !prompt.trim()}
          >
            ↑
          </button>
        </form>
      </section>
    </main>
  );
}

function Message({ message }: { message: ChatMessage }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="role">{message.role === "user" ? "You" : "Agent"}</div>
      {message.blocks.map((block, index) => (
        <Block key={index} block={block} />
      ))}
    </article>
  );
}

function Block({ block }: { block: MessageBlock }) {
  if (block.type === "text") return <p>{block.text}</p>;
  if (block.type === "thinking") {
    return (
      <details>
        <summary>Thinking</summary>
        <pre>{block.text}</pre>
      </details>
    );
  }
  return (
    <details open>
      <summary>{block.error ? "Failed tool" : "Tool"} · {block.name}</summary>
      {block.input !== undefined && <pre>{pretty(block.input)}</pre>}
      {block.output !== undefined && <pre>{pretty(block.output)}</pre>}
    </details>
  );
}

function applyEvent(
  event: AgentEvent,
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>,
  setError: Dispatch<SetStateAction<string | null>>,
): void {
  if (event.type === "error") {
    setError(event.message);
    return;
  }
  if (event.type === "thread" || event.type === "done") return;
  setMessages((current) => {
    const messages = [...current];
    let index = -1;
    for (let cursor = messages.length - 1; cursor >= 0; cursor -= 1) {
      if (messages[cursor].role === "assistant" && messages[cursor].live) {
        index = cursor;
        break;
      }
    }
    if (index < 0) return current;
    if (event.type === "message") {
      messages[index] = { ...event.message, id: messages[index].id, live: true };
      return messages;
    }
    const message = { ...messages[index], blocks: [...messages[index].blocks] };
    if (event.type === "tool") {
      message.blocks.push(event.block);
    } else {
      const blockType = event.type;
      const last = message.blocks.at(-1);
      if (last?.type === blockType && (last.type === "text" || last.type === "thinking")) {
        message.blocks[message.blocks.length - 1] = {
          ...last,
          text: last.text + event.delta,
        };
      } else {
        message.blocks.push({ type: blockType, text: event.delta });
      }
    }
    messages[index] = message;
    return messages;
  });
}

function pretty(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
