import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  AlertCircle,
  Bell,
  Bot,
  BookText,
  Braces,
  CalendarClock,
  Calendar,
  CaseSensitive,
  CheckCircle,
  Clock,
  Code,
  Dices,
  FileInput,
  FileOutput,
  FileText,
  Filter,
  Gauge,
  GitBranch,
  Globe,
  Hash,
  Loader2,
  ListTree,
  Lock,
  Merge,
  Pause,
  Play,
  Regex,
  Route,
  ScanText,
  Sigma,
  Tags,
  Type,
  Variable,
  Volume2,
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

export function ContextNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const files =
    (d.params?.files as { path?: string; content?: string }[] | undefined) ?? [];
  const hasPrompt = !!(d.params?.system_prompt as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-emerald-50 to-teal-50 border-emerald-200"
      icon={<BookText className="size-6 text-emerald-600" />}
      title={d.label || "Context"}
      subtitle={
        files.length === 0 && !hasPrompt
          ? "empty"
          : files.length > 0
            ? `${files.length} file${files.length === 1 ? "" : "s"}${hasPrompt ? " + sys" : ""}`
            : "system prompt"
      }
      showLeftHandle
      showRightHandle
    />
  );
}

export function AgentNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const prompt = (d.params?.prompt as string | undefined)?.trim();
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-300"
      icon={<Bot className="size-6 text-zinc-700" />}
      title={d.label || "Agent"}
      subtitle={prompt ? truncate(prompt, 24) : "no prompt"}
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

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

export function SwitchNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const cases =
    (d.params?.cases as { when?: string; port?: string }[] | undefined) ?? [];
  const defaultPort =
    (d.params?.default_port as string | undefined) || "out_default";
  const rightHandles = [
    ...cases
      .filter((c) => !!c.port)
      .map((c, i) => ({
        id: c.port as string,
        label: truncate(c.when || c.port || "", 8),
        topPct: 18 + (i * 64) / Math.max(cases.length, 1),
      })),
    {
      id: defaultPort,
      label: "default",
      topPct: 86,
    },
  ];
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Route className="size-6 text-zinc-700" />}
      title={d.label || "Switch"}
      subtitle={`${cases.length} case${cases.length === 1 ? "" : "s"}`}
      showLeftHandle
      showRightHandle={false}
      rightHandles={rightHandles}
    />
  );
}

export function SetNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const values =
    (d.params?.values as Record<string, string> | undefined) ?? {};
  const keys = Object.keys(values);
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Variable className="size-6 text-zinc-700" />}
      title={d.label || "Set"}
      subtitle={
        keys.length === 0
          ? "no vars"
          : keys.length === 1
            ? keys[0]
            : `${keys.length} vars`
      }
      showLeftHandle
      showRightHandle
    />
  );
}

export function FormatNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const template = (d.params?.template as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Type className="size-6 text-zinc-700" />}
      title={d.label || "Format"}
      subtitle={template ? truncate(template, 22) : "empty template"}
      showLeftHandle
      showRightHandle
    />
  );
}

export function LogNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const level = (d.params?.level as string | undefined) ?? "info";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-zinc-50 border-zinc-200"
      icon={<FileText className="size-6 text-zinc-600" />}
      title={d.label || "Log"}
      subtitle={`level: ${level}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function RandomNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const kind = (d.params?.kind as string | undefined) ?? "uuid";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Dices className="size-6 text-zinc-700" />}
      title={d.label || "Random"}
      subtitle={kind}
      showLeftHandle
      showRightHandle
    />
  );
}

export function WaitUntilNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const until = (d.params?.until as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-sky-50/70 border-sky-200"
      icon={<CalendarClock className="size-6 text-sky-600" />}
      title={d.label || "Wait until"}
      subtitle={until ? truncate(until, 20) : "no time set"}
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
      title={d.label || "End - success"}
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
      title={d.label || "End - error"}
      subtitle={message ? truncate(message, 20) : "fails run"}
      showLeftHandle
      showRightHandle={false}
    />
  );
}

// ─── Logic / control ──────────────────────────────────────────────────────

export function FilterNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "==";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Filter className="size-6 text-zinc-700" />}
      title={d.label || "Filter"}
      subtitle={`if ${op}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function MergeNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Merge className="size-6 text-zinc-700" />}
      title={d.label || "Merge"}
      subtitle="join inputs"
      showLeftHandle
      showRightHandle
    />
  );
}

