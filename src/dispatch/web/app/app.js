// Dispatch — unified web app.
// One UI, one WebSocket per role:
//   /inbox?token=JWT             — live inbox + approval for things sent to me
//   /dispatch/{id}/watch?token=  — live watch for things I sent (one per dispatch)

const STORAGE_TOKEN = "dispatch_token";
const STORAGE_USER = "dispatch_user_id";
const PENDING_INVITE_KEY = "dispatch_pending_invite";

let token = localStorage.getItem(STORAGE_TOKEN);
let userId = localStorage.getItem(STORAGE_USER);

const authStatus = document.getElementById("auth-status");
const daemonSetup = document.getElementById("daemon-setup");
const installCmd = document.getElementById("install-cmd");
const tokenDisplay = document.getElementById("token-display");
const logoutBtn = document.getElementById("logout");
const loginBtn = document.getElementById("login-btn");
const loggedInView = document.getElementById("logged-in-view");
const composeStatus = document.getElementById("compose-status");
const inboxList = document.getElementById("inbox-list");
const outboxList = document.getElementById("outbox-list");
const daemonHint = document.getElementById("daemon-hint");

let inboxWs = null;
const inboxCards = new Map();   // dispatch_id → card element wrapper
const outboxCards = new Map();  // dispatch_id → card element wrapper

// Stash any /invite/{token}?invite=… param so it survives the Clerk
// sign-in round-trip and gets picked up after we're logged in.
(function consumeRedirects() {
  const params = new URLSearchParams(location.search);
  const invite = params.get("invite");
  if (invite) {
    localStorage.setItem(PENDING_INVITE_KEY, invite);
    history.replaceState({}, "", location.pathname);
  }
})();

function refreshAuth() {
  if (token && userId) {
    authStatus.textContent = `signed in as ${userId}`;
    authStatus.className = "status done";
    loggedInView.hidden = false;
    daemonSetup.hidden = false;
    tokenDisplay.textContent = token;
    installCmd.textContent = `curl -fsSL ${location.origin}/install.sh | bash -s -- ${token}`;
    logoutBtn.hidden = false;
    loginBtn.hidden = true;
    openInboxStream();
    loadContacts();
    maybeShowPendingInvite();
  } else {
    if (!authStatus.classList.contains("error")) {
      authStatus.textContent = "not signed in";
      authStatus.className = "status idle";
    }
    loggedInView.hidden = true;
    daemonSetup.hidden = true;
    tokenDisplay.textContent = "";
    installCmd.textContent = "";
    logoutBtn.hidden = true;
    loginBtn.hidden = false;
    closeInboxStream();
  }
}
refreshAuth();

// ---------------- Clerk auth flow ----------------

const CONFIG = window.DISPATCH_CONFIG || {};
const CLERK_TEMPLATE = CONFIG.clerk_jwt_template || "dispatch";

function waitForClerkScript() {
  return new Promise((resolve, reject) => {
    if (window.Clerk) return resolve();
    if (!CONFIG.clerk_publishable_key) {
      return reject(new Error(
        "Clerk is not configured — set CLERK_PUBLISHABLE_KEY and " +
        "CLERK_FRONTEND_API on the broker."
      ));
    }
    window.addEventListener("clerk-script-loaded", () => resolve(), { once: true });
  });
}

let clerkReady = null;
function ensureClerk() {
  if (!clerkReady) {
    clerkReady = (async () => {
      await waitForClerkScript();
      await window.Clerk.load();
      // Sync any in-progress Clerk session into our broker JWT.
      window.Clerk.addListener(({ user }) => {
        if (user && !token) exchangeClerkForBrokerJwt();
        if (!user && token) clearLocalSession();
      });
      if (window.Clerk.user && !token) await exchangeClerkForBrokerJwt();
    })();
  }
  return clerkReady;
}

