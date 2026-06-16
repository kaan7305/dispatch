import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { Play } from "@/lib/icons";

import { cn } from "@/lib/utils";
import type { InputSchemaEntry, NodeData } from "./types";

export function TriggerNode({ data, selected }: NodeProps) {
  const params = (data ?? {}) as NodeData;
  const schema = (params.input_schema as InputSchemaEntry[] | undefined) ?? [];

  return (
    <div
      className={cn(
        "w-[120px] rounded-md border border-neutral-200 bg-white shadow-sm overflow-hidden",
        selected && "ring-2 ring-indigo-500",
      )}
    >
      <div className="bg-gradient-to-br from-indigo-500 to-violet-600 text-white px-2 py-1.5 flex items-center gap-1.5">
        <Play className="size-3 fill-current" />
        <span className="text-xs font-semibold">Trigger</span>
      </div>

      <div className="px-2 py-1.5">
        {schema.length === 0 ? (
          <p className="text-[10px] text-muted-foreground italic">
            No inputs
          </p>
        ) : (
          <ul className="space-y-0.5">
            {schema.map((entry) => (
              <li
                key={entry.key}
                className="text-[10px] text-neutral-700 truncate"
              >
                <span className="text-neutral-400">•</span> {entry.key}
              </li>
            ))}
          </ul>
        )}
      </div>

      <Handle
        type="source"
        position={Position.Right}
        id="out"
        className="!bg-indigo-500 !border-white !w-2.5 !h-2.5"
      />
    </div>
  );
}
