import { getToken } from "./token";
import { isBroker, openLocalApp } from "./config";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// In broker mode the SPA talks straight to the broker's native endpoints,
// which return the same shapes the daemon's /api/* proxies pass through. The
// daemon prefixes those with /api; the broker doesn't — so drop the prefix.
// (session + inbox have no 1:1 broker route and are handled explicitly below.)
function resolvePath(path: string): string {
  if (isBroker && path.startsWith("/api/")) return path.slice(4);
  return path;
}

/** Compose + approve must happen on the trusted local surface. On the broker
 *  site we surface them but defer: open the local app and fail loudly. */
function redirectToLocal(action: string): never {
  openLocalApp();
  throw new ApiError(409, `${action} happens in the local Dispatch app — opening it now.`);
}

/** Adapt a broker dispatch summary into the InboxEntry shape the UI expects.
 *  The broker doesn't track per-entry scopes / live pending tool calls (those
 *  live on the recipient's daemon), so they come back empty in broker mode. */
function summaryToInboxEntry(s: DispatchSummary): InboxEntry {
  return {
    dispatch_id: s.dispatch_id,
    sender_id: s.sender_id,
    task: s.task,
    created_at: s.created_at,
    expires_at: s.expires_at ?? "",
    status: s.status,
    scopes: {},
    pending_tools: {},
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const t = getToken();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(resolvePath(path), { ...init, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as unknown));
    throw new ApiError(res.status, formatBrokerError(body, res.status));
  }
  // 204 No Content guard.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function formatBrokerError(body: unknown, status: number): string {
  if (typeof body !== "object" || body === null) return `HTTP ${status}`;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{ type, loc, msg, ... }, ...]
    return detail
      .map((e: { msg?: string; loc?: unknown[] }) => {
        const loc = Array.isArray(e.loc) ? e.loc.join(".") : "";
        return loc ? `${loc}: ${e.msg ?? JSON.stringify(e)}` : e.msg ?? JSON.stringify(e);
      })
      .join("; ");
  }
  if (detail) return JSON.stringify(detail);
  return `HTTP ${status}`;
}

export const api = {
  // ── Session ─────────────────────────────────────────────────────────
  // Local: daemon hosts identity. Broker: derive from /me.
  session: () =>
    isBroker
      ? request<{ user_id: string }>("/me").then((r) => ({
          user_id: r.user_id,
          broker_url: location.origin,
        }))
      : request<{ user_id: string; broker_url: string }>("/api/session"),
  installCommand: () =>
    request<{ command: string; broker: string }>("/api/install-command"),
  openBroker: () =>
    request<{ status: string; url: string }>("/api/open-broker", {
      method: "POST",
    }),
  signOut: () =>
    request<{ status: string; broker: string }>("/api/sign-out", {
      method: "POST",
    }),
  // Inbox = received dispatches. Local mode has a richer per-entry view
  // (scopes + live pending tool calls); the broker mirror lists received
  // dispatches read-only (approvals happen in the local app).
  inbox: () =>
    isBroker
      ? request<{ role: string; dispatches: DispatchSummary[] }>(
          "/dispatches?role=received",
        ).then((b) => b.dispatches.map(summaryToInboxEntry))
      : request<InboxEntry[]>("/api/inbox"),
  dispatchDetail: (id: string) =>
    request<InboxEntry & { events: DispatchEvent[] }>(`/api/dispatch/${id}`),
  decide: (id: string, decision: "accept" | "reject") =>
    isBroker
      ? redirectToLocal("Approving a dispatch")
      : request<{ status: string }>(`/api/dispatch/${id}/decision`, {
          method: "POST",
          body: JSON.stringify({ decision }),
        }),
  decideTool: (dispatchId: string, requestId: string, decision: "allow" | "deny") =>
    isBroker
      ? redirectToLocal("Approving a tool call")
      : request<{ status: string }>(
          `/api/dispatch/${dispatchId}/tool/${requestId}/decision`,
          { method: "POST", body: JSON.stringify({ decision }) },
        ),
  cancelDispatch: (dispatchId: string) =>
    request<{ status: string }>(`/api/dispatch/${dispatchId}/cancel`, {
      method: "POST",
    }),

  // ── Broker proxy ────────────────────────────────────────────────────
  compose: (body: ComposeRequest) =>
    isBroker
      ? redirectToLocal("Composing a dispatch")
      : request<DispatchSummary | ComposeFanOutResult>("/api/compose", {
          method: "POST",
          body: JSON.stringify(body),
        }),
  trust: () => request<{ trust: TrustEdge[] }>("/api/trust"),
  updateTrust: (id: string, scopes: Scopes) =>
    request<{ status: string }>(`/api/trust/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ scopes }),
    }),
  revokeTrust: (id: string) =>
    request<{ status: string }>(`/api/trust/${id}`, { method: "DELETE" }),
  invite: (to_email: string) =>
    request<InviteResult>("/api/invitations", {
      method: "POST",
      body: JSON.stringify({ to_email }),
    }),
  invitations: () =>
    request<{ sent: Invitation[]; received: Invitation[] }>("/api/invitations"),
  invitation: (token: string) =>
    request<InvitationDetail>(`/api/invitations/${token}`),
  acceptInvite: (token: string, scopes?: Scopes) =>
    request<{ status: string; trust_link_id: string }>(
      `/api/invitations/${token}/accept`,
      { method: "POST", body: JSON.stringify({ scopes: scopes ?? null }) },
    ),
  declineInvite: (token: string) =>
    request<{ status: string }>(`/api/invitations/${token}/decline`, {
      method: "POST",
    }),
  dispatches: async (role: "sent" | "received" = "received") => {
    const body = await request<{ role: string; dispatches: DispatchSummary[] }>(
      `/api/dispatches?role=${role}`,
    );
    return body.dispatches;
  },
  devices: () => request<{ devices: Device[] }>("/api/devices"),
  renameDevice: (id: string, label: string) =>
    request<{ status: string }>(`/api/devices/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ label }),
    }),
  revokeDevice: (id: string) =>
    request<{ status: string }>(`/api/devices/${id}`, { method: "DELETE" }),

  // ── SMS notifications ───────────────────────────────────────────────
  // The recipient's phone for dispatch-arrival texts. `sms_enabled` reflects
  // whether the broker actually has Twilio configured — a saved number with
  // sms_enabled=false means texts won't send until the broker is wired up.
  phone: () => request<PhoneSettings>("/api/me/phone"),
  setPhone: (phone: string | null) =>
    request<PhoneSettings>("/api/me/phone", {
      method: "POST",
      body: JSON.stringify({ phone }),
    }),
};

