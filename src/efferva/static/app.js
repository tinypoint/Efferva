const state = {
  principal: null,
  scope: "mine",
  sessions: [],
  sessionId: null,
  threads: [],
  threadId: null,
  streamAbortController: null,
  streamingRunId: null,
};

const $ = (selector) => document.querySelector(selector);
const basePath = new URL(".", document.baseURI).pathname.replace(/\/$/, "");
const endpoint = (path) => `${basePath}${path}`;

async function request(path, options = {}) {
  const { headers = {}, ...requestOptions } = options;
  const response = await fetch(endpoint(path), {
    credentials: "same-origin",
    ...requestOptions,
    headers: { "content-type": "application/json", ...headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value;
  return node.innerHTML;
}

function hasCapability(capability) {
  return state.principal?.capabilities.includes(capability) || false;
}

function currentSession() {
  return state.sessions.find((session) => session.id === state.sessionId);
}

function ownsSession(session) {
  return (
    session?.tenant_id === state.principal?.tenant_id &&
    session?.owner_issuer === state.principal?.issuer &&
    session?.owner_subject === state.principal?.subject
  );
}

function canWriteSession(session) {
  return ownsSession(session) || hasCapability("sessions:write:tenant");
}

function updateWriteControls() {
  const writable = canWriteSession(currentSession());
  $("#new-thread").disabled = !state.sessionId || !writable;
  const runActive = state.streamingRunId !== null;
  $("#prompt").disabled = !state.threadId || !writable || runActive;
  $("#send").disabled = !state.threadId || !writable || runActive;
  $("#read-only").classList.toggle("hidden", !state.threadId || writable);
}

function renderSidebar() {
  const container = $("#sessions");
  container.innerHTML = state.sessions
    .map((session) => {
      const active = session.id === state.sessionId ? "active" : "";
      const owner =
        state.scope === "tenant"
          ? `<small class="session-owner">${escapeHtml(session.owner_subject)}</small>`
          : "";
      const threads =
        session.id === state.sessionId
          ? state.threads
              .map(
                (thread) =>
                  `<button class="thread ${thread.id === state.threadId ? "active" : ""}" data-thread="${thread.id}">${escapeHtml(thread.title || "Untitled thread")}</button>`,
              )
              .join("")
          : "";
      return `<div><button class="session ${active}" data-session="${session.id}"><span>${escapeHtml(session.name)}</span>${owner}</button>${threads}</div>`;
    })
    .join("");
}

async function loadSessions() {
  state.sessions = await request(`/api/sessions?scope=${state.scope}`);
  renderSidebar();
}

async function loadMeta() {
  const meta = await request("/api/meta");
  $("#product-title").textContent = meta.title;
  document.title = meta.title;
}

async function loadPrincipal() {
  state.principal = await request("/api/me");
  $("#principal").textContent = `${state.principal.subject} · ${state.principal.tenant_id}`;
  $("#session-scope").classList.toggle(
    "hidden",
    !hasCapability("sessions:read:tenant"),
  );
}

async function changeScope(scope) {
  if (scope === state.scope) return;
  stopStreaming();
  state.scope = scope;
  state.sessionId = null;
  state.threadId = null;
  state.threads = [];
  $("#scope-mine").classList.toggle("active", scope === "mine");
  $("#scope-tenant").classList.toggle("active", scope === "tenant");
  $("#session-label").textContent = "选择一个 Session";
  $("#thread-title").textContent = "开始构建";
  $("#empty").classList.remove("hidden");
  $("#chat").classList.add("hidden");
  await loadSessions();
  updateWriteControls();
}

async function selectSession(id) {
  stopStreaming();
  state.sessionId = id;
  state.threadId = null;
  state.threads = await request(`/api/sessions/${id}/threads`);
  const session = currentSession();
  $("#session-label").textContent = session?.name || "Session";
  $("#thread-title").textContent = "选择或创建 Thread";
  $("#empty").classList.remove("hidden");
  $("#chat").classList.add("hidden");
  renderSidebar();
  updateWriteControls();
}

function renderMessages(messages) {
  $("#messages").innerHTML = messages
    .map(
      (message) =>
        `<article class="message ${message.role}"${message.run_id ? ` data-run-id="${message.run_id}"` : ""}>${escapeHtml(message.content)}</article>`,
    )
    .join("");
  scrollMessages();
}

function scrollMessages() {
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function findStreamingMessage(messageId) {
  return [...$("#messages").querySelectorAll("[data-message-id]")].find(
    (node) => node.dataset.messageId === messageId,
  );
}

function startStreamingMessage(run, messageId) {
  let node = findStreamingMessage(messageId);
  if (node) {
    node.textContent = "";
    return node;
  }
  node = [...$("#messages").querySelectorAll(".message.assistant[data-run-id]")].find(
    (candidate) => candidate.dataset.runId === run.id,
  );
  if (node) {
    node.dataset.messageId = messageId;
    node.textContent = "";
    return node;
  }
  node = document.createElement("article");
  node.className = "message assistant";
  node.dataset.runId = run.id;
  node.dataset.messageId = messageId;
  $("#messages").append(node);
  scrollMessages();
  return node;
}

function appendStreamingMessage(run, event) {
  const node =
    findStreamingMessage(event.messageId) ||
    startStreamingMessage(run, event.messageId);
  node.textContent += event.delta || "";
  scrollMessages();
}

function appendUserMessage(content) {
  const node = document.createElement("article");
  node.className = "message user";
  node.textContent = content;
  $("#messages").append(node);
  scrollMessages();
}

async function selectThread(id) {
  stopStreaming();
  state.threadId = id;
  const thread = await request(`/api/sessions/${state.sessionId}/threads/${id}`);
  $("#thread-title").textContent = thread.title || "Untitled thread";
  $("#empty").classList.add("hidden");
  $("#chat").classList.remove("hidden");
  renderMessages(thread.messages);
  renderSidebar();
  updateWriteControls();
}

function stopStreaming() {
  state.streamAbortController?.abort();
  state.streamAbortController = null;
  state.streamingRunId = null;
  $("#run-status").textContent = "";
  updateWriteControls();
}

function openCreator(kind) {
  const isSession = kind === "session";
  const dialog = $("#create-dialog");
  dialog.dataset.kind = kind;
  $("#create-eyebrow").textContent = isSession ? "共享工作区" : "当前 Session";
  $("#create-title").textContent = isSession ? "新建 Session" : "新建 Thread";
  $("#create-label").textContent = isSession ? "Session 名称" : "Thread 名称";
  $("#create-label").classList.toggle("hidden", !isSession);
  $("#create-name").classList.toggle("hidden", !isSession);
  $("#create-name").required = isSession;
  $("#create-name").value = isSession ? "New workspace" : "";
  dialog.showModal();
  if (isSession) $("#create-name").select();
}

async function streamTurn(prompt) {
  stopStreaming();
  const run = { id: crypto.randomUUID() };
  const sessionId = state.sessionId;
  const threadId = state.threadId;
  const controller = new AbortController();
  state.streamAbortController = controller;
  state.streamingRunId = run.id;
  $("#run-status").textContent = "Codex 执行中 · 刷新后从 Thread 快照恢复";
  updateWriteControls();

  try {
    const response = await fetch(
      endpoint(`/api/sessions/${sessionId}/threads/${threadId}/turns`),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ prompt }),
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(body.detail || response.statusText);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const data = frame
          .split("\n")
          .find((line) => line.startsWith("data: "));
        if (!data || threadId !== state.threadId) continue;
        const event = JSON.parse(data.slice(6));
        if (event.type === "TEXT_MESSAGE_START") {
          startStreamingMessage(run, event.messageId);
        } else if (event.type === "TEXT_MESSAGE_CONTENT") {
          appendStreamingMessage(run, event);
        } else if (event.type === "RUN_ERROR") {
          $("#run-status").textContent = event.message || "Codex 执行失败";
        }
      }
      if (done) break;
    }
    if (threadId === state.threadId && sessionId === state.sessionId) {
      const thread = await request(
        `/api/sessions/${sessionId}/threads/${threadId}`,
      );
      renderMessages(thread.messages);
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      $("#run-status").textContent =
        "浏览器连接已断开；Codex 仍在 Sandbox 中执行，刷新 Thread 可恢复状态";
    }
  } finally {
    if (state.streamAbortController === controller) {
      state.streamAbortController = null;
      state.streamingRunId = null;
      updateWriteControls();
    }
  }
}

window.addEventListener("beforeunload", () => {
  state.streamAbortController?.abort();
});

$("#sessions").addEventListener("click", (event) => {
  const session = event.target.closest("[data-session]");
  const thread = event.target.closest("[data-thread]");
  if (thread) selectThread(thread.dataset.thread);
  else if (session) selectSession(session.dataset.session);
});

$("#scope-mine").addEventListener("click", () => changeScope("mine"));
$("#scope-tenant").addEventListener("click", () => changeScope("tenant"));
$("#new-session").addEventListener("click", () => openCreator("session"));
$("#new-thread").addEventListener("click", () => openCreator("thread"));
$("#create-cancel").addEventListener("click", () => $("#create-dialog").close());

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const dialog = $("#create-dialog");
  const name = $("#create-name").value.trim();
  if (dialog.dataset.kind === "session") {
    if (!name) return;
    const session = await request("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    dialog.close();
    await loadSessions();
    await selectSession(session.id);
    return;
  }
  const thread = await request(`/api/sessions/${state.sessionId}/threads`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  dialog.close();
  state.threads.unshift(thread);
  await selectThread(thread.id);
});

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!canWriteSession(currentSession())) return;
  const prompt = $("#prompt").value.trim();
  if (!prompt) return;
  $("#prompt").value = "";
  appendUserMessage(prompt);
  await streamTurn(prompt);
});

await Promise.all([loadMeta(), loadPrincipal()]);
await loadSessions();
updateWriteControls();
