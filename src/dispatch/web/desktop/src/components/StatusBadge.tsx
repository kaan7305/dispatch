import { Badge } from "./ui/badge";
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

const VARIANT: Record<DispatchStatus, "muted" | "warning" | "running" | "success" | "destructive"> = {
  pending:   "warning",
  delivered: "warning",
  accepted:  "muted",
  running:   "running",
  completed: "success",
  denied:    "destructive",
  failed:    "destructive",
  expired:   "muted",
  cancelled: "muted",
};

export function StatusBadge({ status }: { status: DispatchStatus }) {
  return <Badge variant={VARIANT[status]}>{LABEL[status]}</Badge>;
}
