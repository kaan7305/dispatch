import { Handle, Position, type NodeProps } from "@xyflow/react";
import { Bell, Hourglass, Loader2, Play, Send } from "lucide-react";

import { cn } from "@/lib/utils";
import type { NodeStatus } from "@/lib/workflowApi";

// n8n-style nodes: a square icon card with the human label and subtitle
// rendered BELOW the card (not inside). Handles are flush against the card
// edges so the connection lines visually start at the node, not at a stub.
//
// Status-driven styling is applied to the card border so the run view
// can recolor live without resizing or shifting the layout.

type StatusKey = NodeStatus | undefined;

interface NodeData {
  label?: string;
  params?: Record<string, unknown>;
  status?: StatusKey;
  output?: unknown;
  error?: string | null;
}

const STATUS_RING: Record<NodeStatus, string> = {
  pending:   "border-zinc-200",
  running:   "border-indigo-500 ring-4 ring-indigo-500/15",
  completed: "border-emerald-500",
  failed:    "border-red-500",
  skipped:   "border-zinc-200 opacity-50",
};

const HANDLE_CLASS =
  "!w-3 !h-3 !bg-white !border-2 !border-zinc-400 hover:!border-indigo-500 transition-colors";

function NodeShell({
  status,
  selected,
  accent,           // tailwind classes for the card body color
  icon,
  title,
  subtitle,
  showLeftHandle,
  showRightHandle,
  rightHandles,
  loud,             // make the icon larger / accent stronger (trigger node)
}: {
  status: StatusKey;
  selected: boolean;
  accent: string;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  showLeftHandle: boolean;
  showRightHandle: boolean;
  rightHandles?: { id: string; label: string; topPct: number }[];
  loud?: boolean;
}) {
  const ring = status ? STATUS_RING[status] : "border-zinc-200";
  const selectionRing =
    selected && !status ? "ring-2 ring-indigo-400/70" : "";

  return (
    <div className="flex flex-col items-center select-none">
      <div
        className={cn(
          "relative grid place-items-center bg-white border-2 rounded-2xl shadow-sm transition-all",
          loud ? "size-20 rounded-[28px]" : "size-[72px]",
          ring,
          selectionRing,
          accent,
        )}
      >
        {showLeftHandle && (
          <Handle
            type="target"
            position={Position.Left}
            className={HANDLE_CLASS}
          />
        )}

        <div className={cn("text-foreground", loud ? "scale-110" : "")}>
          {icon}
        </div>

        {/* Live status badge */}
        {status === "running" && (
          <span className="absolute -top-1.5 -right-1.5 grid place-items-center size-5 rounded-full bg-indigo-500 ring-2 ring-white">
            <Loader2 className="size-3 text-white animate-spin" />
          </span>
        )}
        {status === "completed" && (
          <span className="absolute -top-1.5 -right-1.5 grid place-items-center size-5 rounded-full bg-emerald-500 text-white text-[10px] font-bold ring-2 ring-white">
            ✓
          </span>
        )}
        {status === "failed" && (
          <span className="absolute -top-1.5 -right-1.5 grid place-items-center size-5 rounded-full bg-red-500 text-white text-[10px] font-bold ring-2 ring-white">
            !
          </span>
        )}

        {/* Multiple labeled output handles (e.g. true/false on a branch). */}
        {!showRightHandle && rightHandles?.length
          ? rightHandles.map((h) => (
              <div
                key={h.id}
                style={{ top: `${h.topPct}%` }}
                className="absolute -right-6 translate-y-[-50%] text-[10px] text-muted-foreground font-medium"
              >
                <Handle
                  type="source"
                  position={Position.Right}
                  id={h.id}
                  className={HANDLE_CLASS}
                />
                <span className="ml-3">{h.label}</span>
              </div>
            ))
          : null}

        {showRightHandle && (
          <Handle
            type="source"
            position={Position.Right}
            className={HANDLE_CLASS}
          />
        )}
      </div>

      {/* Title + subtitle live BELOW the card, n8n-style. */}
      <div className="mt-2 max-w-[140px] text-center">
        <div className="text-[13px] font-semibold leading-tight text-foreground truncate">
          {title}
        </div>
        {subtitle && (
          <div className="text-[11px] text-muted-foreground truncate mt-0.5">
            {subtitle}
          </div>
        )}
      </div>
    </div>
  );
}

export function TriggerManualNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-indigo-50 to-violet-50 border-indigo-300"
      icon={<Play className="size-7 text-indigo-600 fill-indigo-600/10" />}
      title={d.label || "When run starts"}
      subtitle="Manual trigger"
      showLeftHandle={false}
      showRightHandle
      loud
    />
  );
}

export function DispatchNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const recipient = (d.params?.recipient_id as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white"
      icon={<Send className="size-6 text-zinc-700" />}
      title={d.label || "Dispatch"}
      subtitle={recipient || "no recipient"}
      showLeftHandle
      showRightHandle
    />
  );
}

export function NotifyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const title = (d.params?.title as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-amber-50/60 border-amber-200"
      icon={<Bell className="size-6 text-amber-600" />}
      title={d.label || "Notify"}
      subtitle={title || "macOS notification"}
      showLeftHandle
      showRightHandle={false}
    />
  );
}

export function WaitReplyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const from = (d.params?.from_recipient_id as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-sky-50/70 border-sky-200"
      icon={<Hourglass className="size-6 text-sky-600" />}
      title={d.label || "Wait for reply"}
      subtitle={from ? `from ${from}` : "any sender"}
      showLeftHandle
      showRightHandle
    />
  );
}

export const NODE_TYPES = {
  "trigger.manual": TriggerManualNode,
  dispatch: DispatchNode,
  notify: NotifyNode,
  wait_reply: WaitReplyNode,
};

export interface PaletteItem {
  type: keyof typeof NODE_TYPES;
  label: string;
  description: string;
  icon: React.ReactNode;
  accent: string;
  defaultParams: Record<string, unknown>;
}

export const PALETTE: PaletteItem[] = [
  {
    type: "trigger.manual",
    label: "Manual trigger",
    description: "Start node with user input",
    icon: <Play className="size-4 text-indigo-600" />,
    accent: "bg-gradient-to-br from-indigo-50 to-violet-50 border-indigo-200",
    defaultParams: { input_schema: {} },
  },
  {
    type: "dispatch",
    label: "Dispatch",
    description: "Send a task to one recipient",
    icon: <Send className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { recipient_id: "", task: "" },
  },
  {
    type: "notify",
    label: "Notify",
    description: "Local macOS notification",
    icon: <Bell className="size-4 text-amber-600" />,
    accent: "bg-amber-50/60 border-amber-200",
    defaultParams: { title: "Dispatch", message: "" },
  },
  {
    type: "wait_reply",
    label: "Wait for reply",
    description: "Pause until a dispatch comes back",
    icon: <Hourglass className="size-4 text-sky-600" />,
    accent: "bg-sky-50/70 border-sky-200",
    defaultParams: { from_recipient_id: "" },
  },
];