export interface PhoneSettings {
  phone: string | null;
  sms_enabled: boolean;
}

// ─── Broker-side wire types (proxied through the daemon) ─────────────────

export interface ComposeRequest {
  recipient_id?: string;
  recipient_ids?: string[];
  task: string;
  expires_in_seconds?: number;
  metadata?: Record<string, unknown>;
}

export interface ComposeFanOutResult {
  dispatches: Array<{ recipient_id: string; dispatch_id: string; status: DispatchStatus }>;
  failures: Array<{ recipient_id: string; status_code: number; error: string }>;
}

export interface DispatchSummary {
  dispatch_id: string;
  sender_id: string;
  recipient_id: string;
  task: string;
  status: DispatchStatus;
  created_at: string;
  expires_at?: string;
}

export interface TrustEdge {
  trust_link_id: string;
  from_user: string;
  to_user: string;
  direction: "outgoing" | "incoming";
  peer: string;
  peer_online: boolean;
  scopes: Scopes;
  can_edit_scopes: boolean;
}

export interface Invitation {
  invitation_id: string;
  from_user: string;
  to_email: string;
  token: string;
  status: "pending" | "accepted" | "declined" | "expired";
  created_at: string;
}

export interface InvitationDetail {
  from_user: string;
  to_email: string;
  status: Invitation["status"];
  expired: boolean;
}

export interface InviteResult {
  status: string;
  delivered: boolean;
  to_email: string;
  dev_link?: string;
}

export interface Device {
  device_id: string;
  label: string;
  status: "active" | "revoked";
  online: boolean;
  last_seen: string | null;
  created_at: string;
}

// ─── Wire types ──────────────────────────────────────────────────────────
// Hand-written for now; will be auto-generated from Pydantic in a follow-up.

export type DispatchStatus =
  | "pending"
  | "delivered"
  | "accepted"
  | "running"
  | "completed"
  | "denied"
  | "failed"
  | "expired"
  | "cancelled";

export interface Scopes {
  tools?: string[];
  paths?: string[];
  approval?: "manual" | "auto";
  max_dispatches_per_day?: number;
  expires_at?: string | null;
}

export interface InboxEntry {
  dispatch_id: string;
  sender_id: string;
  task: string;
  created_at: string;
  expires_at: string;
  status: DispatchStatus;
  scopes: Scopes;
  pending_tools: Record<string, { tool: string; input: Record<string, unknown> }>;
}

export interface DispatchEvent {
  type:
    | "agent_text"
    | "tool_use"
    | "tool_result"
    | "permission_request"
    | "permission_response"
    | "dispatch_status"
    | "done"
    | "error";
  data: Record<string, unknown>;
}
