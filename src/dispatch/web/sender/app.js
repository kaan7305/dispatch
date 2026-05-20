// Dispatch sender UI — friends-first experience.

const STORAGE_TOKEN   = "dispatch_token";
const STORAGE_USER_ID = "dispatch_user_id";

// Auto-sign in from URL params injected by the tray app.
(function () {
  const p = new URLSearchParams(location.search);
  if (p.get("token") && p.get("user_id")) {
    localStorage.setItem(STORAGE_TOKEN, p.get("token"));
    localStorage.setItem(STORAGE_USER_ID, p.get("user_id"));
  }
})();

let token  = localStorage.getItem(STORAGE_TOKEN);
let userId = localStorage.getItem(STORAGE_USER_ID);
let selectedRecipient = null;

// ── DOM refs ────────────────────────────────────────────────────────────────
const authStatus          = document.getElementById("auth-status");
const tokenDetails        = document.getElementById("token-details");
const tokenDisplay        = document.getElementById("token-display");
const logoutBtn           = document.getElementById("logout");
const friendsSection      = document.getElementById("friends-section");
const friendsList         = document.getElementById("friends-list");
const noFriends           = document.getElementById("no-friends");
const friendRequestsSec   = document.getElementById("friend-requests-section");
const friendRequestsEl    = document.getElementById("friend-requests");
const addFriendBtn        = document.getElementById("add-friend-btn");
const addFriendForm       = document.getElementById("add-friend-form");
const addFriendInput      = document.getElementById("add-friend-input");
const addFriendStatus     = document.getElementById("add-friend-status");
const cancelAddFriend     = document.getElementById("cancel-add-friend");
const composeSection      = document.getElementById("compose-section");
const composeRecipName    = document.getElementById("compose-recipient-name");
const cancelCompose       = document.getElementById("cancel-compose");
const dispatchesSection   = document.getElementById("dispatches-section");
const dispatchesEl        = document.getElementById("dispatches");
const composeStatus       = document.getElementById("compose-status");

// ── Auth ────────────────────────────────────────────────────────────────────

function refreshAuth() {
  const loggedIn = !!(token && userId);
  authStatus.textContent  = loggedIn ? `signed in as ${userId}` : "not signed in";
  authStatus.className    = loggedIn ? "status done" : "status idle";
  tokenDetails.hidden     = !loggedIn;
  tokenDisplay.textContent = token || "";
  logoutBtn.hidden        = !loggedIn;
  friendsSection.hidden   = !loggedIn;
  dispatchesSection.hidden = !loggedIn;
  if (loggedIn) loadFriends();
}
refreshAuth();

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("username").value.trim();
  if (!username) return;
  authStatus.className = "status running";
  authStatus.textContent = "signing in…";
  try {
    const res  = await apiFetch("/auth/login", { method: "POST", body: { username } });
    token  = res.token;
    userId = res.user_id;
    localStorage.setItem(STORAGE_TOKEN, token);
    localStorage.setItem(STORAGE_USER_ID, userId);
    refreshAuth();
  } catch (err) {
    authStatus.className = "status error";
    authStatus.textContent = `sign-in failed: ${err.message}`;
  }
});

logoutBtn.addEventListener("click", () => {
  token = null; userId = null;
  localStorage.removeItem(STORAGE_TOKEN);
  localStorage.removeItem(STORAGE_USER_ID);
  selectedRecipient = null;
  composeSection.hidden = true;
  refreshAuth();
});

document.getElementById("copy-token").addEventListener("click", async () => {
  await navigator.clipboard.writeText(token || "").catch(() => {});
  composeStatus.textContent = "token copied";
  composeStatus.className = "status done";
});

// ── Friends ─────────────────────────────────────────────────────────────────

async function loadFriends() {
  try {
    const [{ friends }, { requests }] = await Promise.all([
      apiFetch("/friends"),
      apiFetch("/friends/requests"),
    ]);
    renderFriends(friends);
    renderFriendRequests(requests);
  } catch (err) {
    console.error("loadFriends:", err);
  }
}

function renderFriends(friends) {
  // Remove existing friend cards (keep no-friends placeholder).
  friendsList.querySelectorAll(".friend-card").forEach((el) => el.remove());

  noFriends.hidden = friends.length > 0;

  friends.forEach((friendId) => {
    const card = document.createElement("div");
    card.className = "friend-card";
    card.innerHTML = `
      <span class="friend-name">${escapeHtml(friendId)}</span>
      <button class="send-btn" data-friend="${escapeHtml(friendId)}">Send task →</button>
    `;
    card.querySelector(".send-btn").addEventListener("click", () => openCompose(friendId));
    friendsList.appendChild(card);
  });
}

function renderFriendRequests(requests) {
  friendRequestsSec.hidden = requests.length === 0;
  friendRequestsEl.innerHTML = "";
  requests.forEach((req) => {
    const row = document.createElement("div");
    row.className = "request-row";
    row.dataset.requestId = req.request_id;
    row.innerHTML = `
      <span>${escapeHtml(req.from_user)} wants to connect</span>
      <button class="accept-btn">Accept</button>
      <button class="decline-btn">Decline</button>
    `;
    row.querySelector(".accept-btn").addEventListener("click", () => respondRequest(req.request_id, "accept", row));
    row.querySelector(".decline-btn").addEventListener("click", () => respondRequest(req.request_id, "decline", row));
    friendRequestsEl.appendChild(row);
  });
}

