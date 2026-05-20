// Dispatch — unified web app.
// One UI, one WebSocket per role:
//   /inbox?token=JWT             — live inbox + consent for things sent to me
//   /dispatch/{id}/watch?token=  — live watch for things I sent (one per dispatch)

const STORAGE_TOKEN = "dispatch_token";
const STORAGE_USER = "dispatch_user_id";

let token = localStorage.getItem(STORAGE_TOKEN);
let userId = localStorage.getItem(STORAGE_USER);

const authStatus = document.getElementById("auth-status");
const tokenDetails = document.getElementById("token-details");
const tokenDisplay = document.getElementById("token-display");
const logoutBtn = document.getElementById("logout");
const loginBtn = document.getElementById("login-btn");
const linkSent = document.getElementById("link-sent");
const linkSentMessage = document.getElementById("link-sent-message");
const devLink = document.getElementById("dev-link");
const loggedInView = document.getElementById("logged-in-view");
const composeStatus = document.getElementById("compose-status");
const inboxList = document.getElementById("inbox-list");
const outboxList = document.getElementById("outbox-list");
const daemonHint = document.getElementById("daemon-hint");

let inboxWs = null;
const inboxCards = new Map();   // dispatch_id → card element wrapper
const outboxCards = new Map();  // dispatch_id → card element wrapper

// --- bootstrap: did we just come back from /auth/magic? ---
(function consumeLoginRedirect() {
  const params = new URLSearchParams(location.search);
  const incomingToken = params.get("login_token");
  const incomingUser = params.get("user_id");
  const error = params.get("auth_error");

  if (incomingToken && incomingUser) {
    token = incomingToken;
    userId = incomingUser;
    localStorage.setItem(STORAGE_TOKEN, token);
    localStorage.setItem(STORAGE_USER, userId);
  }
  if (incomingToken || incomingUser || error) {
    history.replaceState({}, "", location.pathname);
  }
  if (error === "invalid_or_expired") {
    authStatus.className = "status error";
    authStatus.textContent = "that link was invalid or expired — try again";
  }
})();

function refreshAuth() {
  if (token && userId) {
    authStatus.textContent = `signed in as ${userId}`;
    authStatus.className = "status done";
    loggedInView.hidden = false;
    tokenDetails.hidden = false;
    tokenDisplay.textContent = token;
    logoutBtn.hidden = false;
    loginBtn.hidden = true;
    document.getElementById("email").disabled = true;
    linkSent.hidden = true;
    openInboxStream();
  } else {
    if (!authStatus.classList.contains("error")) {
      authStatus.textContent = "not signed in";
      authStatus.className = "status idle";
    }
    loggedInView.hidden = true;
    tokenDetails.hidden = true;
    tokenDisplay.textContent = "";
    logoutBtn.hidden = true;
    loginBtn.hidden = false;
    document.getElementById("email").disabled = false;
    closeInboxStream();
  }
}
refreshAuth();

// ---------------- auth flow ----------------

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = document.getElementById("email").value.trim();
  if (!email) return;
  authStatus.className = "status running";
  authStatus.textContent = "sending link…";
  linkSent.hidden = true;
  devLink.hidden = true;
  try {
    const res = await fetch("/auth/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    authStatus.className = "status done";
    authStatus.textContent = "link sent";
    if (body.delivered) {
      linkSentMessage.textContent = `Check ${body.email} for a sign-in link. It expires in 15 minutes.`;
    } else {
      linkSentMessage.textContent = "Dev mode: no email sent. Click the link below to sign in.";
      devLink.href = body.dev_link;
      devLink.hidden = false;
    }
    linkSent.hidden = false;
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
  inboxCards.clear();
  outboxCards.clear();
  inboxList.innerHTML = "";
  outboxList.innerHTML = "";
  refreshAuth();
});

document.getElementById("copy-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(token || "");
    composeStatus.textContent = "token copied";
    composeStatus.className = "status done";
  } catch (_) {}
});

// ---------------- compose ----------------

document.getElementById("compose-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const recipient = document.getElementById("recipient").value.trim().toLowerCase();
  const task = document.getElementById("task").value.trim();
  if (!recipient || !task) return;
  composeStatus.className = "status running";
  composeStatus.textContent = "sending…";
  try {
    const res = await fetch("/dispatch", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ recipient_id: recipient, task }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    composeStatus.className = "status done";
    composeStatus.textContent = `sent: ${body.dispatch_id.slice(0, 8)}… (${body.status})`;
    document.getElementById("task").value = "";
    addOutboxCard(body.dispatch_id, recipient, task, body.status);
    openWatchStream(body.dispatch_id);
  } catch (err) {
    composeStatus.className = "status error";
    composeStatus.textContent = `send failed: ${err.message}`;
  }
});

// ---------------- inbox stream ----------------

function openInboxStream() {
  if (inboxWs && inboxWs.readyState === WebSocket.OPEN) return;
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  inboxWs = new WebSocket(`${wsProto}//${location.host}/inbox?token=${encodeURIComponent(token)}`);
  inboxWs.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handleInboxFrame(msg);
  });
  inboxWs.addEventListener("close", () => {
    daemonHint.textContent = "inbox connection lost — refresh to reconnect";
  });
  inboxWs.addEventListener("error", () => {
    daemonHint.textContent = "inbox connection error";
  });
}

function closeInboxStream() {
  if (inboxWs) {
    try { inboxWs.close(); } catch (_) {}
    inboxWs = null;
  }
}

