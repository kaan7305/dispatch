import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, X } from "lucide-react";

import { api, type DispatchStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { avatarStyle, displayName, initials, relativeTime } from "@/lib/format";
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
}

export function DispatchRow({
  dispatchId, who, task, createdAt, status, hint, onClick, emphasized, showQuickDecision,
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
        "group relative w-full text-left flex items-start gap-4 px-6 py-4 border-b transition-all cursor-pointer",
        "hover:bg-muted/40 focus:outline-none focus-visible:bg-muted/60",
        emphasized && "bg-gradient-to-r from-amber-50/80 to-transparent hover:from-amber-50",
      )}
    >
      {/* Unread/pending accent rail */}
      {emphasized && (
        <span className="absolute inset-y-0 left-0 w-0.5 bg-amber-400" />
      )}

      <div
        className="grid place-items-center size-10 rounded-full text-sm font-semibold shrink-0 shadow-sm ring-1 ring-black/5"
        style={avatarStyle(who)}
      >
        {initials(who)}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold tracking-tight">{displayName(who)}</span>
          <span className="text-xs text-muted-foreground truncate">{who}</span>
        </div>
        <div className="mt-1 flex items-center gap-2 flex-wrap">
          <StatusBadge status={status} />
          {hint && (
            <Badge variant="outline" className="rounded-full font-normal">
              {hint}
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">·</span>
          <span className="text-xs text-muted-foreground">
            {relativeTime(createdAt)}
          </span>
        </div>
        <div className="mt-2 text-sm leading-snug line-clamp-2 text-foreground/90">
          {task}
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
