import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  ChevronRight,
  ShieldOff,
  SlidersHorizontal,
  UserCheck,
  UserPlus,
  UserX,
} from "@/lib/icons";

import { api, type AccountEvent, type DispatchSummary, type ScopeChange } from "@/lib/api";
import { initials, plainPreview, relativeTime } from "@/lib/format";
import { SegmentedTabs } from "@/components/SegmentedTabs";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";

type Tab = "all" | "sent" | "received";

type Direction = "sent" | "received";

type Row =
  | { kind: "dispatch"; ts: string; direction: Direction; dispatch: DispatchSummary }
  | { kind: "event"; ts: string; direction: Direction; event: AccountEvent };

export default function History() {
  const [tab, setTab] = useState<Tab>("all");

  const [sentQ, receivedQ, eventsQ] = useQueries({
    queries: [
      { queryKey: ["history", "sent"],     queryFn: () => api.dispatches("sent") },
      { queryKey: ["history", "received"], queryFn: () => api.dispatches("received") },
      { queryKey: ["history", "events"],   queryFn: () => api.accountEvents() },
    ],
  });

  const rows = useMemo(() => {
    const all: Row[] = [
      ...(sentQ.data ?? []).map((d): Row => ({
        kind: "dispatch", ts: d.created_at, direction: "sent", dispatch: d,
      })),
      ...(receivedQ.data ?? []).map((d): Row => ({
        kind: "dispatch", ts: d.created_at, direction: "received", dispatch: d,
      })),
      ...(eventsQ.data ?? []).map((e): Row => ({
        kind: "event",
        ts: e.created_at,
        direction: e.direction === "outgoing" ? "sent" : "received",
        event: e,
      })),
    ];
    const filtered = tab === "all" ? all : all.filter((r) => r.direction === tab);
    return filtered.sort((a, b) => b.ts.localeCompare(a.ts));
  }, [tab, sentQ.data, receivedQ.data, eventsQ.data]);

  const loading = sentQ.isLoading || receivedQ.isLoading || eventsQ.isLoading;

  return (
    <div className="px-6 py-6">
      <h1 className="text-2xl font-semibold mb-5">History</h1>
      <div className="mb-4">
        <SegmentedTabs
          options={[
            { value: "all",      label: "All" },
            { value: "sent",     label: "Sent" },
            { value: "received", label: "Received" },
          ]}
          value={tab}
          onChange={setTab}
          variant="underline"
        />
      </div>

      <div className="rounded-lg border">
        {rows.length === 0 ? (
          <div className="px-6 py-10 text-sm text-muted-foreground">
            {loading ? "Loading…" : "No history yet."}
          </div>
        ) : (
          rows.map((row) =>
            row.kind === "dispatch" ? (
              <DispatchRow
                key={`d-${row.dispatch.dispatch_id}-${row.direction}`}
                row={row.dispatch}
                direction={row.direction}
              />
            ) : (
              <EventRow key={`e-${row.event.id}`} event={row.event} />
            ),
          )
        )}
      </div>
    </div>
  );
}

function DispatchRow({
  row,
  direction,
}: {
  row: DispatchSummary;
  direction: Direction;
}) {
  const navigate = useNavigate();
  const peer = direction === "sent" ? row.recipient_id : row.sender_id;
  return (
    <button
      onClick={() => navigate(`/dispatch/${row.dispatch_id}`)}
      className="w-full text-left flex items-start gap-4 px-5 py-4 border-b last:border-b-0 hover:bg-muted/50"
    >
      <div className="grid place-items-center size-9 rounded-full bg-muted text-xs font-semibold shrink-0">
        {initials(peer)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="outline" className="capitalize">{direction}</Badge>
          <span className="font-semibold">{peer}</span>
          <StatusBadge status={row.status} />
        </div>
        <div className="mt-1 text-sm leading-snug line-clamp-2">{plainPreview(row.task)}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          Started {relativeTime(row.created_at)}
        </div>
      </div>
      <ChevronRight className="size-4 text-muted-foreground shrink-0 mt-1" />
    </button>
  );
}

const EVENT_ICONS: Record<AccountEvent["type"], typeof UserPlus> = {
  invite_sent: UserPlus,
  invite_accepted: UserCheck,
  invite_declined: UserX,
  trust_scopes_updated: SlidersHorizontal,
  trust_revoked: ShieldOff,
};

function eventCopy(e: AccountEvent): string {
  const out = e.direction === "outgoing";
  switch (e.type) {
    case "invite_sent":
      return out
        ? `You invited ${e.peer} to connect`
        : `${e.peer} invited you to connect`;
    case "invite_accepted":
      return out
        ? `You accepted ${e.peer}'s invitation`
        : `${e.peer} accepted your invitation`;
    case "invite_declined":
      return out
        ? `You declined ${e.peer}'s invitation`
        : `${e.peer} declined your invitation`;
    case "trust_scopes_updated":
      return out
        ? `You updated the permissions you grant ${e.peer}`
        : `${e.peer} updated the permissions they grant you`;
    case "trust_revoked":
      return out
        ? `You revoked your connection with ${e.peer}`
        : `${e.peer} revoked your connection`;
  }
}

function formatScopeValue(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  return String(v);
}

function EventRow({ event }: { event: AccountEvent }) {
  const Icon = EVENT_ICONS[event.type];
  const changes: [string, ScopeChange][] = Object.entries(event.data.changes ?? {});
  const cancelled = event.data.cancelled_dispatches ?? 0;
  return (
    <div className="flex items-start gap-4 px-5 py-3.5 border-b last:border-b-0">
      <div className="grid place-items-center size-9 rounded-full bg-muted shrink-0">
        <Icon className="size-4 text-muted-foreground" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium leading-snug">{eventCopy(event)}</div>
        {changes.length > 0 && (
          <div className="mt-1 space-y-0.5">
            {changes.map(([field, c]) => (
              <div key={field} className="text-xs text-muted-foreground break-words">
                <span className="font-medium">{field}</span>:{" "}
                {formatScopeValue(c.from)} → {formatScopeValue(c.to)}
              </div>
            ))}
          </div>
        )}
        {event.type === "trust_revoked" && cancelled > 0 && (
          <div className="mt-1 text-xs text-muted-foreground">
            {cancelled} in-flight dispatch{cancelled === 1 ? "" : "es"} cancelled
          </div>
        )}
        <div className="mt-1 text-xs text-muted-foreground">
          {relativeTime(event.created_at)}
        </div>
      </div>
    </div>
  );
}
