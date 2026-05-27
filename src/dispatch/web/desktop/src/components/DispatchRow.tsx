import { cn } from "@/lib/utils";
import { initials, relativeTime } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";
import { Badge } from "./ui/badge";
import type { DispatchStatus } from "@/lib/api";

interface Props {
  who: string;
  task: string;
  createdAt: string;
  status: DispatchStatus;
  hint?: string;
  onClick?: () => void;
  emphasized?: boolean;
}

export function DispatchRow({
  who, task, createdAt, status, hint, onClick, emphasized,
}: Props) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left flex items-start gap-4 px-6 py-4 border-b transition-colors",
        "hover:bg-muted/50",
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
        <div className="mt-1 text-sm leading-snug line-clamp-2">{task}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {relativeTime(createdAt)}
        </div>
      </div>
    </button>
  );
}
