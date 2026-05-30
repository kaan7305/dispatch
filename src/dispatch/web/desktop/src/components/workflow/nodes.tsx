import { Handle, Position, type NodeProps } from "@xyflow/react";
import { Bell, Play, Send, Hourglass, Check, X, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { NodeStatus } from "@/lib/workflowApi";

// One styled shell so all four node types share dimensions and status colors.
// `data.status` is set by WorkflowRun for live coloring; undefined in the editor.

type StatusKey = NodeStatus | undefined;

interface NodeData {
  label?: string;
  params?: Record<string, unknown>;
  status?: StatusKey;
  output?: unknown;
  error?: string | null;
}

const STATUS_RING: Record<NodeStatus, string> = {
  pending:   "border-border",
  running:   "border-indigo-500 animate-pulse",
  completed: "border-emerald-500",
  failed:    "border-red-500",
  skipped:   "border-border opacity-50",
};

function NodeShell({
  status,
  selected,
  icon,
  title,
  subtitle,
  showLeftHandle,
  showRightHandle,
}: {
  status: StatusKey;
  selected: boolean;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  showLeftHandle: boolean;
  showRightHandle: boolean;
}) {
  const ring = status ? STATUS_RING[status] : "border-border";
  return (
    <div
      className={cn(
        "rounded-lg border-2 bg-background px-4 py-3 shadow-sm w-56 transition-colors",
        ring,
        selected && !status && "border-foreground/40",
      )}
    >
      {showLeftHandle && (
        <Handle
          type="target"
          position={Position.Left}
          className="!w-2.5 !h-2.5 !bg-muted-foreground !border-background"
        />
      )}
      <div className="flex items-center gap-2 mb-1">
        <div className="grid place-items-center size-7 rounded-md bg-secondary text-foreground shrink-0">
          {icon}
        </div>
        <div className="font-semibold text-sm truncate flex-1">{title}</div>
        <StatusGlyph status={status} />
      </div>
      {subtitle && (
        <div className="text-xs text-muted-foreground truncate pl-9">
          {subtitle}
        </div>
      )}
      {showRightHandle && (
        <Handle
          type="source"
          position={Position.Right}
          className="!w-2.5 !h-2.5 !bg-muted-foreground !border-background"
        />
      )}
    </div>
  );
}

function StatusGlyph({ status }: { status: StatusKey }) {
  if (!status || status === "pending") return null;
  if (status === "running") return <Loader2 className="size-3.5 text-indigo-500 animate-spin" />;
  if (status === "completed") return <Check className="size-3.5 text-emerald-500" />;
  if (status === "failed") return <X className="size-3.5 text-red-500" />;
  return null;
}

export function TriggerManualNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      icon={<Play className="size-3.5" />}
      title={d.label || "Manual trigger"}
      subtitle="Starts on run"
      showLeftHandle={false}
      showRightHandle
    />
  );
}

export function DispatchNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const recipient = (d.params?.recipient_id as string | undefined) || "no recipient";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      icon={<Send className="size-3.5" />}
      title={d.label || "Dispatch"}
      subtitle={`→ ${recipient}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function NotifyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      icon={<Bell className="size-3.5" />}
      title={d.label || "Notify"}
      subtitle={(d.params?.message as string | undefined) || "macOS notification"}
      showLeftHandle
      showRightHandle={false}
    />
  );
}

export function WaitReplyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const from = (d.params?.from_recipient_id as string | undefined) || "any sender";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      icon={<Hourglass className="size-3.5" />}
      title={d.label || "Wait for reply"}
      subtitle={`from ${from}`}
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
  defaultParams: Record<string, unknown>;
}

export const PALETTE: PaletteItem[] = [
  {
    type: "trigger.manual",
    label: "Manual trigger",
    description: "Start node with user input",
    icon: <Play className="size-4" />,
    defaultParams: { input_schema: {} },
  },
  {
    type: "dispatch",
    label: "Dispatch",
    description: "Send a task to one recipient",
    icon: <Send className="size-4" />,
    defaultParams: { recipient_id: "", task: "" },
  },
  {
    type: "notify",
    label: "Notify",
    description: "Local macOS notification",
    icon: <Bell className="size-4" />,
    defaultParams: { title: "Dispatch", message: "" },
  },
  {
    type: "wait_reply",
    label: "Wait for reply",
    description: "Pause until a dispatch comes back",
    icon: <Hourglass className="size-4" />,
    defaultParams: { from_recipient_id: "" },
  },
];
