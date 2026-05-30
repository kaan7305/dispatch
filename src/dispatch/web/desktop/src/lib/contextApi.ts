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

export interface ContextFile {
  path: string;
  content: string;
}

export interface ContextSummary {
  context_id: string;
  name: string;
  description: string;
  file_count: number;
  has_system_prompt: boolean;
  created_at: string;
  updated_at: string;
}

export interface ContextPack extends ContextSummary {
  owner_id: string;
  system_prompt: string;
  files: ContextFile[];
}

export interface ContextWriteBody {
  name: string;
  description: string;
  system_prompt: string;
  files: ContextFile[];
}

export const contexts = {
  list: () => request<{ contexts: ContextSummary[] }>("/api/contexts"),
  get: (id: string) => request<ContextPack>(`/api/contexts/${id}`),
  create: (body: ContextWriteBody) =>
    request<{ context_id: string }>("/api/contexts", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (id: string, body: ContextWriteBody) =>
    request<{ status: string }>(`/api/contexts/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  remove: (id: string) =>
    request<{ status: string }>(`/api/contexts/${id}`, { method: "DELETE" }),
};
