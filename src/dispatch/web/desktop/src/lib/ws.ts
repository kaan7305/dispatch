import { getToken } from "./token";

export type EventMessage =
  | { type: "snapshot"; data: unknown[] }
  | { type: "inbox_new"; data: unknown }
  | { type: "dispatch_status"; dispatch_id: string; data: { status: string } }
  | { type: "dispatch_event"; dispatch_id: string; data: { type: string; data: unknown } };

export function openEventStream(onMessage: (m: EventMessage) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let backoff = 500;

  const connect = () => {
    if (closed) return;
    const url =
      `${location.origin.replace(/^http/, "ws")}/ws/events` +
      `?t=${encodeURIComponent(getToken())}`;
    ws = new WebSocket(url);
    ws.addEventListener("message", (e) => {
      try { onMessage(JSON.parse(e.data) as EventMessage); } catch { /* ignore */ }
    });
    ws.addEventListener("open", () => { backoff = 500; });
    ws.addEventListener("close", () => {
      if (closed) return;
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 15000);
    });
    ws.addEventListener("error", () => ws?.close());
  };

  connect();
  return () => {
    closed = true;
    ws?.close();
  };
}
