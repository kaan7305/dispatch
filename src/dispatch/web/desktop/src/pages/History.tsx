import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";

import { api, type DispatchSummary } from "@/lib/api";
import { initials, relativeTime } from "@/lib/format";
import { SegmentedTabs } from "@/components/SegmentedTabs";
import { StatusBadge } from "@/components/StatusBadge";
import { Badge } from "@/components/ui/badge";

type Tab = "all" | "sent" | "received";

export default function History() {
  const [tab, setTab] = useState<Tab>("all");

  const [sentQ, receivedQ] = useQueries({
    queries: [
      { queryKey: ["history", "sent"],     queryFn: () => api.dispatches("sent") },
      { queryKey: ["history", "received"], queryFn: () => api.dispatches("received") },
    ],
  });

  const rows = useMemo(() => {
    const sent =     (sentQ.data ?? []).map((d) => ({ ...d, _direction: "sent"     as const }));
    const received = (receivedQ.data ?? []).map((d) => ({ ...d, _direction: "received" as const }));
    let all = [...sent, ...received];
    if (tab === "sent")     all = sent;
    if (tab === "received") all = received;
    return all.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }, [tab, sentQ.data, receivedQ.data]);

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
            {sentQ.isLoading || receivedQ.isLoading ? "Loading…" : "No history yet."}
          </div>
        ) : (
          rows.map((row) => <HistoryRow key={row.dispatch_id} row={row} />)
        )}
      </div>
    </div>
  );
}

function HistoryRow({
  row,
}: {
  row: DispatchSummary & { _direction: "sent" | "received" };
}) {
  const peer = row._direction === "sent" ? row.recipient_id : row.sender_id;
  return (
    <button className="w-full text-left flex items-start gap-4 px-5 py-4 border-b last:border-b-0 hover:bg-muted/50">
      <div className="grid place-items-center size-9 rounded-full bg-muted text-xs font-semibold shrink-0">
        {initials(peer)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="outline" className="capitalize">{row._direction}</Badge>
          <span className="font-semibold">{peer}</span>
          <StatusBadge status={row.status} />
        </div>
        <div className="mt-1 text-sm leading-snug line-clamp-2">{row.task}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          Started {relativeTime(row.created_at)}
        </div>
      </div>
      <ChevronRight className="size-4 text-muted-foreground shrink-0 mt-1" />
    </button>
  );
}