function handleInboxFrame(msg) {
  if (msg.type === "inbox_new") {
    upsertInboxCard(msg.data);
    return;
  }
  // All other messages are tagged with dispatch_id.
  const did = msg.dispatch_id;
  if (!did) return;
  const card = inboxCards.get(did);
  if (!card) return;
  if (msg.type === "dispatch_status") {
    setStatus(card.statusEl, msg.data.status);
    refreshActions(card, msg.data.status);
    return;
  }
  const eventEl = renderEvent(card.eventsEl, msg.type, msg.data || {});
  if (msg.type === "permission_request") {
    attachPermissionButtons(did, msg.data, eventEl);
  }
}

function upsertInboxCard(d) {
  let card = inboxCards.get(d.dispatch_id);
  if (!card) {
    const el = document.createElement("div");
    el.className = "dispatch-card";
    el.dataset.id = d.dispatch_id;
    el.innerHTML = `
      <header>
        <strong>From ${escapeHtml(d.sender_id)}</strong>
        <code class="dispatch-id">${d.dispatch_id.slice(0, 8)}…</code>
        <span class="status idle" data-status>${escapeHtml(d.status)}</span>
      </header>
      <div class="task-text">${escapeHtml(d.task)}</div>
      <div class="meta">created ${formatTime(d.created_at)} · expires ${formatTime(d.expires_at)}</div>
      <div class="dispatch-actions" data-actions hidden>
        <button class="btn-allow" data-decision="accept">Accept</button>
        <button class="btn-deny" data-decision="reject">Reject</button>
      </div>
      <div class="events"></div>
    `;
    const statusEl = el.querySelector("[data-status]");
    const eventsEl = el.querySelector(".events");
    const actionsWrap = el.querySelector("[data-actions]");
    el.querySelectorAll("[data-decision]").forEach((btn) => {
      btn.addEventListener("click", () => {
        sendInbox({
          type: "dispatch_decision",
          dispatch_id: d.dispatch_id,
          decision: btn.dataset.decision,
        });
        actionsWrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
      });
    });
    inboxList.prepend(el);
    card = { el, statusEl, eventsEl, actionsWrap };
    inboxCards.set(d.dispatch_id, card);
  }
  setStatus(card.statusEl, d.status);
  refreshActions(card, d.status);
}

function refreshActions(card, status) {
  // Only show Accept/Reject while the dispatch is still awaiting a decision.
  const decisionPending = status === "pending" || status === "delivered";
  card.actionsWrap.hidden = !decisionPending;
}

function attachPermissionButtons(dispatchId, data, eventEl) {
  const actions = document.createElement("div");
  actions.className = "permission-actions";
  const allow = document.createElement("button");
  allow.className = "btn-allow";
  allow.textContent = "Allow";
  const deny = document.createElement("button");
  deny.className = "btn-deny";
  deny.textContent = "Deny";
  const decided = document.createElement("span");
  decided.className = "permission-decided";

  const send = (decision) => {
    if (allow.disabled) return;
    allow.disabled = true;
    deny.disabled = true;
    decided.textContent = decision === "allow" ? "you allowed" : "you denied";
    eventEl.classList.add(decision === "allow" ? "allowed" : "denied");
    sendInbox({
      type: "tool_consent",
      dispatch_id: dispatchId,
      request_id: data.id,
      decision,
    });
  };
  allow.addEventListener("click", () => send("allow"));
  deny.addEventListener("click", () => send("deny"));
  actions.appendChild(allow);
  actions.appendChild(deny);
  actions.appendChild(decided);
  eventEl.querySelector(".body").appendChild(actions);
}

function sendInbox(msg) {
  if (!inboxWs || inboxWs.readyState !== WebSocket.OPEN) return;
  inboxWs.send(JSON.stringify(msg));
}

// ---------------- outbox (per-dispatch watch) ----------------

function addOutboxCard(dispatchId, recipient, task, status) {
  if (outboxCards.has(dispatchId)) return;
  const el = document.createElement("div");
  el.className = "dispatch-card";
  el.innerHTML = `
    <header>
      <strong>→ ${escapeHtml(recipient)}</strong>
      <code class="dispatch-id">${dispatchId.slice(0, 8)}…</code>
      <span class="status idle" data-status>${escapeHtml(status)}</span>
    </header>
    <div class="task-text">${escapeHtml(task)}</div>
    <div class="events"></div>
  `;
  const statusEl = el.querySelector("[data-status]");
  const eventsEl = el.querySelector(".events");
  setStatus(statusEl, status);
  outboxList.prepend(el);
  outboxCards.set(dispatchId, { el, statusEl, eventsEl });
}

function openWatchStream(dispatchId) {
  const card = outboxCards.get(dispatchId);
  if (!card) return;
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(
    `${wsProto}//${location.host}/dispatch/${dispatchId}/watch?token=${encodeURIComponent(token)}`
  );
  ws.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "dispatch_status") {
      setStatus(card.statusEl, msg.data.status);
      return;
    }
    renderEvent(card.eventsEl, msg.type, msg.data || {});
  });
  ws.addEventListener("error", () => {
    renderEvent(card.eventsEl, "error", { exception: "WebSocketError", message: "connection error" });
    setStatus(card.statusEl, "failed");
  });
}

// ---------------- shared rendering ----------------

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
    permission_request: "consent required",
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
  if (type === "agent_text") { body.textContent = data.text || ""; return body; }
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
    const summary = document.createElement("div");
    summary.className = "permission-summary";
    summary.innerHTML = `Agent wants to run <strong>${escapeHtml(data.tool || "")}</strong>`;
    body.appendChild(summary);
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

function formatTime(iso) {
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]
  );
}
