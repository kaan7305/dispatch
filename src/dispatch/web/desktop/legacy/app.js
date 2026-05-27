// Recipient local UI. Bound to 127.0.0.1, plus a per-launch bearer token
// the tray app delivers via URL fragment. Without the token, a drive-by
// page hitting our origin can't drive the daemon.

const TOKEN_KEY = "dispatch_local_token";
const STORAGE_TOKEN_KEY = "dispatch_local_token_session";

function readTokenFromFragment() {
  // Bootstrap once: ?t=… in query OR #t=… in fragment.
  const params = new URLSearchParams(location.search);
  let t = params.get("t");
  if (!t && location.hash.startsWith("#t=")) t = location.hash.slice(3);
  if (t) {
    sessionStorage.setItem(STORAGE_TOKEN_KEY, t);
    history.replaceState({}, "", location.pathname);
  }
  return sessionStorage.getItem(STORAGE_TOKEN_KEY) || "";
}

const LOCAL_TOKEN = readTokenFromFragment();

function authedFetch(url, init = {}) {
  const headers = new Headers(init.headers || {});
  if (LOCAL_TOKEN) headers.set("Authorization", `Bearer ${LOCAL_TOKEN}`);
  return fetch(url, { ...init, headers });
}

const inboxList = document.getElementById("inbox-list");
const emptyEl   = document.getElementById("empty");
const signedIn  = document.getElementById("signed-in");

const cards = new Map(); // dispatch_id → element

(async function init() {
  try {
    const s = await authedFetch("/api/session").then((r) => r.json());
    if (s.user_id) signedIn.textContent = ` · signed in as ${s.user_id}`;
  } catch (_) {}
  await loadInbox();
  openEventStream();
})();

async function loadInbox() {
  const entries = await authedFetch("/api/inbox").then((r) => r.json());
  inboxList.innerHTML = "";
  cards.clear();
  for (const e of entries) renderEntry(e);
  syncEmpty();
}

function syncEmpty() {
  emptyEl.hidden = cards.size > 0;
}

function renderEntry(entry) {
  let card = cards.get(entry.dispatch_id);
  if (!card) {
    card = document.createElement("article");
    card.className = "dispatch-card";
    card.innerHTML = `
      <header>
        <span class="from"></span>
        <span class="status"></span>
      </header>
      <div class="task"></div>
      <div class="scopes"></div>
      <div class="actions"></div>
      <div class="tool-requests"></div>
      <div class="events" hidden></div>
    `;
    inboxList.appendChild(card);
    cards.set(entry.dispatch_id, card);
  }
  card.querySelector(".from").textContent = entry.sender_id;
  card.querySelector(".status").textContent = entry.status;
  card.querySelector(".task").textContent = entry.task;
  const scopes = entry.scopes || {};
  const tools = (scopes.tools || []).join(", ") || "none";
  card.querySelector(".scopes").textContent =
    `scope tools: ${tools} · approval: ${scopes.approval || "manual"}`;
  renderActions(card, entry);
  renderToolRequests(card, entry);
  syncEmpty();
}

function renderActions(card, entry) {
  const wrap = card.querySelector(".actions");
  wrap.innerHTML = "";
  if (entry.status !== "delivered") return; // already decided / running
  const accept = document.createElement("button");
  accept.className = "allow";
  accept.textContent = "Accept";
  accept.onclick = () => decide(entry.dispatch_id, "accept", accept, reject);
  const reject = document.createElement("button");
  reject.className = "deny";
  reject.textContent = "Reject";
  reject.onclick = () => decide(entry.dispatch_id, "reject", accept, reject);
  wrap.append(accept, reject);
}

async function decide(id, decision, ...buttons) {
  for (const b of buttons) b.disabled = true;
  try {
    const res = await authedFetch(`/api/dispatch/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
  } catch (err) {
    for (const b of buttons) b.disabled = false;
    alert(`Could not record decision: ${err.message}`);
  }
}

function renderToolRequests(card, entry) {
  const wrap = card.querySelector(".tool-requests");
  wrap.innerHTML = "";
  for (const [requestId, t] of Object.entries(entry.pending_tools || {})) {
    const el = document.createElement("div");
    el.className = "tool-request";
    el.innerHTML = `
      <div class="name"></div>
      <pre></pre>
      <div class="actions"></div>
    `;
    el.querySelector(".name").textContent = `${t.tool} wants permission`;
    el.querySelector("pre").textContent = JSON.stringify(t.input, null, 2);
    const allow = document.createElement("button");
    allow.className = "allow";
    allow.textContent = "Allow";
    allow.onclick = () => decideTool(entry.dispatch_id, requestId, "allow", allow, deny);
    const deny = document.createElement("button");
    deny.className = "deny";
    deny.textContent = "Deny";
    deny.onclick = () => decideTool(entry.dispatch_id, requestId, "deny", allow, deny);
    el.querySelector(".actions").append(allow, deny);
    wrap.appendChild(el);
  }
}

async function decideTool(dispatchId, requestId, decision, ...buttons) {
  for (const b of buttons) b.disabled = true;
  try {
    const res = await authedFetch(
      `/api/dispatch/${dispatchId}/tool/${requestId}/decision`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
  } catch (err) {
    for (const b of buttons) b.disabled = false;
    alert(`Could not record decision: ${err.message}`);
  }
}

function openEventStream() {
  const wsUrl =
    `${location.origin.replace(/^http/, "ws")}/ws/events` +
    (LOCAL_TOKEN ? `?t=${encodeURIComponent(LOCAL_TOKEN)}` : "");
  const ws = new WebSocket(wsUrl);
  ws.addEventListener("message", (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch (_) { return; }
    if (data.type === "snapshot") {
      cards.clear();
      inboxList.innerHTML = "";
      for (const e of data.data) renderEntry(e);
    } else if (data.type === "inbox_new") {
      renderEntry(data.data);
    } else if (data.type === "dispatch_status") {
      const card = cards.get(data.dispatch_id);
      if (card) {
        card.querySelector(".status").textContent = data.data.status;
        // If status changed, re-render actions (hides Accept/Reject after delivered).
        const entry = currentEntry(data.dispatch_id);
        if (entry) {
          entry.status = data.data.status;
          renderActions(card, entry);
        }
      }
    } else if (data.type === "dispatch_event") {
      appendEvent(data.dispatch_id, data.data);
    }
  });
  ws.addEventListener("close", () => setTimeout(openEventStream, 1500));
}

function currentEntry(dispatchId) {
  // The SPA keeps display state on the card itself; refetch the entry to
  // get the freshest server-side view.
  return _entriesCache.get(dispatchId);
}

const _entriesCache = new Map();

function appendEvent(dispatchId, event) {
  const card = cards.get(dispatchId);
  if (!card) return;
  const wrap = card.querySelector(".events");
  wrap.hidden = false;
  const el = document.createElement("div");
  el.className = "event " + (event.type === "tool_use" ? "tool"
                          : event.type === "tool_result" ? "result"
                          : event.type === "error" ? "error" : "");
  const tag = document.createElement("strong");
  tag.textContent = event.type;
  el.appendChild(tag);
  const body = document.createElement("pre");
  body.textContent = JSON.stringify(event.data, null, 2);
  el.appendChild(body);
  wrap.appendChild(el);

  // If a permission_request comes through, re-pull inbox to refresh
  // pending_tools — simpler than threading the request into the DOM.
  if (event.type === "permission_request" || event.type === "permission_response") {
    loadInbox();
  }
}
