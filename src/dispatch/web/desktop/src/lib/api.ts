import { getToken } from "./token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const t = getToken();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers });
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
  // ── Local-only (daemon hosts the state) ─────────────────────────────
  session: () => request<{ user_id: string; broker_url: string }>("/api/session"),
  inbox: () => request<InboxEntry[]>("/api/inbox"),
  dispatchDetail: (id: string) =>
    request<InboxEntry & { events: DispatchEvent[] }>(`/api/dispatch/${id}`),
  decide: (id: string, decision: "accept" | "reject") =>
    request<{ status: string }>(`/api/dispatch/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  decideTool: (dispatchId: string, requestId: string, decision: "allow" | "deny") =>
    request<{ status: string }>(
      `/api/dispatch/${dispatchId}/tool/${requestId}/decision`,
      { method: "POST", body: JSON.stringify({ decision }) },
    ),
  cancelDispatch: (dispatchId: string) =>
    request<{ status: string }>(`/api/dispatch/${dispatchId}/cancel`, {
      method: "POST",
    }),

  // ── Broker proxy ────────────────────────────────────────────────────
  compose: (body: ComposeRequest) =>
    request<DispatchSummary>("/api/compose", {
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
  revokeDevice: (id: string) =>
    request<{ status: string }>(`/api/devices/${id}`, { method: "DELETE" }),
};

// ─── Broker-side wire types (proxied through the daemon) ─────────────────

export interface ComposeRequest {
  recipient_id: string;
  task: string;
  expires_in_seconds?: number;
  metadata?: Record<string, unknown>;
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