async function exchangeClerkForBrokerJwt() {
  try {
    const clerkToken = await window.Clerk.session.getToken({ template: CLERK_TEMPLATE });
    if (!clerkToken) throw new Error("Clerk returned no session token");
    const res = await fetch("/auth/clerk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clerk_token: clerkToken }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
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
}

function clearLocalSession() {
  token = null;
  userId = null;
  localStorage.removeItem(STORAGE_TOKEN);
  localStorage.removeItem(STORAGE_USER);
  inboxCards.clear();
  outboxCards.clear();
  inboxList.innerHTML = "";
  outboxList.innerHTML = "";
  refreshAuth();
}

loginBtn.addEventListener("click", async () => {
  authStatus.className = "status running";
  authStatus.textContent = "opening sign-in…";
  try {
    await ensureClerk();
    if (window.Clerk.user) {
      await exchangeClerkForBrokerJwt();
    } else {
      window.Clerk.openSignIn({ afterSignInUrl: location.pathname });
    }
  } catch (err) {
    authStatus.className = "status error";
    authStatus.textContent = `sign-in failed: ${err.message}`;
  }
});

logoutBtn.addEventListener("click", async () => {
  try { await ensureClerk(); await window.Clerk.signOut(); } catch (_) {}
  clearLocalSession();
});

// Bootstrap Clerk on page load so a returning user (Clerk session still
// valid, broker JWT cleared) gets re-authed without clicking sign-in.
ensureClerk().catch(() => { /* surfaced when user clicks Sign in */ });

document.getElementById("copy-install").addEventListener("click", async (e) => {
  try {
    await navigator.clipboard.writeText(installCmd.textContent || "");
    e.target.textContent = "copied ✓";
    setTimeout(() => { e.target.textContent = "Copy install command"; }, 1500);
  } catch (_) {}
});

// ---------------- contacts: invitations & trust ----------------

function authHeaders() {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

document.getElementById("invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = document.getElementById("invite-email").value.trim();
  if (!email) return;
  const status = document.getElementById("invite-status");
  const devLinkEl = document.getElementById("invite-dev-link");
  status.className = "status running";
  status.textContent = "sending…";
  devLinkEl.hidden = true;
  try {
    const res = await fetch("/invitations", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ to_email: email }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const body = await res.json();
    status.className = "status done";
    if (body.delivered) {
      status.textContent = `invite emailed to ${body.to_email}`;
    } else {
      status.textContent = `dev mode — invite link for ${body.to_email}:`;
      devLinkEl.href = body.dev_link;
      devLinkEl.hidden = false;
    }
    document.getElementById("invite-email").value = "";
    loadContacts();
  } catch (err) {
    status.className = "status error";
    status.textContent = `invite failed: ${err.message}`;
  }
});

async function loadContacts() {
  if (!token) return;
  try {
    const res = await fetch("/trust", { headers: authHeaders() });
    if (res.ok) renderTrust((await res.json()).trust);
  } catch (_) {}
  try {
    const res = await fetch("/invitations", { headers: authHeaders() });
    if (res.ok) renderReceivedInvites((await res.json()).received);
  } catch (_) {}
}

function renderTrust(trust) {
  const list = document.getElementById("trust-list");
  list.innerHTML = "";
  if (!trust.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "No contacts yet. Invite someone, or accept an invitation.";
    list.appendChild(p);
    return;
  }
  for (const t of trust) {
    const el = document.createElement("div");
    el.className = "contact-card";
    const arrow = t.direction === "outgoing"
      ? "you can dispatch to them"
      : "they can dispatch to you";
    const tools = (t.scopes.tools || []).join(", ") || "none";
    const head = document.createElement("div");
    head.className = "contact-head";
    head.innerHTML =
      `<span class="dot ${t.peer_online ? "on" : "off"}"></span>` +
      `<strong>${escapeHtml(t.peer)}</strong>` +
      `<span class="contact-dir">${arrow}</span>`;
    const revoke = document.createElement("button");
    revoke.className = "btn-deny btn-sm";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", async () => {
      revoke.disabled = true;
      try {
        await fetch(`/trust/${t.trust_link_id}`, { method: "DELETE", headers: authHeaders() });
      } finally {
        loadContacts();
      }
    });
    head.appendChild(revoke);
    el.appendChild(head);
    const scopes = document.createElement("div");
    scopes.className = "contact-scopes";
    scopes.textContent = `tools: ${tools} · approval: ${t.scopes.approval || "manual"}`;
    el.appendChild(scopes);
    list.appendChild(el);
  }
}

