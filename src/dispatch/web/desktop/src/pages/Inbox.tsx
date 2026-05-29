import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { api, type InboxEntry, type DispatchSummary } from "@/lib/api";
import { DispatchRow } from "@/components/DispatchRow";
import { SegmentedTabs } from "@/components/SegmentedTabs";

type Tab = "inbox" | "sent";
type Filter = "all" | "pending" | "running" | "completed" | "rejected";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all",       label: "All" },
  { value: "pending",   label: "Pending" },
  { value: "running",   label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "rejected",  label: "Rejected" },
];

export default function Inbox() {
  const [tab, setTab]       = useState<Tab>("inbox");
  const [filter, setFilter] = useState<Filter>("all");
  const navigate = useNavigate();

  const inbox = useQuery({
    queryKey: ["inbox"],
    queryFn: () => api.inbox(),
    enabled: tab === "inbox",
  });

  const sent = useQuery({
    queryKey: ["sent"],
    queryFn: () => api.dispatches("sent"),
    enabled: tab === "sent",
  });

  const rows = useMemo(() => {
    if (tab === "inbox") {
      return (inbox.data ?? []).filter((e) => statusMatches(e.status, filter));
    }
    return (sent.data ?? []).filter((d) => statusMatches(d.status, filter));
  }, [tab, filter, inbox.data, sent.data]);

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 pt-6 pb-3 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {tab === "inbox" ? "Inbox" : "Sent"}
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            {tab === "inbox"
              ? "Dispatches sent to you"
              : "Dispatches you've sent"}
          </p>
        </div>
        <SegmentedTabs
          options={[{ value: "inbox", label: "Inbox" }, { value: "sent", label: "Sent" }]}
          value={tab}
          onChange={setTab}
        />
      </div>
      <div className="px-6 pb-3">
        <SegmentedTabs
          options={FILTERS}
          value={filter}
          onChange={setFilter}
          variant="underline"
        />
      </div>

      <div className="flex-1 overflow-y-auto border-t border-border/60">
        {rows.length === 0 ? (
          <EmptyState
            inboxLoading={tab === "inbox" && inbox.isLoading}
            sentLoading={tab === "sent" && sent.isLoading}
            filter={filter}
          />
        ) : (
          rows.map((row) => (
            <DispatchRow
              key={row.dispatch_id}
              dispatchId={row.dispatch_id}
              who={tab === "inbox"
                ? (row as InboxEntry).sender_id
                : (row as DispatchSummary).recipient_id}
              task={row.task}
              createdAt={row.created_at}
              status={row.status}
              hint={tab === "inbox" ? statusHint(row as InboxEntry) : undefined}
              emphasized={
                tab === "inbox" &&
                (row.status === "delivered" || row.status === "pending")
              }
              showQuickDecision={tab === "inbox"}
              onClick={() => navigate(`/dispatch/${row.dispatch_id}`)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function statusMatches(status: string, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "pending")   return status === "pending" || status === "delivered";
  if (filter === "running")   return status === "running" || status === "accepted";
  if (filter === "completed") return status === "completed";
  if (filter === "rejected")  return status === "denied" || status === "failed" || status === "expired" || status === "cancelled";
  return true;
}

function statusHint(entry: InboxEntry): string | undefined {
  if (entry.status === "delivered" || entry.status === "pending") return "needs approval";
  if (Object.keys(entry.pending_tools).length > 0) return `${Object.keys(entry.pending_tools).length} tool to approve`;
  const scopeTools = entry.scopes.tools ?? [];
  if (scopeTools.includes("Write") || scopeTools.includes("Edit") || scopeTools.includes("Bash")) {
    return "write";
  }
  return "read-only";
}

function EmptyState({
  inboxLoading, sentLoading, filter,
}: { inboxLoading: boolean; sentLoading: boolean; filter: Filter }) {
  if (inboxLoading || sentLoading) {
    return <div className="px-6 py-12 text-sm text-muted-foreground">Loading…</div>;
  }
  return (
    <div className="px-6 py-12 text-sm text-muted-foreground">
      No dispatches{filter === "all" ? "" : ` matching "${filter}"`}.
    </div>
  );
}
