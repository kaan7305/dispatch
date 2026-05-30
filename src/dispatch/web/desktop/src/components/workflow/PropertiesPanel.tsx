import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";

import { api } from "@/lib/api";
import type { WorkflowNode } from "@/lib/workflowApi";
import { Button } from "@/components/ui/button";

import type { InputSchemaEntry } from "./types";

const TOOL_OPTIONS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"] as const;

interface Props {
  node: WorkflowNode | null;
  onChange: (updated: WorkflowNode) => void;
}

export function PropertiesPanel({ node, onChange }: Props) {
  return (
    <aside className="w-72 shrink-0 border-l bg-background flex flex-col">
      <div className="px-4 py-3 border-b">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Properties
        </h2>
        {node && (
          <p className="mt-1 text-[11px] text-muted-foreground truncate">
            {node.type} · {node.id}
          </p>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {!node ? (
          <p className="text-sm text-muted-foreground">
            Select a node to edit its properties
          </p>
        ) : (
          <NodeFields node={node} onChange={onChange} />
        )}
      </div>
    </aside>
  );
}

function NodeFields({ node, onChange }: { node: WorkflowNode; onChange: (n: WorkflowNode) => void }) {
  function setParam<T>(key: string, value: T) {
    onChange({ ...node, params: { ...node.params, [key]: value } });
  }

  switch (node.type) {
    case "trigger.manual":
      return <TriggerFields node={node} setParam={setParam} />;
    case "dispatch":
      return <DispatchFields node={node} setParam={setParam} />;
    case "notify":
      return <NotifyFields node={node} setParam={setParam} />;
    case "wait_reply":
      return <WaitReplyFields node={node} setParam={setParam} />;
    default:
      return (
        <p className="text-sm text-muted-foreground">
          Unknown node type: <code>{node.type}</code>
        </p>
      );
  }
}

// ─── trigger.manual ─────────────────────────────────────────────────────────

function TriggerFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const schema = (node.params.input_schema as InputSchemaEntry[] | undefined) ?? [];

  function updateRow(index: number, patch: Partial<InputSchemaEntry>) {
    const next = schema.map((row, i) => (i === index ? { ...row, ...patch } : row));
    setParam("input_schema", next);
  }
  function addRow() {
    setParam("input_schema", [...schema, { key: "", label: "" }]);
  }
  function removeRow(index: number) {
    setParam("input_schema", schema.filter((_, i) => i !== index));
  }

  return (
    <div className="space-y-4">
      <div>
        <FieldLabel>Input schema</FieldLabel>
        <p className="text-[11px] text-muted-foreground">
          Keys exposed as <code>{"{ctx.foo}"}</code> downstream.
        </p>
      </div>

      <div className="space-y-2">
        {schema.length === 0 && (
          <p className="text-xs text-muted-foreground italic">No inputs yet.</p>
        )}
        {schema.map((row, i) => (
          <div key={i} className="flex items-start gap-1.5">
            <div className="flex-1 space-y-1.5">
              <input
                value={row.key}
                onChange={(e) => updateRow(i, { key: e.target.value })}
                placeholder="key"
                className={inputClass}
              />
              <input
                value={row.label ?? ""}
                onChange={(e) => updateRow(i, { label: e.target.value })}
                placeholder="label (optional)"
                className={inputClass}
              />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => removeRow(i)}
              aria-label="Remove input"
              className="h-8 w-8 shrink-0"
            >
              <X className="size-3.5" />
            </Button>
          </div>
        ))}
      </div>

      <Button type="button" variant="outline" size="sm" onClick={addRow} className="w-full">
        <Plus className="size-3.5" /> Add input
      </Button>
    </div>
  );
}

// ─── dispatch ───────────────────────────────────────────────────────────────

function DispatchFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const recipient = (node.params.recipient_id as string | undefined) ?? "";
  const task = (node.params.task as string | undefined) ?? "";
  const scopes = (node.params.scopes as { tools?: string[] } | undefined) ?? {};
  const tools = scopes.tools ?? [];
  const timeout = (node.params.timeout_s as number | undefined) ?? 3600;

  const trust = useQuery({ queryKey: ["trust"], queryFn: () => api.trust() });
  const peers = useMemo(
    () =>
      trust.data?.trust
        .filter((t) => t.direction === "outgoing")
        .map((t) => t.peer) ?? [],
    [trust.data],
  );

  function toggleTool(tool: string) {
    const next = tools.includes(tool)
      ? tools.filter((t) => t !== tool)
      : [...tools, tool];
    setParam("scopes", { ...scopes, tools: next });
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="recipient_id">Recipient</FieldLabel>
        <input
          id="recipient_id"
          list="dispatch-peers"
          value={recipient}
          onChange={(e) => setParam("recipient_id", e.target.value)}
          placeholder="teammate@example.com"
          className={inputClass}
        />
        <datalist id="dispatch-peers">
          {peers.map((p) => <option key={p} value={p} />)}
        </datalist>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="task">Task</FieldLabel>
        <textarea
          id="task"
          rows={5}
          value={task}
          onChange={(e) => setParam("task", e.target.value)}
          placeholder="What should their agent do?"
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Use <code>{"{ctx.key}"}</code> for trigger inputs.
        </p>
      </div>

      <div className="space-y-1.5">
        <FieldLabel>Allowed tools</FieldLabel>
        <div className="flex flex-wrap gap-1.5">
          {TOOL_OPTIONS.map((tool) => {
            const active = tools.includes(tool);
            return (
              <button
                key={tool}
                type="button"
                onClick={() => toggleTool(tool)}
                className={`rounded-md border px-2 py-1 text-[11px] font-medium transition-colors ${
                  active
                    ? "bg-foreground text-background border-foreground"
                    : "bg-background text-foreground border-input hover:bg-accent"
                }`}
              >
                {tool}
              </button>
            );
          })}
        </div>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="timeout_s">Timeout (seconds)</FieldLabel>
        <input
          id="timeout_s"
          type="number"
          min={60}
          max={86400}
          value={timeout}
          onChange={(e) => setParam("timeout_s", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── notify ────────────────────────────────────────────────────────────────

function NotifyFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const title = (node.params.title as string | undefined) ?? "";
  const body = (node.params.body as string | undefined) ?? "";

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="notify_title">Title</FieldLabel>
        <input
          id="notify_title"
          value={title}
          onChange={(e) => setParam("title", e.target.value)}
          placeholder="Notification title"
          className={inputClass}
        />
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="notify_body">Body</FieldLabel>
        <textarea
          id="notify_body"
          rows={4}
          value={body}
          onChange={(e) => setParam("body", e.target.value)}
          placeholder="Notification body"
          className={`${inputClass} resize-y`}
        />
      </div>
    </div>
  );
}

// ─── wait_reply ────────────────────────────────────────────────────────────

function WaitReplyFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const from = (node.params.from_recipient_id as string | undefined) ?? "";
  const timeout = (node.params.timeout_s as number | undefined) ?? 3600;

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="from_recipient_id">From recipient</FieldLabel>
        <input
          id="from_recipient_id"
          value={from}
          onChange={(e) => setParam("from_recipient_id", e.target.value)}
          placeholder="teammate@example.com"
          className={inputClass}
        />
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="wait_timeout_s">Timeout (seconds)</FieldLabel>
        <input
          id="wait_timeout_s"
          type="number"
          min={60}
          max={86400}
          value={timeout}
          onChange={(e) => setParam("timeout_s", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── shared bits ────────────────────────────────────────────────────────────

const inputClass =
  "w-full rounded-md border bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring";

function FieldLabel({ htmlFor, children }: { htmlFor?: string; children: React.ReactNode }) {
  return (
    <label
      htmlFor={htmlFor}
      className="block text-xs font-medium uppercase tracking-wide text-muted-foreground"
    >
      {children}
    </label>
  );
}