async function respondRequest(requestId, action, rowEl) {
  try {
    await apiFetch(`/friends/${action}/${requestId}`, { method: "POST" });
    rowEl.remove();
    if (!friendRequestsEl.children.length) friendRequestsSec.hidden = true;
    if (action === "accept") loadFriends();
  } catch (err) {
    console.error("respondRequest:", err);
  }
}

// Add friend form
addFriendBtn.addEventListener("click", () => {
  addFriendForm.hidden = false;
  addFriendBtn.hidden = true;
  addFriendInput.focus();
});

cancelAddFriend.addEventListener("click", () => {
  addFriendForm.hidden = true;
  addFriendBtn.hidden = false;
  addFriendInput.value = "";
  addFriendStatus.textContent = "";
  addFriendStatus.className = "status idle";
});

addFriendForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const toUser = addFriendInput.value.trim();
  if (!toUser) return;
  addFriendStatus.className = "status running";
  addFriendStatus.textContent = "sending request…";
  try {
    const res = await apiFetch("/friends/request", { method: "POST", body: { to_user_id: toUser } });
    addFriendStatus.className = "status done";
    addFriendStatus.textContent = res.status === "already_friends"
      ? "already friends!"
      : `request sent to ${toUser}`;
    addFriendInput.value = "";
  } catch (err) {
    addFriendStatus.className = "status error";
    addFriendStatus.textContent = err.message;
  }
});

// ── Compose ──────────────────────────────────────────────────────────────────

function openCompose(friendId) {
  selectedRecipient = friendId;
  composeRecipName.textContent = friendId;
  composeSection.hidden = false;
  document.getElementById("task").focus();
}

cancelCompose.addEventListener("click", () => {
  composeSection.hidden = true;
  selectedRecipient = null;
  document.getElementById("task").value = "";
  composeStatus.textContent = "";
  composeStatus.className = "status idle";
});

document.getElementById("compose-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedRecipient) return;
  const task = document.getElementById("task").value.trim();
  if (!task) return;
  composeStatus.className = "status running";
  composeStatus.textContent = "sending…";
  try {
    const body = await apiFetch("/dispatch", {
      method: "POST",
      body: { recipient_id: selectedRecipient, task },
    });
    composeStatus.className = "status done";
    composeStatus.textContent = `sent (${body.status})`;
    document.getElementById("task").value = "";
    dispatchesSection.hidden = false;
    watchDispatch(body.dispatch_id, selectedRecipient, task);
  } catch (err) {
    composeStatus.className = "status error";
    composeStatus.textContent = `failed: ${err.message}`;
  }
});

// ── Dispatch watch ────────────────────────────────────────────────────────────

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
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "dispatch_status") { setStatus(statusEl, msg.data.status); return; }
    renderEvent(eventsEl, msg.type, msg.data || {});
    if (msg.type === "done")  setStatus(statusEl, "completed");
    if (msg.type === "error") setStatus(statusEl, "failed");
  });

  ws.addEventListener("error", () => {
    renderEvent(eventsEl, "error", { exception: "WebSocketError", message: "connection error" });
    setStatus(statusEl, "failed");
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function apiFetch(path, { method = "GET", body } = {}) {
  const opts = {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

function setStatus(el, status) {
  el.textContent = status;
  const map = {
    pending: "idle", delivered: "running", accepted: "running",
    running: "running", completed: "done", denied: "error",
    failed: "error", expired: "error",
  };
  el.className = `status ${map[status] || "idle"}`;
}

function renderEvent(parent, type, data) {
  const el  = document.createElement("div");
  el.className = `event event-${type}`;
  const tag = document.createElement("div");
  tag.className = "tag";
  tag.textContent = {
    agent_text: "agent", tool_use: "tool call", tool_result: "tool result",
    permission_request: "consent requested", permission_response: "consent decided",
    dispatch_status: "status", done: "done", error: "error",
  }[type] || type;
  el.appendChild(tag);
  el.appendChild(renderBody(type, data));
  parent.prepend(el);
}

function renderBody(type, data) {
  const body = document.createElement("div");
  body.className = "body";
  if (type === "agent_text") { body.textContent = data.text || ""; return body; }
  if (type === "tool_use") {
    const name = document.createElement("div");
    name.className = "name"; name.textContent = data.name || "";
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
      n.className = "truncated"; n.textContent = "[truncated]";
      body.appendChild(n);
    }
    return body;
  }
  if (type === "permission_request") {
    const line = document.createElement("div");
    line.innerHTML = `Awaiting decision on <strong>${escapeHtml(data.tool || "")}</strong>`;
    body.appendChild(line);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data.input ?? {}, null, 2);
    body.appendChild(pre);
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
