import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { Bell } from "lucide-react";

import { cn } from "@/lib/utils";
import type { NodeData } from "./types";

export function NotifyNode({ data, selected }: NodeProps) {
  const params = (data ?? {}) as NodeData;
  const title = (params.title as string | undefined) ?? "Notification";
  const body = (params.body as string | undefined) ?? "";

  return (
    <div
      className={cn(
        "w-[120px] rounded-md border border-neutral-200 bg-white shadow-sm overflow-hidden",
        selected && "ring-2 ring-indigo-500",
      )}
    >
      <div className="bg-amber-100 text-amber-900 px-2 py-1.5 flex items-center gap-1.5 border-b border-amber-200">
        <Bell className="size-3" />
        <span className="text-xs font-semibold truncate">{title}</span>
      </div>

      <div className="px-2 py-1.5">
        {body ? (
          <p className="text-[10px] leading-snug text-neutral-700 line-clamp-2">
            {body}
          </p>
        ) : (
          <p className="text-[10px] text-muted-foreground italic">
            macOS notification
          </p>
        )}
      </div>

      <Handle
        type="target"
        position={Position.Left}
        id="in"
        className="!bg-amber-500 !border-white !w-2.5 !h-2.5"
      />
    </div>
  );
}
