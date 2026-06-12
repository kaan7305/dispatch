import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { Clock } from "lucide-react";

import { cn } from "@/lib/utils";
import type { NodeData } from "./types";

export function WaitReplyNode({ data, selected }: NodeProps) {
  const params = (data ?? {}) as NodeData;
  const from = (params.from_recipient_id as string | undefined) ?? "-";
  const timeout = (params.timeout_s as number | undefined) ?? 3600;

  return (
    <div
      className={cn(
        "w-[120px] rounded-md border border-neutral-200 bg-white shadow-sm overflow-hidden",
        selected && "ring-2 ring-indigo-500",
      )}
    >
      <div className="bg-sky-100 text-sky-900 px-2 py-1.5 flex items-center gap-1.5 border-b border-sky-200">
        <Clock className="size-3" />
        <span className="text-xs font-semibold">Wait reply</span>
      </div>

      <div className="px-2 py-1.5 space-y-0.5">
        <p className="text-[10px] text-neutral-700 truncate">
          <span className="text-neutral-400">from</span> {from}
        </p>
        <p className="text-[10px] text-neutral-500">
          timeout {timeout}s
        </p>
      </div>

      <Handle
        type="target"
        position={Position.Left}
        id="in"
        className="!bg-sky-500 !border-white !w-2.5 !h-2.5"
      />
      <Handle
        type="source"
        position={Position.Right}
        id="out"
        className="!bg-sky-500 !border-white !w-2.5 !h-2.5"
      />
    </div>
  );
}
