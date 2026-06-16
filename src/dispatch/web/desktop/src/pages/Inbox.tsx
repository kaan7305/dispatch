import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight } from "@/lib/icons";

import { api, type InboxEntry, type DispatchSummary } from "@/lib/api";
import { DispatchRow } from "@/components/DispatchRow";
import { SegmentedTabs } from "@/components/SegmentedTabs";
import { StatusBadge } from "@/components/StatusBadge";
import { initials, plainPreview, relativeTime } from "@/lib/format";

type Tab = "inbox" | "sent";
type Filter = "all" | "pending" | "running" | "completed" | "rejected";
type Row = InboxEntry | DispatchSummary;

// How many threads to render at once. The list is paginated client-side (the
// daemon/broker return the full set); a "Load more" button reveals the next
// page so a long history doesn't render hundreds of rows up front.
const PAGE_SIZE = 30;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all",       label: "All" },
  { value: "pending",   label: "Pending" },
  { value: "running",   label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "rejected",  label: "Rejected" },
];

export default function Inbox() {
  const navigate = useNavigate();
  // Tab + filter live in the URL so that returning here with the browser Back
  // button (from a dispatch detail) restores the exact view you left, not the
  // default Inbox/All. Written with replace so toggling doesn't spam history.
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "inbox";
  const filter = (params.get("filter") as Filter) || "all";
  const setParam = (key: string, value: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(key, value);
        return next;
      },
      { replace: true },
    );

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

  const rows = useMemo<Row[]>(() => {
    const list: Row[] =
      tab === "inbox"
        ? (inbox.data ?? []).filter((e) => statusMatches(e.status, filter))
        : (sent.data ?? []).filter((d) => statusMatches(d.status, filter));
    // Newest first, regardless of the order the daemon/broker returned;
    // ISO-8601 timestamps compare correctly as strings.
    return [...list].sort((a, b) => b.created_at.localeCompare(a.created_at));
  }, [tab, filter, inbox.data, sent.data]);

  // Group into threads: every dispatch carries a thread_id (its own id when it's
  // a root), so follow-ups collapse under one conversation like an email thread.
  // `rows` is newest-first, so each group's first member is its latest activity
  // and group order follows the newest dispatch per thread.
  const groups = useMemo<Row[][]>(() => {
    const byThread = new Map<string, Row[]>();
    for (const r of rows) {
      const key = threadKey(r);
      const g = byThread.get(key);
      if (g) g.push(r);
      else byThread.set(key, [r]);
    }
    return Array.from(byThread.values());
  }, [rows]);

  // Pagination: reset to the first page whenever the view changes (tab/filter),
  // so switching tabs doesn't strand you deep in a previous list's pages.
  const [visible, setVisible] = useState(PAGE_SIZE);
  useEffect(() => setVisible(PAGE_SIZE), [tab, filter]);
  const shown = groups.slice(0, visible);
  const hasMore = groups.length > visible;

  const whoOf = (r: Row) =>
    tab === "inbox"
      ? (r as InboxEntry).sender_id
      : (r as DispatchSummary).recipient_id;
  const open = (dispatchId: string) => navigate(`/dispatch/${dispatchId}`);

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 py-5 flex items-center gap-4">
        <SegmentedTabs
          options={[{ value: "inbox", label: "Inbox" }, { value: "sent", label: "Sent" }]}
          value={tab}
          onChange={(v) => setParam("tab", v)}
        />
      </div>
      <div className="px-6 pb-2">
        <SegmentedTabs
          options={FILTERS}
          value={filter}
          onChange={(v) => setParam("filter", v)}
          variant="underline"
        />
      </div>

      <div className="flex-1 overflow-y-auto border-t">
        {groups.length === 0 ? (
          <EmptyState
            inboxLoading={tab === "inbox" && inbox.isLoading}
            sentLoading={tab === "sent" && sent.isLoading}
            filter={filter}
          />
        ) : (
          <>
            {shown.map((group) =>
              group.length === 1 ? (
                <DispatchRow
                  key={group[0].dispatch_id}
                  dispatchId={group[0].dispatch_id}
                  who={whoOf(group[0])}
                  task={group[0].task}
                  createdAt={group[0].created_at}
                  status={group[0].status}
                  hint={tab === "inbox" ? statusHint(group[0] as InboxEntry) : undefined}
                  emphasized={
                    tab === "inbox" &&
                    (group[0].status === "delivered" || group[0].status === "pending")
                  }
                  showQuickDecision={tab === "inbox"}
                  indented
                  onClick={() => open(group[0].dispatch_id)}
                />
              ) : (
                <ConversationGroup
                  key={threadKey(group[0])}
                  members={group}
                  whoOf={whoOf}
                  onOpen={open}
                />
              ),
            )}
            {hasMore && (
              <div className="px-6 py-4 flex items-center justify-center gap-3 text-sm">
                <span className="text-muted-foreground">
                  Showing {shown.length} of {groups.length}
                </span>
                <button
                  type="button"
                  onClick={() => setVisible((v) => v + PAGE_SIZE)}
                  className="rounded-md border px-3 py-1.5 font-medium hover:bg-muted"
                >
                  Load more
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** The thread a row belongs to: its explicit thread_id, else its own id. */
function threadKey(r: Row): string {
  return r.thread_id || r.dispatch_id;
}

/** A collapsed email-style conversation: the latest dispatch as the headline
 *  with a thread count, expanding to the whole chain (oldest numbered 1). */
function ConversationGroup({
  members, whoOf, onOpen,
}: {
  members: Row[];
  whoOf: (r: Row) => string;
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const head = members[0]; // newest
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="relative border-b">
      {/* Chevron sits in the row's left gutter (absolute), so the avatar lands
          at the same x as a non-threaded (indented) row's avatar. */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="absolute left-4 top-4 z-10 grid place-items-center size-5 rounded hover:bg-muted text-muted-foreground"
        aria-label={open ? "Collapse thread" : "Expand thread"}
        aria-expanded={open}
      >
        <Chevron className="size-4" />
      </button>
      <div
        role="button"
        tabIndex={0}
        onClick={() => onOpen(head.dispatch_id)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onOpen(head.dispatch_id); }}
        className="w-full text-left flex items-start gap-4 pl-12 pr-6 py-4 hover:bg-muted/50 cursor-pointer"
      >
        <div className="grid place-items-center size-9 rounded-full bg-muted text-xs font-semibold shrink-0">
          {initials(whoOf(head))}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">{whoOf(head)}</span>
            <StatusBadge status={head.status} />
          </div>
          <div className="mt-1 text-sm leading-snug line-clamp-2">{plainPreview(head.task)}</div>
          <div className="mt-1 text-xs text-muted-foreground">{relativeTime(head.created_at)}</div>
        </div>
      </div>
      {open && (
        <div className="border-t bg-muted/10">
          {members.map((m) => (
            <button
              key={m.dispatch_id}
              type="button"
              onClick={() => onOpen(m.dispatch_id)}
              className="w-full text-left flex items-start gap-3 pl-16 pr-6 py-3 hover:bg-muted/50 border-b last:border-b-0"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <StatusBadge status={m.status} />
                  <span className="text-xs text-muted-foreground">{relativeTime(m.created_at)}</span>
                </div>
                <div className="mt-0.5 text-sm line-clamp-1">{plainPreview(m.task)}</div>
              </div>
            </button>
          ))}
        </div>
      )}
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
  const pending = Object.keys(entry.pending_tools).length;
  if (pending > 0) return `${pending} tool${pending === 1 ? "" : "s"} to approve`;

  const tools = entry.scopes.tools ?? [];
  const mcp = entry.scopes.mcp ?? [];

  // No scope data on this entry, e.g. it was hydrated from the broker's dispatch
  // list after a daemon restart, which doesn't carry per-edge scopes (see daemon
  // seed_from_broker / api.summaryToInboxEntry, both set scopes={}). Show no
  // badge rather than asserting "read-only", which we can't actually know.
  if (tools.length === 0 && mcp.length === 0) return undefined;

  if (tools.includes("Write") || tools.includes("Edit") || tools.includes("Bash")) {
    return "write";
  }
  // MCP grants reach the recipient's powerful tools (Notion, search, etc.), far
  // from read-only, so surface them rather than collapsing to "read-only".
  if (mcp.length > 0) return "MCP";
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