// ─── Data ops ─────────────────────────────────────────────────────────────

export function MathNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "+";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Sigma className="size-6 text-zinc-700" />}
      title={d.label || "Math"}
      subtitle={`op: ${op}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function StringNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "trim";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<CaseSensitive className="size-6 text-zinc-700" />}
      title={d.label || "String"}
      subtitle={op}
      showLeftHandle
      showRightHandle
    />
  );
}

export function RegexNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "extract";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Regex className="size-6 text-zinc-700" />}
      title={d.label || "Regex"}
      subtitle={op}
      showLeftHandle
      showRightHandle
    />
  );
}

export function JsonParseNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Braces className="size-6 text-zinc-700" />}
      title={d.label || "JSON Parse"}
      subtitle="string → object"
      showLeftHandle
      showRightHandle
    />
  );
}

export function JsonStringifyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Braces className="size-6 text-zinc-700" />}
      title={d.label || "JSON Stringify"}
      subtitle="object → string"
      showLeftHandle
      showRightHandle
    />
  );
}

export function HashNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const algo = (d.params?.algo as string | undefined) ?? "sha256";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Hash className="size-6 text-zinc-700" />}
      title={d.label || "Hash"}
      subtitle={algo}
      showLeftHandle
      showRightHandle
    />
  );
}

export function Base64Node({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "encode";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Lock className="size-6 text-zinc-700" />}
      title={d.label || "Base64"}
      subtitle={op}
      showLeftHandle
      showRightHandle
    />
  );
}

export function DateTimeNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const op = (d.params?.op as string | undefined) ?? "now";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-white border-zinc-200"
      icon={<Calendar className="size-6 text-zinc-700" />}
      title={d.label || "DateTime"}
      subtitle={op}
      showLeftHandle
      showRightHandle
    />
  );
}

// ─── File I/O ────────────────────────────────────────────────────────────

export function FileReadNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const path = (d.params?.path as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-zinc-50 border-zinc-200"
      icon={<FileInput className="size-6 text-zinc-600" />}
      title={d.label || "File read"}
      subtitle={path ? truncate(path, 20) : "no path"}
      showLeftHandle
      showRightHandle
    />
  );
}

export function FileWriteNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const path = (d.params?.path as string | undefined) ?? "";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-zinc-50 border-zinc-200"
      icon={<FileOutput className="size-6 text-zinc-600" />}
      title={d.label || "File write"}
      subtitle={path ? truncate(path, 20) : "no path"}
      showLeftHandle
      showRightHandle
    />
  );
}

// ─── AI convenience ──────────────────────────────────────────────────────

export function AIClassifyNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const cats = (d.params?.categories as string[] | undefined) ?? [];
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200"
      icon={<Tags className="size-6 text-violet-600" />}
      title={d.label || "AI Classify"}
      subtitle={`${cats.length} cat${cats.length === 1 ? "" : "s"}`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function AIExtractNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200"
      icon={<ListTree className="size-6 text-violet-600" />}
      title={d.label || "AI Extract"}
      subtitle="text → JSON"
      showLeftHandle
      showRightHandle
    />
  );
}

export function AISummarizeNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const words = (d.params?.max_words as number | undefined) ?? 50;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200"
      icon={<ScanText className="size-6 text-violet-600" />}
      title={d.label || "AI Summarize"}
      subtitle={`${words} words`}
      showLeftHandle
      showRightHandle
    />
  );
}

export function AIJudgeNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200"
      icon={<Gauge className="size-6 text-violet-600" />}
      title={d.label || "AI Judge"}
      subtitle="score 1–10"
      showLeftHandle
      showRightHandle
    />
  );
}

// ─── Notify ──────────────────────────────────────────────────────────────

export function SoundNode({ data, selected }: NodeProps) {
  const d = (data ?? {}) as NodeData;
  const sound = (d.params?.sound as string | undefined) ?? "Ping";
  return (
    <NodeShell
      status={d.status}
      selected={!!selected}
      accent="bg-amber-50/60 border-amber-200"
      icon={<Volume2 className="size-6 text-amber-600" />}
      title={d.label || "Sound"}
      subtitle={sound}
      showLeftHandle
      showRightHandle
    />
  );
}

export const NODE_TYPES = {
  "trigger.manual": TriggerManualNode,
  "trigger.cron": CronTriggerNode,
  context: ContextNode,
  agent: AgentNode,
  "ai.classify": AIClassifyNode,
  "ai.extract": AIExtractNode,
  "ai.summarize": AISummarizeNode,
  "ai.judge": AIJudgeNode,
  branch: BranchNode,
  switch: SwitchNode,
  filter: FilterNode,
  merge: MergeNode,
  set: SetNode,
  format: FormatNode,
  random: RandomNode,
  math: MathNode,
  string: StringNode,
  regex: RegexNode,
  "json.parse": JsonParseNode,
  "json.stringify": JsonStringifyNode,
  hash: HashNode,
  base64: Base64Node,
  datetime: DateTimeNode,
  "file.read": FileReadNode,
  "file.write": FileWriteNode,
  wait_until: WaitUntilNode,
  notify: NotifyNode,
  "notify.sound": SoundNode,
  log: LogNode,
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
    type: "context",
    label: "Context",
    description: "Ship files + system prompt to the recipient",
    icon: <BookText className="size-4 text-emerald-600" />,
    accent: "bg-gradient-to-br from-emerald-50 to-teal-50 border-emerald-200",
    defaultParams: {
      files: [],
      system_prompt: "",
      notes: "",
    },
  },
  {
    type: "agent",
    label: "Agent",
    description: "Run a Claude prompt on the recipient",
    icon: <Bot className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-300",
    defaultParams: { prompt: "", system_prompt: "" },
  },
  {
    type: "ai.classify",
    label: "AI Classify",
    description: "Pick one category from a list",
    icon: <Tags className="size-4 text-violet-600" />,
    accent: "bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200",
    defaultParams: { input: "{{n1.output}}", categories: ["yes", "no"] },
  },
  {
    type: "ai.extract",
    label: "AI Extract",
    description: "Text → structured JSON",
    icon: <ListTree className="size-4 text-violet-600" />,
    accent: "bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200",
    defaultParams: { input: "{{n1.output}}", schema: { name: "string", email: "string" } },
  },
  {
    type: "ai.summarize",
    label: "AI Summarize",
    description: "Condense in N words",
    icon: <ScanText className="size-4 text-violet-600" />,
    accent: "bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200",
    defaultParams: { input: "{{n1.output}}", max_words: 50 },
  },
  {
    type: "ai.judge",
    label: "AI Judge",
    description: "Score 1–10 against a criterion",
    icon: <Gauge className="size-4 text-violet-600" />,
    accent: "bg-gradient-to-br from-violet-50 to-fuchsia-50 border-violet-200",
    defaultParams: { input: "{{n1.output}}", criterion: "clarity" },
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
    type: "switch",
    label: "Switch",
    description: "Multi-way branch by value",
    icon: <Route className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: {
      value: "{{n1.output}}",
      cases: [
        { when: "yes", port: "out_yes" },
        { when: "no", port: "out_no" },
      ],
      default_port: "out_default",
    },
  },
  {
    type: "filter",
    label: "Filter",
    description: "Gate downstream on a condition",
    icon: <Filter className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { left: "{{n1.output}}", op: "is_not_empty", right: "" },
  },
  {
    type: "merge",
    label: "Merge",
    description: "Combine outputs from many incoming edges",
    icon: <Merge className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: {},
  },
  {
    type: "set",
    label: "Set",
    description: "Define variables for downstream nodes",
    icon: <Variable className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { values: {} },
  },
  {
    type: "format",
    label: "Format",
    description: "Hydrate a template string",
    icon: <Type className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { template: "" },
  },
  {
    type: "random",
    label: "Random",
    description: "Generate a UUID / int / float",
    icon: <Dices className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { kind: "uuid" },
  },
  {
    type: "math",
    label: "Math",
    description: "Arithmetic on two numbers",
    icon: <Sigma className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { op: "+", left: "0", right: "0" },
  },
  {
    type: "string",
    label: "String",
    description: "Upper / lower / trim / slice / replace",
    icon: <CaseSensitive className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { op: "trim", value: "" },
  },
  {
    type: "regex",
    label: "Regex",
    description: "Extract or replace via pattern",
    icon: <Regex className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { op: "extract", pattern: "", value: "" },
  },
  {
    type: "json.parse",
    label: "JSON Parse",
    description: "String → object",
    icon: <Braces className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { value: "" },
  },
  {
    type: "json.stringify",
    label: "JSON Stringify",
    description: "Object → string",
    icon: <Braces className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { value: "n1.output", pretty: true },
  },
  {
    type: "hash",
    label: "Hash",
    description: "md5 / sha1 / sha256 / sha512",
    icon: <Hash className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { algo: "sha256", value: "" },
  },
  {
    type: "base64",
    label: "Base64",
    description: "Encode / decode",
    icon: <Lock className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { op: "encode", value: "" },
  },
  {
    type: "datetime",
    label: "DateTime",
    description: "now / format / add / diff",
    icon: <Calendar className="size-4 text-zinc-700" />,
    accent: "bg-white border-zinc-200",
    defaultParams: { op: "now" },
  },
  {
    type: "file.read",
    label: "File read",
    description: "Read a workspace file",
    icon: <FileInput className="size-4 text-zinc-600" />,
    accent: "bg-zinc-50 border-zinc-200",
    defaultParams: { path: "" },
  },
  {
    type: "file.write",
    label: "File write",
    description: "Write to a workspace file",
    icon: <FileOutput className="size-4 text-zinc-600" />,
    accent: "bg-zinc-50 border-zinc-200",
    defaultParams: { path: "", content: "", append: false },
  },
  {
    type: "wait_until",
    label: "Wait until",
    description: "Sleep until an ISO timestamp",
    icon: <CalendarClock className="size-4 text-sky-600" />,
    accent: "bg-sky-50/70 border-sky-200",
    defaultParams: { until: "", max_wait_s: 86400 },
  },
  {
    type: "log",
    label: "Log",
    description: "Write to the daemon log",
    icon: <FileText className="size-4 text-zinc-600" />,
    accent: "bg-zinc-50 border-zinc-200",
    defaultParams: { level: "info", message: "" },
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
    type: "notify.sound",
    label: "Sound",
    description: "Play a macOS system sound",
    icon: <Volume2 className="size-4 text-amber-600" />,
    accent: "bg-amber-50/60 border-amber-200",
    defaultParams: { sound: "Ping" },
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
    label: "End - success",
    description: "Halt with success",
    icon: <CheckCircle className="size-4 text-emerald-600" />,
    accent: "bg-emerald-50 border-emerald-300",
    defaultParams: { message: "" },
  },
  {
    type: "end.error",
    label: "End - error",
    description: "Halt with failure",
    icon: <AlertCircle className="size-4 text-red-600" />,
    accent: "bg-red-50 border-red-300",
    defaultParams: { message: "" },
  },
];