function renderReceivedInvites(received) {
  const box = document.getElementById("received-invites");
  box.innerHTML = "";
  for (const inv of received) {
    const el = document.createElement("div");
    el.className = "invite-card";
    const text = document.createElement("span");
    text.innerHTML = `<strong>${escapeHtml(inv.from_user)}</strong> invited you to connect.`;
    const review = document.createElement("button");
    review.className = "btn-allow btn-sm";
    review.textContent = "Review";
    review.addEventListener("click", () => showInvitePanel(inv.token));
    el.appendChild(text);
    el.appendChild(review);
    box.appendChild(el);
  }
}

// --- invite acceptance panel ---

let activeInviteToken = null;

function maybeShowPendingInvite() {
  const pending = localStorage.getItem(PENDING_INVITE_KEY);
  if (pending) showInvitePanel(pending);
}

async function showInvitePanel(inviteToken) {
  activeInviteToken = inviteToken;
  const section = document.getElementById("invite-accept-section");
  const detail = document.getElementById("invite-detail");
  const scopes = document.getElementById("invite-scopes");
  const acceptBtn = document.getElementById("invite-accept-btn");
  const declineBtn = document.getElementById("invite-decline-btn");
  const status = document.getElementById("invite-accept-status");
  status.textContent = "";
  status.className = "status idle";
  try {
    const res = await fetch(`/invitations/${inviteToken}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const inv = await res.json();
    const usable = inv.status === "pending" && !inv.expired;
    if (usable) {
      detail.innerHTML =
        `<strong>${escapeHtml(inv.from_user)}</strong> wants to be able to send you ` +
        `Dispatches — agentic tasks that run on your machine. Choose what their ` +
        `dispatches may do, then accept.`;
    } else {
      detail.textContent = `This invitation is ${inv.expired ? "expired" : inv.status}.`;
    }
    scopes.hidden = !usable;
    acceptBtn.hidden = !usable;
    declineBtn.hidden = !usable;
    section.hidden = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    detail.textContent = `Could not load invitation: ${err.message}`;
    scopes.hidden = true;
    acceptBtn.hidden = true;
    declineBtn.hidden = true;
    section.hidden = false;
  }
}

function chosenScopes() {
  const tools = [
    ...document.querySelectorAll("#invite-scopes .tool-checks input:checked"),
  ].map((c) => c.value);
  const approval = document.querySelector(
    "#invite-scopes input[name=approval]:checked"
  ).value;
  return { tools, paths: [], approval, max_dispatches_per_day: 50, expires_at: null };
}

function clearInvitePanel() {
  document.getElementById("invite-accept-section").hidden = true;
  localStorage.removeItem(PENDING_INVITE_KEY);
  activeInviteToken = null;
}

document.getElementById("invite-accept-btn").addEventListener("click", async () => {
  if (!activeInviteToken) return;
  const status = document.getElementById("invite-accept-status");
  status.className = "status running";
  status.textContent = "accepting…";
  try {
    const res = await fetch(`/invitations/${activeInviteToken}/accept`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ scopes: chosenScopes() }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    clearInvitePanel();
    loadContacts();
  } catch (err) {
    status.className = "status error";
    status.textContent = `accept failed: ${err.message}`;
  }
});

document.getElementById("invite-decline-btn").addEventListener("click", async () => {
  if (!activeInviteToken) return;
  try {
    await fetch(`/invitations/${activeInviteToken}/decline`, {
      method: "POST",
      headers: authHeaders(),
    });
  } finally {
    clearInvitePanel();
    loadContacts();
  }
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
      type: "tool_approval",
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
    cancelled: "error",
  };
  el.className = `status ${map[status] || "idle"}`;
}

function labelFor(type) {
  return {
    agent_text: "agent",
    tool_use: "tool call",
    tool_result: "tool result",
    permission_request: "approval required",
    permission_response: "approval decided",
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
