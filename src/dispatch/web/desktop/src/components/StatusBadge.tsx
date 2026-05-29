import { cn } from "@/lib/utils";
import type { DispatchStatus } from "@/lib/api";

const LABEL: Record<DispatchStatus, string> = {
  pending:   "Pending",
  delivered: "Pending",
  accepted:  "Accepted",
  running:   "Running",
  completed: "Completed",
  denied:    "Rejected",
  failed:    "Failed",
  expired:   "Expired",
  cancelled: "Cancelled",
};

const STYLE: Record<DispatchStatus, { wrap: string; dot: string }> = {
  pending:   { wrap: "bg-amber-50 text-amber-900 ring-amber-200",   dot: "bg-amber-500" },
  delivered: { wrap: "bg-amber-50 text-amber-900 ring-amber-200",   dot: "bg-amber-500" },
  accepted:  { wrap: "bg-sky-50 text-sky-900 ring-sky-200",         dot: "bg-sky-500" },
  running:   { wrap: "bg-sky-50 text-sky-900 ring-sky-200",         dot: "bg-sky-500 animate-pulse" },
  completed: { wrap: "bg-emerald-50 text-emerald-900 ring-emerald-200", dot: "bg-emerald-500" },
  denied:    { wrap: "bg-red-50 text-red-900 ring-red-200",         dot: "bg-red-500" },
  failed:    { wrap: "bg-red-50 text-red-900 ring-red-200",         dot: "bg-red-500" },
  expired:   { wrap: "bg-zinc-100 text-zinc-700 ring-zinc-200",     dot: "bg-zinc-400" },
  cancelled: { wrap: "bg-zinc-100 text-zinc-700 ring-zinc-200",     dot: "bg-zinc-400" },
};

export function StatusBadge({ status }: { status: DispatchStatus }) {
  const s = STYLE[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full ring-1 ring-inset px-2 py-0.5 text-xs font-medium",
        s.wrap,
      )}
    >
      <span className={cn("size-1.5 rounded-full", s.dot)} />
      {LABEL[status]}
    </span>
  );
}
