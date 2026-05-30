import { useMemo, useState } from "react";
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
  onDelete?: () => void;
}

export function PropertiesPanel({ node, onChange, onDelete }: Props) {
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

      {node && onDelete && (
        <div className="border-t px-4 py-3">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onDelete}
            className="w-full text-destructive hover:text-destructive"
          >
            Delete node
          </Button>
        </div>
      )}
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
    case "dispatch.multi":
      return <MultiDispatchFields node={node} setParam={setParam} />;
    case "branch":
      return <BranchFields node={node} setParam={setParam} />;
    case "notify":
      return <NotifyFields node={node} setParam={setParam} />;
    case "wait_reply":
      return <WaitReplyFields node={node} setParam={setParam} />;
    case "trigger.cron":
      return <CronTriggerFields node={node} setParam={setParam} />;
    case "transform.code":
      return <CodeFields node={node} setParam={setParam} />;
    case "http.request":
      return <HTTPFields node={node} setParam={setParam} />;
    case "delay":
      return <DelayFields node={node} setParam={setParam} />;
    case "end.success":
      return <EndFields node={node} setParam={setParam} kind="success" />;
    case "end.error":
      return <EndFields node={node} setParam={setParam} kind="error" />;
    default:
      return (
        <p className="text-sm text-muted-foreground">
          Unknown node type: <code>{node.type}</code>
        </p>
      );
  }
}

// ─── branch ────────────────────────────────────────────────────────────────

function BranchFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const left  = (node.params.left  as string | undefined) ?? "";
  const op    = (node.params.op    as string | undefined) ?? "==";
  const right = (node.params.right as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="branch-left">Left</FieldLabel>
        <input
          id="branch-left"
          value={left}
          onChange={(e) => setParam("left", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="branch-op">Operator</FieldLabel>
        <select
          id="branch-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="==">equals (==)</option>
          <option value="!=">not equals (!=)</option>
          <option value="contains">contains</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="branch-right">Right</FieldLabel>
        <input
          id="branch-right"
          value={right}
          onChange={(e) => setParam("right", e.target.value)}
          placeholder="approved"
          className={inputClass}
        />
        <p className="text-[11px] text-muted-foreground">
          Both sides accept <code>{"{{ctx.key}}"}</code> and{" "}
          <code>{"{{nN.output}}"}</code>.
        </p>
      </div>
    </div>
  );
}

// ─── dispatch.multi (fan-out) ─────────────────────────────────────────────

function MultiDispatchFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const ids = (node.params.recipient_ids as string[] | undefined) ?? [];
  const task = (node.params.task as string | undefined) ?? "";
  const timeout = (node.params.timeout_s as number | undefined) ?? 3600;

  const trust = useQuery({ queryKey: ["trust"], queryFn: () => api.trust() });
  const peers = useMemo(
    () =>
      trust.data?.trust
        .filter((t) => t.direction === "outgoing")
        .map((t) => t.peer)
        .filter((p) => !ids.includes(p)) ?? [],
    [trust.data, ids],
  );

  function add(value: string) {
    const v = value.trim().toLowerCase();
    if (!v || ids.includes(v)) return;
    setParam("recipient_ids", [...ids, v]);
  }
  function remove(value: string) {
    setParam("recipient_ids", ids.filter((id) => id !== value));
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel>Recipients</FieldLabel>
        <div className="flex flex-wrap items-center gap-1 rounded-md border bg-background px-2 py-1.5 min-h-[36px]">
          {ids.map((id) => (
            <span
              key={id}
              className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium"
            >
              {id}
              <button
                type="button"
                onClick={() => remove(id)}
                className="text-muted-foreground hover:text-foreground"
                aria-label={`Remove ${id}`}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          <input
            list="multi-peers"
            placeholder={ids.length === 0 ? "teammate@example.com" : "+ add"}
            className="flex-1 min-w-[100px] bg-transparent px-1 py-0.5 text-xs focus:outline-none"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === ",") {
                e.preventDefault();
                add((e.target as HTMLInputElement).value);
                (e.target as HTMLInputElement).value = "";
              }
            }}
            onBlur={(e) => {
              if (e.target.value) {
                add(e.target.value);
                e.target.value = "";
              }
            }}
          />
          <datalist id="multi-peers">
            {peers.map((p) => <option key={p} value={p} />)}
          </datalist>
        </div>
        <p className="text-[11px] text-muted-foreground">Enter or comma to add.</p>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="multi-task">Task</FieldLabel>
        <textarea
          id="multi-task"
          rows={5}
          value={task}
          onChange={(e) => setParam("task", e.target.value)}
          placeholder="Same task sent to every recipient"
          className={`${inputClass} resize-y`}
        />
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="multi-timeout_s">Timeout (seconds)</FieldLabel>
        <input
          id="multi-timeout_s"
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

// ─── trigger.cron ──────────────────────────────────────────────────────────

function CronTriggerFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const expression = (node.params.expression as string | undefined) ?? "";
  const input = node.params.input;
  const initialText =
    typeof input === "string"
      ? input
      : input == null
        ? ""
        : JSON.stringify(input, null, 2);
  const [inputText, setInputText] = useState<string>(initialText);
  const [inputError, setInputError] = useState<string | null>(null);

  function onInputChange(value: string) {
    setInputText(value);
    if (value.trim() === "") {
      setInputError(null);
      setParam("input", {});
      return;
    }
    try {
      const parsed = JSON.parse(value);
      setInputError(null);
      setParam("input", parsed);
    } catch {
      setInputError("Invalid JSON");
      setParam("input", value);
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="cron-expression">Expression</FieldLabel>
        <input
          id="cron-expression"
          value={expression}
          onChange={(e) => setParam("expression", e.target.value)}
          placeholder="0 9 * * *"
          className={`${inputClass} font-mono`}
        />
        <p className="text-[11px] text-muted-foreground">
          5 fields: minute, hour, day, month, weekday. Examples:{" "}
          <code>0 9 * * *</code> = 9am daily, <code>*/15 * * * *</code> = every
          15 min
        </p>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="cron-input">Static input (JSON)</FieldLabel>
        <textarea
          id="cron-input"
          rows={5}
          value={inputText}
          onChange={(e) => onInputChange(e.target.value)}
          placeholder='{"key": "value"}'
          className={`${inputClass} font-mono text-xs resize-y`}
        />
        {inputError && (
          <p className="text-[11px] text-destructive">{inputError}</p>
        )}
      </div>
    </div>
  );
}

// ─── transform.code ────────────────────────────────────────────────────────

function CodeFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const code = (node.params.code as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="code-expr">Expression</FieldLabel>
        <textarea
          id="code-expr"
          rows={6}
          value={code}
          onChange={(e) => setParam("code", e.target.value)}
          placeholder="ctx.foo + 1  // or  json.loads(n2.output)['key']"
          className={`${inputClass} font-mono text-xs resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Available: <code>ctx</code> (inputs), <code>nN</code> (prior node
          outputs), <code>json</code>, <code>math</code>, <code>len</code>,{" "}
          <code>str</code>, <code>int</code>, <code>float</code>,{" "}
          <code>dict</code>, <code>list</code>, <code>sum</code>,{" "}
          <code>min</code>, <code>max</code>, <code>sorted</code>,{" "}
          <code>any</code>, <code>all</code>. Return any value.
        </p>
      </div>
    </div>
  );
}

// ─── http.request ──────────────────────────────────────────────────────────

function HTTPFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const method = (node.params.method as string | undefined) ?? "GET";
  const url = (node.params.url as string | undefined) ?? "";
  const body = (node.params.body as string | undefined) ?? "";
  const timeout = (node.params.timeout_s as number | undefined) ?? 30;
  const headers = node.params.headers;
  const initialHeadersText =
    typeof headers === "string"
      ? headers
      : headers == null
        ? ""
        : JSON.stringify(headers, null, 2);
  const [headersText, setHeadersText] = useState<string>(initialHeadersText);
  const [headersError, setHeadersError] = useState<string | null>(null);

  function onHeadersChange(value: string) {
    setHeadersText(value);
    if (value.trim() === "") {
      setHeadersError(null);
      setParam("headers", {});
      return;
    }
    try {
      const parsed = JSON.parse(value);
      setHeadersError(null);
      setParam("headers", parsed);
    } catch {
      setHeadersError("Invalid JSON");
      setParam("headers", value);
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="http-method">Method</FieldLabel>
        <select
          id="http-method"
          value={method}
          onChange={(e) => setParam("method", e.target.value)}
          className={inputClass}
        >
          <option value="GET">GET</option>
          <option value="POST">POST</option>
          <option value="PUT">PUT</option>
          <option value="PATCH">PATCH</option>
          <option value="DELETE">DELETE</option>
        </select>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="http-url">URL</FieldLabel>
        <input
          id="http-url"
          value={url}
          onChange={(e) => setParam("url", e.target.value)}
          placeholder="https://example.com/api"
          className={inputClass}
        />
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="http-headers">Headers (JSON)</FieldLabel>
        <textarea
          id="http-headers"
          rows={4}
          value={headersText}
          onChange={(e) => onHeadersChange(e.target.value)}
          placeholder='{"Authorization": "Bearer ..."}'
          className={`${inputClass} font-mono text-xs resize-y`}
        />
        {headersError && (
          <p className="text-[11px] text-destructive">{headersError}</p>
        )}
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="http-body">Body</FieldLabel>
        <textarea
          id="http-body"
          rows={4}
          value={body}
          onChange={(e) => setParam("body", e.target.value)}
          placeholder="request body (templatable)"
          className={`${inputClass} resize-y`}
        />
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="http-timeout">Timeout (seconds)</FieldLabel>
        <input
          id="http-timeout"
          type="number"
          min={1}
          max={300}
          value={timeout}
          onChange={(e) => setParam("timeout_s", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── delay ─────────────────────────────────────────────────────────────────

function DelayFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const seconds = (node.params.seconds as number | undefined) ?? 5;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="delay-seconds">Seconds</FieldLabel>
        <input
          id="delay-seconds"
          type="number"
          min={1}
          max={3600}
          value={seconds}
          onChange={(e) => setParam("seconds", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── end.success / end.error ───────────────────────────────────────────────

function EndFields({
  node, setParam, kind,
}: {
  node: WorkflowNode;
  setParam: <T>(k: string, v: T) => void;
  kind: "success" | "error";
}) {
  const message = (node.params.message as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="end-message">
          Message{kind === "error" ? " (error)" : ""}
        </FieldLabel>
        <input
          id="end-message"
          value={message}
          onChange={(e) => setParam("message", e.target.value)}
          placeholder="Workflow done"
          className={inputClass}
        />
        <p className="text-[11px] text-muted-foreground">
          Optional. Supports <code>{"{{ctx.key}}"}</code> and{" "}
          <code>{"{{nN.output}}"}</code>.
        </p>
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
