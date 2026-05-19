// Dispatch sender UI.

const STORAGE_TOKEN = "dispatch_token";
const STORAGE_USER = "dispatch_user_id";

let token = localStorage.getItem(STORAGE_TOKEN);
let userId = localStorage.getItem(STORAGE_USER);

const authStatus = document.getElementById("auth-status");
const tokenDetails = document.getElementById("token-details");
const tokenDisplay = document.getElementById("token-display");
const logoutBtn = document.getElementById("logout");
const composeSection = document.getElementById("compose-section");
const dispatchesSection = document.getElementById("dispatches-section");
const dispatchesEl = document.getElementById("dispatches");
const composeStatus = document.getElementById("compose-status");

function refreshAuth() {
  if (token && userId) {
    authStatus.textContent = `signed in as ${userId}`;
    authStatus.className = "status done";
    composeSection.hidden = false;
    dispatchesSection.hidden = false;
    tokenDetails.hidden = false;
    tokenDisplay.textContent = token;
    logoutBtn.hidden = false;
  } else {
    authStatus.textContent = "not signed in";
    authStatus.className = "status idle";
    composeSection.hidden = true;
    dispatchesSection.hidden = true;
    tokenDetails.hidden = true;
    tokenDisplay.textContent = "";
    logoutBtn.hidden = true;
  }
}
refreshAuth();

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("username").value.trim();
  if (!username) return;
  authStatus.className = "status running";
  authStatus.textContent = "signing in…";
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    token = body.token;
    userId = body.user_id;
    localStorage.setItem(STORAGE_TOKEN, token);
    localStorage.setItem(STORAGE_USER, userId);
    refreshAuth();
  } catch (err) {
    authStatus.className = "status error";
    authStatus.textContent = `sign-in failed: ${err.message}`;
  }
});

logoutBtn.addEventListener("click", () => {
  token = null;
  userId = null;
  localStorage.removeItem(STORAGE_TOKEN);
  localStorage.removeItem(STORAGE_USER);
  refreshAuth();
});

document.getElementById("copy-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(token || "");
    composeStatus.textContent = "token copied";
    composeStatus.className = "status done";
  } catch (_) {
    /* ignore */
  }
});

document.getElementById("compose-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const recipient = document.getElementById("recipient").value.trim();
  const task = document.getElementById("task").value.trim();
  if (!recipient || !task) return;
  composeStatus.className = "status running";
  composeStatus.textContent = "sending…";
  try {
    const res = await fetch("/dispatch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ recipient_id: recipient, task }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    composeStatus.className = "status done";
    composeStatus.textContent = `sent: ${body.dispatch_id.slice(0, 8)}… (${body.status})`;
    document.getElementById("task").value = "";
    watchDispatch(body.dispatch_id, recipient, task);
  } catch (err) {
    composeStatus.className = "status error";
    composeStatus.textContent = `send failed: ${err.message}`;
  }
});

function watchDispatch(dispatchId, recipient, task) {
  const card = document.createElement("div");
  card.className = "dispatch-card";
  card.innerHTML = `
    <header>
      <strong>→ ${escapeHtml(recipient)}</strong>
      <code class="dispatch-id">${dispatchId.slice(0, 8)}…</code>
      <span class="status idle" data-status>pending</span>
    </header>
    <div class="task-text">${escapeHtml(task)}</div>
    <div class="events"></div>
  `;
  dispatchesEl.prepend(card);
  const statusEl = card.querySelector("[data-status]");
  const eventsEl = card.querySelector(".events");

  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(
    `${wsProto}//${location.host}/dispatch/${dispatchId}/watch?token=${encodeURIComponent(token)}`
  );

  ws.addEventListener("message", (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    if (msg.type === "dispatch_status") {
      setStatus(statusEl, msg.data.status);
      return;
    }
    renderEvent(eventsEl, msg.type, msg.data || {});
    if (msg.type === "done") setStatus(statusEl, "completed");
    if (msg.type === "error") setStatus(statusEl, "failed");
  });

  ws.addEventListener("error", () => {
    renderEvent(eventsEl, "error", { exception: "WebSocketError", message: "connection error" });
    setStatus(statusEl, "failed");
  });
}

function setStatus(el, status) {
  el.textContent = status;
  const map = {
    pending: "idle",
    delivered: "running",
    accepted: "running",
    running: "running",
    completed: "done",
    denied: "error",
    failed: "error",
    expired: "error",
  };
  el.className = `status ${map[status] || "idle"}`;
}

function labelFor(type) {
  return {
    agent_text: "agent",
    tool_use: "tool call",
    tool_result: "tool result",
    permission_request: "consent requested",
    permission_response: "consent decided",
    dispatch_status: "status",
    done: "done",
    error: "error",
  }[type] || type;
}

function renderEvent(parent, type, data) {
  const el = document.createElement("div");
  el.className = `event event-${type}`;
  const tag = document.createElement("div");
  tag.className = "tag";
  tag.textContent = labelFor(type);
  el.appendChild(tag);
  el.appendChild(renderBody(type, data));
  parent.prepend(el);
  return el;
}

function renderBody(type, data) {
  const body = document.createElement("div");
  body.className = "body";
  if (type === "agent_text") {
    body.textContent = data.text || "";
    return body;
  }
  if (type === "tool_use") {
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = data.name || "";
    body.appendChild(name);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data.input ?? {}, null, 2);
    body.appendChild(pre);
    return body;
  }
  if (type === "tool_result") {
    const pre = document.createElement("pre");
    pre.textContent = data.content || "(empty)";
    body.appendChild(pre);
    if (data.truncated) {
      const n = document.createElement("span");
      n.className = "truncated";
      n.textContent = "[truncated]";
      body.appendChild(n);
    }
    return body;
  }
  if (type === "permission_request") {
    const line = document.createElement("div");
    line.innerHTML = `Awaiting recipient's decision on <strong>${escapeHtml(data.tool || "")}</strong>`;
    body.appendChild(line);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data.input ?? {}, null, 2);
    body.appendChild(pre);
    return body;
  }
  if (type === "permission_response") {
    body.textContent = `${data.tool || "tool"}: ${data.decision || "?"}`;
    return body;
  }
  if (type === "done") {
    const parts = [
      data.subtype && `subtype: ${data.subtype}`,
      typeof data.duration_ms === "number" && `${data.duration_ms} ms`,
      typeof data.num_turns === "number" && `${data.num_turns} turns`,
      typeof data.total_cost_usd === "number" && `$${data.total_cost_usd.toFixed(4)}`,
    ].filter(Boolean);
    body.textContent = parts.join(" · ") || "(no result metadata)";
    return body;
  }
  if (type === "error") {
    body.textContent = `${data.exception || "Error"}: ${data.message || ""}`;
    return body;
  }
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(data, null, 2);
  body.appendChild(pre);
  return body;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]
  );
}
