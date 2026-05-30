import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  AlertCircle,
  Bell,
  CheckCircle,
  Clock,
  Code,
  GitBranch,
  Globe,
  Hourglass,
  Loader2,
  Pause,
  Play,
  Send,
  Users,
} from "lucide-react";

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

export function BranchNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const left = (d.params?.left as string | undefined) || "value";
  const op = (d.params?.op as string | undefined) || "==";
  const right = (d.params?.right as string | undefined) || "expected";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<GitBranch className="size-6 text-zinc-700" />}
      title={d.label || "Branch"}
      subtitle={`${truncate(left, 8)} ${op} ${truncate(right, 8)}`}
      showLeftHandle
      showRightHandle={false}
      rightHandles={[
        { id: "out_true",  label: "true",  topPct: 30 },
        { id: "out_false", label: "false", topPct: 70 },
      ]}
    />
  );
}

export function MultiDispatchNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const ids = (d.params?.recipient_ids as string[] | undefined) ?? [];
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Users className="size-6 text-zinc-700" />}
      title={d.label || "Fan-out"}
      subtitle={
        ids.length === 0
          ? "no recipients"
          : ids.length === 1
            ? ids[0]
            : `${ids.length} recipients`
      }
      showLeftHandle
      showRightHandle
    />
  );
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
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

export function CronTriggerNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const expr = (d.params?.expression as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-indigo-50 to-violet-50 border-indigo-300"
      icon={<Clock className="size-7 text-indigo-600" />}
      title={d.label || "When schedule fires"}
      subtitle={expr || "Cron schedule"}
      showLeftHandle={false}
      showRightHandle
      loud
    />
  );
}

export function CodeNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const code = (d.params?.code as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-zinc-50 border-zinc-300"
      icon={<Code className="size-6 text-zinc-700" />}
      title={d.label || "Code"}
      subtitle={code ? truncate(code, 24) : "no expression"}
      showLeftHandle
      showRightHandle
    />
  );
}

export function HTTPNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const method = (d.params?.method as string | undefined) ?? "GET";
  const url = (d.params?.url as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Globe className="size-6 text-zinc-700" />}
      title={d.label || "HTTP Request"}
      subtitle={`${method} ${truncate(url, 20)}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function DelayNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const seconds = (d.params?.seconds as number | undefined) ?? 0;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-sky-50/70 border-sky-200"
      icon={<Pause className="size-6 text-sky-600" />}
      title={d.label || "Wait"}
      subtitle={`${seconds}s`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function EndSuccessNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const message = (d.params?.message as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-emerald-50 border-emerald-300"
      icon={<CheckCircle className="size-6 text-emerald-600" />}
      title={d.label || "End — success"}
      subtitle={message ? truncate(message, 20) : "completes run"}
      showLeftHandle
      showRightHandle={false}
    />
  );
}

export function EndErrorNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const message = (d.params?.message as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-red-50 border-red-300"
      icon={<AlertCircle className="size-6 text-red-600" />}
      title={d.label || "End — error"}
      subtitle={message ? truncate(message, 20) : "fails run"}
      showLeftHandle
      showRightHandle={false}
    />
  );
}

export const NODE_TYPES = {
  "trigger.manual": TriggerManualNode,
  "trigger.cron": CronTriggerNode,
  dispatch: DispatchNode,
  "dispatch.multi": MultiDispatchNode,
  branch: BranchNode,
  notify: NotifyNode,
  wait_reply: WaitReplyNode,
  "transform.code": CodeNode,
  "http.request": HTTPNode,
  delay: DelayNode,
  "end.success": EndSuccessNode,
  "end.error": EndErrorNode,
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
    type: "dispatch.multi",
    label: "Fan-out dispatch",
    description: "Send the same task to multiple recipients",
    icon: <Users className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { recipient_ids: [], task: "", timeout_s: 3600 },
  },
  {
    type: "branch",
    label: "Branch",
    description: "If left {op} right → true / false outputs",
    icon: <GitBranch className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { left: "{{n1.output}}", op: "==", right: "" },
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
  {
    type: "trigger.cron",
    label: "Cron trigger",
    description: "Run on a cron schedule",
    icon: <Clock className="size-4 text-indigo-600" />,
    accent: "bg-gradient-to-br from-indigo-50 to-violet-50 border-indigo-200",
    defaultParams: { expression: "0 9 * * *", input: {} },
  },
  {
    type: "transform.code",
    label: "Code",
    description: "Run a Python expression",
    icon: <Code className="size-4 text-zinc-700" />,
    accent: "bg-zinc-50 border-zinc-300",
    defaultParams: { code: "ctx" },
  },
  {
    type: "http.request",
    label: "HTTP Request",
    description: "HTTP API call",
    icon: <Globe className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { method: "GET", url: "", headers: {}, timeout_s: 30 },
  },
  {
    type: "delay",
    label: "Delay",
    description: "Pause for N seconds",
    icon: <Pause className="size-4 text-sky-600" />,
    accent: "bg-sky-50/70 border-sky-200",
    defaultParams: { seconds: 5 },
  },
  {
    type: "end.success",
    label: "End — success",
    description: "Halt with success",
    icon: <CheckCircle className="size-4 text-emerald-600" />,
    accent: "bg-emerald-50 border-emerald-300",
    defaultParams: { message: "" },
  },
  {
    type: "end.error",
    label: "End — error",
    description: "Halt with failure",
    icon: <AlertCircle className="size-4 text-red-600" />,
    accent: "bg-red-50 border-red-300",
    defaultParams: { message: "" },
  },
];
