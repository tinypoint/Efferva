const API = "/agent/api";

const state = {
  sessions: [],
  sessionId: null,
  threads: [],
  threadId: null,
  streaming: false,
};

const $ = (selector) => document.querySelector(selector);

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function currentSession() {
  return state.sessions.find((session) => session.id === state.sessionId);
}

function renderNavigation() {
  $("#sessions").innerHTML = state.sessions
    .map((session) => {
      const threads =
        session.id === state.sessionId
          ? state.threads
              .map(
                (thread) =>
                  `<button class="thread ${thread.id === state.threadId ? "active" : ""}" data-thread="${escapeHtml(thread.id)}">${escapeHtml(thread.title || "Untitled thread")}</button>`,
              )
              .join("")
          : "";
      return `
        <div>
          <button class="session ${session.id === state.sessionId ? "active" : ""}" data-session="${escapeHtml(session.id)}">
            ${escapeHtml(session.name)}
          </button>
          ${threads}
        </div>
      `;
    })
    .join("");
}

function renderMessages(messages) {
  $("#messages").innerHTML = messages
    .map(
      (message) =>
        `<article class="message ${escapeHtml(message.role)}">${escapeHtml(message.content)}</article>`,
    )
    .join("");
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function appendMessage(role, content, id = "") {
  const message = document.createElement("article");
  message.className = `message ${role}`;
  message.textContent = content;
  if (id) message.dataset.messageId = id;
  $("#messages").append(message);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return message;
}

function setChatVisible(visible) {
  $("#welcome").classList.toggle("hidden", visible);
  $("#chat").classList.toggle("hidden", !visible);
}

async function loadSessions() {
  state.sessions = await request("/sessions");
  renderNavigation();
}

async function selectSession(sessionId) {
  state.sessionId = sessionId;
  state.threadId = null;
  state.threads = await request(`/sessions/${sessionId}/threads`);
  $("#session-name").textContent = currentSession()?.name || "Session";
  $("#thread-name").textContent = "Choose a thread";
  $("#new-thread").disabled = false;
  setChatVisible(false);
  renderNavigation();
}

async function selectThread(threadId) {
  state.threadId = threadId;
  const thread = await request(
    `/sessions/${state.sessionId}/threads/${threadId}`,
  );
  $("#thread-name").textContent = thread.title || "Untitled thread";
  renderMessages(thread.messages || []);
  setChatVisible(true);
  renderNavigation();
}

function openCreator(kind) {
  const thread = kind === "thread";
  const dialog = $("#create-dialog");
  dialog.dataset.kind = kind;
  $("#create-kind").textContent = thread ? currentSession()?.name : "Workspace";
  $("#create-title").textContent = thread ? "Create thread" : "Create session";
  $("#name-field").classList.toggle("hidden", thread);
  $("#create-name").required = !thread;
  $("#create-name").value = thread ? "" : "New session";
  $("#create-workspace").value = "";
  $("#workspace-field").classList.toggle("hidden", !thread);
  dialog.showModal();
  const input = thread ? $("#create-workspace") : $("#create-name");
  input.focus();
  input.select();
}

function parseSseFrame(frame) {
  const data = frame
    .split("\n")
    .find((line) => line.startsWith("data:"));
  if (!data) return null;
  return JSON.parse(data.slice(5).trim());
}

async function streamTurn(prompt) {
  state.streaming = true;
  $("#prompt").disabled = true;
  $(".send").disabled = true;
  $("#status").textContent = "Codex is working in the sandbox…";
  appendMessage("user", prompt);

  let activeMessage = null;
  const response = await fetch(
    `${API}/sessions/${state.sessionId}/threads/${state.threadId}/turns`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt }),
    },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
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
      const event = parseSseFrame(frame);
      if (!event) continue;
      if (event.type === "TEXT_MESSAGE_START") {
        activeMessage = appendMessage("assistant", "", event.messageId);
      } else if (event.type === "TEXT_MESSAGE_CONTENT") {
        activeMessage ||= appendMessage("assistant", "", event.messageId);
        activeMessage.textContent += event.delta || "";
        $("#messages").scrollTop = $("#messages").scrollHeight;
      } else if (event.type === "RUN_ERROR") {
        throw new Error(event.message || "Codex run failed");
      }
    }

    if (done) break;
  }
}

$("#sessions").addEventListener("click", (event) => {
  const thread = event.target.closest("[data-thread]");
  const session = event.target.closest("[data-session]");
  if (thread) selectThread(thread.dataset.thread);
  else if (session) selectSession(session.dataset.session);
});

$("#new-session").addEventListener("click", () => openCreator("session"));
$("#new-thread").addEventListener("click", () => openCreator("thread"));
$("#cancel-create").addEventListener("click", () => $("#create-dialog").close());

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const dialog = $("#create-dialog");
  const name = $("#create-name").value.trim();

  if (dialog.dataset.kind === "session") {
    if (!name) return;
    const session = await request("/sessions", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    dialog.close();
    await loadSessions();
    await selectSession(session.id);
    return;
  }

  const workspace = $("#create-workspace").value.trim();
  const thread = await request(`/sessions/${state.sessionId}/threads`, {
    method: "POST",
    body: JSON.stringify({
      ...(workspace ? { workspace } : {}),
    }),
  });
  dialog.close();
  state.threads.unshift(thread);
  await selectThread(thread.id);
});

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.streaming) return;
  const prompt = $("#prompt").value.trim();
  if (!prompt) return;
  $("#prompt").value = "";

  try {
    await streamTurn(prompt);
    const thread = await request(
      `/sessions/${state.sessionId}/threads/${state.threadId}`,
    );
    renderMessages(thread.messages || []);
    $("#status").textContent = "";
  } catch (error) {
    $("#status").textContent = error.message;
  } finally {
    state.streaming = false;
    $("#prompt").disabled = false;
    $(".send").disabled = false;
    $("#prompt").focus();
  }
});

await loadSessions();
