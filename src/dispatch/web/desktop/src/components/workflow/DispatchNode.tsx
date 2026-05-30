import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";

import { cn } from "@/lib/utils";
import { initials } from "@/lib/format";
import type { NodeStatus } from "@/lib/workflowApi";
import type { NodeData } from "./types";

const STATUS_BADGE: Record<NodeStatus, { label: string; className: string }> = {
  pending:   { label: "Pending",   className: "bg-neutral-100 text-neutral-700" },
  running:   { label: "Running",   className: "bg-indigo-100 text-indigo-800 animate-pulse" },
  completed: { label: "Completed", className: "bg-emerald-100 text-emerald-800" },
  failed:    { label: "Failed",    className: "bg-red-100 text-red-800" },
  skipped:   { label: "Skipped",   className: "bg-neutral-100 text-neutral-500" },
};

export function DispatchNode({ data, selected }: NodeProps) {
  const params = (data ?? {}) as NodeData;
  const recipient = (params.recipient_id as string | undefined) ?? "—";
  const task = (params.task as string | undefined) ?? "";
  const status: NodeStatus = params.state?.status ?? "pending";
  const badge = STATUS_BADGE[status];

  return (
    <div
      className={cn(
        "w-[120px] rounded-md border border-neutral-200 bg-white shadow-sm overflow-hidden",
        selected && "ring-2 ring-indigo-500",
      )}
    >
      <div className="px-2 py-1.5 flex items-center gap-1.5 border-b border-neutral-100">
        <div className="grid place-items-center size-5 rounded-full bg-neutral-100 text-[9px] font-semibold text-neutral-700 shrink-0">
          {initials(recipient)}
        </div>
        <span className="text-[10px] font-medium text-neutral-700 truncate">
          {recipient}
        </span>
      </div>

      <div className="px-2 py-1.5">
        {task ? (
          <p className="text-[10px] leading-snug text-neutral-700 line-clamp-2">
            {task}
          </p>
        ) : (
          <p className="text-[10px] text-muted-foreground italic">
            No task set
          </p>
        )}
      </div>

      <div className="px-2 pb-1.5 pt-0.5">
        <span
          className={cn(
            "inline-block rounded px-1.5 py-0.5 text-[9px] font-medium",
            badge.className,
          )}
        >
          {badge.label}
        </span>
      </div>

      <Handle
        type="target"
        position={Position.Left}
        id="in"
        className="!bg-neutral-400 !border-white !w-2.5 !h-2.5"
      />
      <Handle
        type="source"
        position={Position.Right}
        id="out"
        className="!bg-neutral-400 !border-white !w-2.5 !h-2.5"
      />
    </div>
  );
}
