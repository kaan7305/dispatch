import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, X } from "@/lib/icons";

import { api, type DispatchStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { initials, plainPreview, relativeTime } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

interface Props {
  dispatchId: string;
  who: string;
  task: string;
  createdAt: string;
  status: DispatchStatus;
  hint?: string;
  onClick?: () => void;
  emphasized?: boolean;
  /** When true and status is pending/delivered, show inline Accept/Reject. */
  showQuickDecision?: boolean;
  /** Reserve a left gutter (for a thread chevron) so this row's avatar lines up
   *  with threaded conversation rows in the same list. */
  indented?: boolean;
}

export function DispatchRow({
  dispatchId, who, task, createdAt, status, hint, onClick, emphasized, showQuickDecision, indented,
}: Props) {
  const decisionPending = status === "pending" || status === "delivered";
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"accept" | "reject" | null>(null);
  const decide = useMutation({
    mutationFn: (decision: "accept" | "reject") => api.decide(dispatchId, decision),
    onMutate: (d) => setBusy(d),
    onSettled: () => {
      setBusy(null);
      qc.invalidateQueries({ queryKey: ["inbox"] });
      qc.invalidateQueries({ queryKey: ["dispatch", dispatchId] });
    },
  });

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onClick?.(); }}
      className={cn(
        "w-full text-left flex items-start gap-4 py-4 border-b transition-colors cursor-pointer",
        indented ? "pl-12 pr-6" : "px-6",
        "hover:bg-muted/50 focus:outline-none focus-visible:bg-muted/60",
        emphasized && "bg-amber-50/60 hover:bg-amber-50",
      )}
    >
      <div className="grid place-items-center size-9 rounded-full bg-muted text-xs font-semibold shrink-0">
        {initials(who)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{who}</span>
          <StatusBadge status={status} />
          {hint && <Badge variant="outline">{hint}</Badge>}
        </div>
        <div className="mt-1 text-sm leading-snug line-clamp-2">{plainPreview(task)}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {relativeTime(createdAt)}
        </div>
      </div>

      {showQuickDecision && decisionPending && (
        <div
          className="flex gap-2 shrink-0 self-center"
          onClick={(e) => e.stopPropagation()}
        >
          <Button
            variant="outline"
            size="sm"
            disabled={busy !== null}
            onClick={() => decide.mutate("reject")}
          >
            <X className="size-3.5" />
            {busy === "reject" ? "…" : "Reject"}
          </Button>
          <Button
            size="sm"
            disabled={busy !== null}
            onClick={() => decide.mutate("accept")}
          >
            <Check className="size-3.5" />
            {busy === "accept" ? "…" : "Accept"}
          </Button>
        </div>
      )}
    </div>
  );
}
