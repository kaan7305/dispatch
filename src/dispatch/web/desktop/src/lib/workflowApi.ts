import { getToken } from "./token";
import { ApiError } from "./api";

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
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function formatBrokerError(body: unknown, status: number): string {
  if (typeof body !== "object" || body === null) return `HTTP ${status}`;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
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

// ─── Wire types ──────────────────────────────────────────────────────────

export type WorkflowNodeType =
  | "trigger.manual"
  | "dispatch"
  | "notify"
  | "wait_reply";

export interface WorkflowNode {
  id: string;
  type: string;            // WorkflowNodeType, but allow unknowns from server
  pos: [number, number];
  params: Record<string, unknown>;
}

export interface WorkflowEdge {
  from: string;
  from_port: string;
  to: string;
  to_port: string;
}

export interface WorkflowDefinition {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowSummary {
  workflow_id: string;
  name: string;
  node_count: number;
  created_at: string;
  updated_at: string;
}

export interface Workflow extends WorkflowSummary {
  definition: WorkflowDefinition;
}

export interface WorkflowCreateRequest {
  name: string;
  definition: WorkflowDefinition;
}

export type NodeStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "skipped";

export interface NodeState {
  status: NodeStatus;
  output: unknown;
  dispatch_id?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  error?: string | null;
}

export type WorkflowRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface WorkflowRun {
  run_id: string;
  workflow_id: string;
  triggered_by: string;
  status: WorkflowRunStatus;
  input: Record<string, unknown>;
  node_states: Record<string, NodeState>;
  error?: string | null;
  started_at: string;
  ended_at?: string | null;
}

// ─── Client ──────────────────────────────────────────────────────────────

export const workflows = {
  list: () => request<{ workflows: WorkflowSummary[] }>("/api/workflows"),
  get: (id: string) => request<Workflow>(`/api/workflows/${id}`),
  create: (body: WorkflowCreateRequest) =>
    request<{ workflow_id: string }>("/api/workflows", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (id: string, body: WorkflowCreateRequest) =>
    request<{ status: string }>(`/api/workflows/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  remove: (id: string) =>
    request<{ status: string }>(`/api/workflows/${id}`, { method: "DELETE" }),
  run: (id: string, input: Record<string, unknown>) =>
    request<{ run_id: string }>(`/api/workflows/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ input }),
    }),
  getRun: (runId: string) => request<WorkflowRun>(`/api/runs/${runId}`),
  listRuns: (workflowId: string) =>
    request<{ runs: WorkflowRun[] }>(`/api/workflows/${workflowId}/runs`),
  cancelRun: (runId: string) =>
    request<{ status: string }>(`/api/runs/${runId}/cancel`, { method: "POST" }),
};
