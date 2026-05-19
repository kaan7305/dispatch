// Dispatch recipient UI (served by the local daemon).

const inboxEl = document.getElementById("inbox");
const dispatchCards = new Map();   // dispatch_id -> {card, eventsEl, statusEl, watchWs}
const inboxWs = new WebSocket(`ws://${location.host}/ws/inbox`);

inboxWs.addEventListener("message", (e) => {
  let msg;
  try { msg = JSON.parse(e.data); } catch { return; }
  if (msg.type === "inbox_update") upsertCard(msg.data);
});
inboxWs.addEventListener("error", () => {
  const banner = document.createElement("div");
  banner.className = "event event-error";
  banner.textContent = "lost connection to daemon";
  inboxEl.prepend(banner);
});

function upsertCard(d) {
  let entry = dispatchCards.get(d.dispatch_id);
  if (!entry) {
    const card = document.createElement("div");
    card.className = "dispatch-card";
    card.dataset.id = d.dispatch_id;
    card.innerHTML = `
      <header>
        <strong>From ${escapeHtml(d.sender_id)}</strong>
        <code class="dispatch-id">${d.dispatch_id.slice(0, 8)}…</code>
        <span class="status idle" data-status>${escapeHtml(d.status)}</span>
      </header>
      <div class="task-text">${escapeHtml(d.task)}</div>
      <div class="meta">created ${formatTime(d.created_at)} · expires ${formatTime(d.expires_at)}</div>
      <div class="dispatch-actions" data-decision-actions>
        <button class="btn-allow" data-decision="accept">Accept</button>
        <button class="btn-deny" data-decision="reject">Reject</button>
      </div>
      <div class="events"></div>
    `;
    const statusEl = card.querySelector("[data-status]");
    const eventsEl = card.querySelector(".events");
    const actionsWrap = card.querySelector("[data-decision-actions]");

    card.querySelectorAll("[data-decision]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const decision = btn.dataset.decision;
        inboxWs.send(JSON.stringify({
          type: "dispatch_decision",
          dispatch_id: d.dispatch_id,
          decision,
        }));
        actionsWrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
        if (decision === "accept") openExecutionStream(d.dispatch_id, eventsEl);
      });
    });

    inboxEl.prepend(card);
    entry = { card, statusEl, eventsEl, actionsWrap, watchWs: null };
    dispatchCards.set(d.dispatch_id, entry);
  }
  setStatus(entry.statusEl, d.status);

  // Hide accept/reject buttons once the dispatch has been decided.
  if (["accepted", "running", "completed", "denied", "failed", "expired"].includes(d.status)) {
    entry.actionsWrap.hidden = true;
  }
  // If the dispatch is already accepted/running but we don't have a WS, open one.
  if (
    ["accepted", "running", "completed"].includes(d.status) &&
    entry.watchWs === null
  ) {
    openExecutionStream(d.dispatch_id, entry.eventsEl);
  }
}

function openExecutionStream(dispatchId, eventsEl) {
  const entry = dispatchCards.get(dispatchId);
  if (!entry || entry.watchWs) return;
  const ws = new WebSocket(`ws://${location.host}/ws/dispatch/${dispatchId}`);
  entry.watchWs = ws;
  ws.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "dispatch_status") {
      setStatus(entry.statusEl, msg.data.status);
      return;
    }
    const eventEl = renderEvent(eventsEl, msg.type, msg.data || {});
    if (msg.type === "permission_request") {
      attachPermissionButtons(ws, msg.data, eventEl);
    }
  });
}

function attachPermissionButtons(ws, data, eventEl) {
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
    ws.send(JSON.stringify({
      type: "permission_response",
      id: data.id,
      decision,
    }));
  };
  allow.addEventListener("click", () => send("allow"));
  deny.addEventListener("click", () => send("deny"));
  actions.appendChild(allow);
  actions.appendChild(deny);
  actions.appendChild(decided);
  eventEl.querySelector(".body").appendChild(actions);
}

function setStatus(el, status) {
  el.textContent = status;
  const map = {
    pending: "idle",
    delivered: "idle",
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
