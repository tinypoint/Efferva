const state = {
  principal: null,
  scope: "mine",
  sessions: [],
  sessionId: null,
  threads: [],
  threadId: null,
  eventSource: null,
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
  const thread = await request(`/api/threads/${id}`);
  $("#thread-title").textContent = thread.title || "Untitled thread";
  $("#empty").classList.add("hidden");
  $("#chat").classList.remove("hidden");
  renderMessages(thread.messages);
  renderSidebar();
  updateWriteControls();
  const activeRun = thread.runs.find((run) => ["queued", "running"].includes(run.status));
  if (activeRun) streamRun(activeRun);
}

function stopStreaming() {
  state.eventSource?.close();
  state.eventSource = null;
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
  $("#create-name").value = isSession ? "New workspace" : "New thread";
  dialog.showModal();
  $("#create-name").select();
}

function streamRun(run) {
  if (state.streamingRunId === run.id) return;
  stopStreaming();
  const threadId = state.threadId;
  const source = new EventSource(endpoint(`/api/runs/${run.id}/events/stream`));
  state.eventSource = source;
  state.streamingRunId = run.id;
  $("#run-status").textContent = "Run 执行中 · 可安全刷新页面";
  updateWriteControls();

  source.onmessage = async (message) => {
    const event = JSON.parse(message.data);
    if (threadId !== state.threadId) return;
    if (event.type === "TEXT_MESSAGE_START") {
      startStreamingMessage(run, event.messageId);
    } else if (event.type === "TEXT_MESSAGE_CONTENT") {
      appendStreamingMessage(run, event);
    }
    if (["RUN_FINISHED", "RUN_ERROR", "RUN_CANCELLED"].includes(event.type)) {
      stopStreaming();
      const thread = await request(`/api/threads/${threadId}`);
      renderMessages(thread.messages);
    }
  };

  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && state.eventSource === source) {
      stopStreaming();
    } else if (state.eventSource === source) {
      $("#run-status").textContent = "连接中断，正在自动补流…";
    }
  };
}

window.addEventListener("beforeunload", () => {
  state.eventSource?.close();
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
  if (!name) return;
  if (dialog.dataset.kind === "session") {
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
    body: JSON.stringify({ title: name }),
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
  const run = await request(`/api/threads/${state.threadId}/runs`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
  appendUserMessage(prompt);
  streamRun(run);
});

await Promise.all([loadMeta(), loadPrincipal()]);
await loadSessions();
updateWriteControls();
