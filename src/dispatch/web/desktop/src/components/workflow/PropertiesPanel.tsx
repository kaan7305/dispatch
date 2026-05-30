import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";

import type { WorkflowNode } from "@/lib/workflowApi";
import { contexts } from "@/lib/contextApi";
import { Button } from "@/components/ui/button";

import type { InputSchemaEntry } from "./types";

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
    case "context":
      return <ContextFields node={node} setParam={setParam} />;
    case "agent":
      return <AgentFields node={node} setParam={setParam} />;
    case "ai.classify":
      return <AIClassifyFields node={node} setParam={setParam} />;
    case "ai.extract":
      return <AIExtractFields node={node} setParam={setParam} />;
    case "ai.summarize":
      return <AISummarizeFields node={node} setParam={setParam} />;
    case "ai.judge":
      return <AIJudgeFields node={node} setParam={setParam} />;
    case "branch":
      return <BranchFields node={node} setParam={setParam} />;
    case "switch":
      return <SwitchFields node={node} setParam={setParam} />;
    case "filter":
      return <FilterFields node={node} setParam={setParam} />;
    case "merge":
      return <MergeFields />;
    case "set":
      return <SetFields node={node} setParam={setParam} />;
    case "format":
      return <FormatFields node={node} setParam={setParam} />;
    case "log":
      return <LogFields node={node} setParam={setParam} />;
    case "random":
      return <RandomFields node={node} setParam={setParam} />;
    case "math":
      return <MathFields node={node} setParam={setParam} />;
    case "string":
      return <StringFields node={node} setParam={setParam} />;
    case "regex":
      return <RegexFields node={node} setParam={setParam} />;
    case "json.parse":
      return <JsonParseFields node={node} setParam={setParam} />;
    case "json.stringify":
      return <JsonStringifyFields node={node} setParam={setParam} />;
    case "hash":
      return <HashFields node={node} setParam={setParam} />;
    case "base64":
      return <Base64Fields node={node} setParam={setParam} />;
    case "datetime":
      return <DateTimeFields node={node} setParam={setParam} />;
    case "file.read":
      return <FileReadFields node={node} setParam={setParam} />;
    case "file.write":
      return <FileWriteFields node={node} setParam={setParam} />;
    case "wait_until":
      return <WaitUntilFields node={node} setParam={setParam} />;
    case "notify":
      return <NotifyFields node={node} setParam={setParam} />;
    case "notify.sound":
      return <SoundFields node={node} setParam={setParam} />;
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

// ─── context ────────────────────────────────────────────────────────────────

function ContextFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const files =
    (node.params.files as { path?: string; content?: string }[] | undefined) ?? [];
  const systemPrompt = (node.params.system_prompt as string | undefined) ?? "";
  const notes = (node.params.notes as string | undefined) ?? "";

  const library = useQuery({
    queryKey: ["contexts"],
    queryFn: () => contexts.list(),
  });
  const [picking, setPicking] = useState<string>("");

  function updateFile(i: number, patch: { path?: string; content?: string }) {
    setParam("files", files.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));
  }
  function addFile() {
    setParam("files", [...files, { path: "", content: "" }]);
  }
  function removeFile(i: number) {
    setParam("files", files.filter((_, idx) => idx !== i));
  }

  async function loadFromLibrary(contextId: string) {
    if (!contextId) return;
    const hasContent =
      systemPrompt.trim() !== "" || files.length > 0;
    if (hasContent) {
      const ok = confirm(
        "Overwrite the current system prompt and files with the picked context?",
      );
      if (!ok) {
        setPicking("");
        return;
      }
    }
    try {
      const pack = await contexts.get(contextId);
      setParam("system_prompt", pack.system_prompt);
      setParam("files", pack.files);
    } catch (err) {
      alert(`Could not load context: ${err instanceof Error ? err.message : err}`);
    } finally {
      setPicking("");
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="ctx-load">Load from library</FieldLabel>
        <select
          id="ctx-load"
          value={picking}
          onChange={(e) => {
            const v = e.target.value;
            setPicking(v);
            void loadFromLibrary(v);
          }}
          className={inputClass}
        >
          <option value="">
            {(library.data?.contexts ?? []).length === 0
              ? "No saved contexts — create one in the Context section"
              : "Pick a saved context…"}
          </option>
          {(library.data?.contexts ?? []).map((c) => (
            <option key={c.context_id} value={c.context_id}>
              {c.name}
              {c.file_count > 0 ? ` · ${c.file_count} file${c.file_count === 1 ? "" : "s"}` : ""}
            </option>
          ))}
        </select>
        <p className="text-[11px] text-muted-foreground">
          Loads a snapshot — later edits to the saved pack won't change this
          workflow until you re-load.
        </p>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="ctx-system">System prompt</FieldLabel>
        <textarea
          id="ctx-system"
          rows={5}
          value={systemPrompt}
          onChange={(e) => setParam("system_prompt", e.target.value)}
          placeholder="You are an editor for our internal blog. Always …"
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Downstream agents reference this as{" "}
          <code>{`{{${node.id}.output.system_prompt}}`}</code>.
        </p>
      </div>

      <div className="space-y-2">
        <FieldLabel>Files (written to recipient's workspace)</FieldLabel>
        {files.length === 0 && (
          <p className="text-xs text-muted-foreground italic">No files yet.</p>
        )}
        {files.map((f, i) => (
          <div key={i} className="rounded-md border border-zinc-200 p-2 space-y-1.5">
            <div className="flex items-center gap-1.5">
              <input
                value={f.path ?? ""}
                onChange={(e) => updateFile(i, { path: e.target.value })}
                placeholder="path (e.g. CLAUDE.md)"
                className={`${inputClass} font-mono text-xs flex-1`}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => removeFile(i)}
                aria-label="Remove file"
                className="h-8 w-8 shrink-0"
              >
                <X className="size-3.5" />
              </Button>
            </div>
            <textarea
              rows={4}
              value={f.content ?? ""}
              onChange={(e) => updateFile(i, { content: e.target.value })}
              placeholder="contents (templatable)"
              className={`${inputClass} font-mono text-xs resize-y`}
            />
          </div>
        ))}
        <Button type="button" variant="outline" size="sm" onClick={addFile} className="w-full">
          <Plus className="size-3.5" /> Add file
        </Button>
        <p className="text-[11px] text-muted-foreground">
          Paths are workspace-relative; escapes outside the workspace are
          rejected. Each recipient's daemon writes them locally before the
          downstream agent runs.
        </p>
      </div>

      <div className="space-y-1.5">
        <FieldLabel htmlFor="ctx-notes">Notes (recipient-visible)</FieldLabel>
        <textarea
          id="ctx-notes"
          rows={3}
          value={notes}
          onChange={(e) => setParam("notes", e.target.value)}
          placeholder="Why I'm sending this context…"
          className={`${inputClass} resize-y`}
        />
      </div>
    </div>
  );
}

// ─── agent ──────────────────────────────────────────────────────────────────

function AgentFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const prompt = (node.params.prompt as string | undefined) ?? "";
  const systemPrompt = (node.params.system_prompt as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="agent-prompt">Prompt</FieldLabel>
        <textarea
          id="agent-prompt"
          rows={7}
          value={prompt}
          onChange={(e) => setParam("prompt", e.target.value)}
          placeholder="What should the recipient's agent do?"
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Runs on the recipient's machine using their tool scopes from the
          trust edge. Reference earlier node outputs with{" "}
          <code>{"{{nN.output}}"}</code> and trigger inputs with{" "}
          <code>{"{{ctx.key}}"}</code>.
        </p>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="agent-system">System prompt (optional)</FieldLabel>
        <textarea
          id="agent-system"
          rows={4}
          value={systemPrompt}
          onChange={(e) => setParam("system_prompt", e.target.value)}
          placeholder="{{ctxNode.output.system_prompt}}  or  literal instructions"
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Sent to Claude as the system message. Often references a{" "}
          <code>Context</code> node upstream.
        </p>
      </div>
    </div>
  );
}

// ─── switch ────────────────────────────────────────────────────────────────

function SwitchFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const value = (node.params.value as string | undefined) ?? "";
  const cases =
    (node.params.cases as { when?: string; port?: string }[] | undefined) ?? [];
  const defaultPort =
    (node.params.default_port as string | undefined) ?? "out_default";

  function updateCase(i: number, patch: { when?: string; port?: string }) {
    const next = cases.map((c, idx) => (idx === i ? { ...c, ...patch } : c));
    setParam("cases", next);
  }
  function addCase() {
    setParam("cases", [...cases, { when: "", port: `out_${cases.length + 1}` }]);
  }
  function removeCase(i: number) {
    setParam("cases", cases.filter((_, idx) => idx !== i));
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="switch-value">Value</FieldLabel>
        <input
          id="switch-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
      <div className="space-y-2">
        <FieldLabel>Cases</FieldLabel>
        {cases.length === 0 && (
          <p className="text-xs text-muted-foreground italic">No cases yet.</p>
        )}
        {cases.map((c, i) => (
          <div key={i} className="flex items-start gap-1.5">
            <div className="flex-1 space-y-1.5">
              <input
                value={c.when ?? ""}
                onChange={(e) => updateCase(i, { when: e.target.value })}
                placeholder="when equals…"
                className={inputClass}
              />
              <input
                value={c.port ?? ""}
                onChange={(e) => updateCase(i, { port: e.target.value })}
                placeholder="port id (e.g. out_yes)"
                className={`${inputClass} font-mono text-xs`}
              />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => removeCase(i)}
              aria-label="Remove case"
              className="h-8 w-8 shrink-0"
            >
              <X className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button type="button" variant="outline" size="sm" onClick={addCase} className="w-full">
          <Plus className="size-3.5" /> Add case
        </Button>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="switch-default">Default port</FieldLabel>
        <input
          id="switch-default"
          value={defaultPort}
          onChange={(e) => setParam("default_port", e.target.value)}
          placeholder="out_default"
          className={`${inputClass} font-mono text-xs`}
        />
      </div>
    </div>
  );
}

// ─── set ───────────────────────────────────────────────────────────────────

function SetFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const values =
    (node.params.values as Record<string, string> | undefined) ?? {};
  const entries = Object.entries(values);

  function updateKey(oldKey: string, newKey: string) {
    const next: Record<string, string> = {};
    for (const [k, v] of entries) {
      next[k === oldKey ? newKey : k] = v;
    }
    setParam("values", next);
  }
  function updateValue(key: string, value: string) {
    setParam("values", { ...values, [key]: value });
  }
  function addRow() {
    let i = 1;
    while (`var${i}` in values) i++;
    setParam("values", { ...values, [`var${i}`]: "" });
  }
  function removeRow(key: string) {
    const next = { ...values };
    delete next[key];
    setParam("values", next);
  }

  return (
    <div className="space-y-3">
      <FieldLabel>Variables</FieldLabel>
      {entries.length === 0 && (
        <p className="text-xs text-muted-foreground italic">No variables yet.</p>
      )}
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-start gap-1.5">
          <div className="flex-1 space-y-1.5">
            <input
              value={k}
              onChange={(e) => updateKey(k, e.target.value)}
              placeholder="name"
              className={`${inputClass} font-mono text-xs`}
            />
            <input
              value={v}
              onChange={(e) => updateValue(k, e.target.value)}
              placeholder="value or {{template}}"
              className={inputClass}
            />
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => removeRow(k)}
            aria-label="Remove variable"
            className="h-8 w-8 shrink-0"
          >
            <X className="size-3.5" />
          </Button>
        </div>
      ))}
      <Button type="button" variant="outline" size="sm" onClick={addRow} className="w-full">
        <Plus className="size-3.5" /> Add variable
      </Button>
      <p className="text-[11px] text-muted-foreground">
        Downstream nodes read with{" "}
        <code>{`{{${node.id}.output.name}}`}</code>.
      </p>
    </div>
  );
}

// ─── format ────────────────────────────────────────────────────────────────

function FormatFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const template = (node.params.template as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="format-template">Template</FieldLabel>
        <textarea
          id="format-template"
          rows={5}
          value={template}
          onChange={(e) => setParam("template", e.target.value)}
          placeholder={"Hello {{ctx.name}}, n1 said: {{n1.output}}"}
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Output is the hydrated string. Supports{" "}
          <code>{"{{ctx.key}}"}</code> and{" "}
          <code>{"{{nN.output[.key]}}"}</code>.
        </p>
      </div>
    </div>
  );
}

// ─── log ───────────────────────────────────────────────────────────────────

function LogFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const level = (node.params.level as string | undefined) ?? "info";
  const message = (node.params.message as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="log-level">Level</FieldLabel>
        <select
          id="log-level"
          value={level}
          onChange={(e) => setParam("level", e.target.value)}
          className={inputClass}
        >
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="log-message">Message</FieldLabel>
        <textarea
          id="log-message"
          rows={4}
          value={message}
          onChange={(e) => setParam("message", e.target.value)}
          placeholder="Step done: {{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
    </div>
  );
}

// ─── random ────────────────────────────────────────────────────────────────

function RandomFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const kind = (node.params.kind as string | undefined) ?? "uuid";
  const min = (node.params.min as number | undefined) ?? 0;
  const max = (node.params.max as number | undefined) ?? (kind === "float" ? 1 : 100);
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="random-kind">Kind</FieldLabel>
        <select
          id="random-kind"
          value={kind}
          onChange={(e) => setParam("kind", e.target.value)}
          className={inputClass}
        >
          <option value="uuid">uuid</option>
          <option value="int">int</option>
          <option value="float">float</option>
        </select>
      </div>
      {kind !== "uuid" && (
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <FieldLabel htmlFor="random-min">Min</FieldLabel>
            <input
              id="random-min"
              type="number"
              value={min}
              onChange={(e) => setParam("min", Number(e.target.value))}
              className={inputClass}
            />
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="random-max">Max</FieldLabel>
            <input
              id="random-max"
              type="number"
              value={max}
              onChange={(e) => setParam("max", Number(e.target.value))}
              className={inputClass}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── wait_until ────────────────────────────────────────────────────────────

function WaitUntilFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const until = (node.params.until as string | undefined) ?? "";
  const max = (node.params.max_wait_s as number | undefined) ?? 86400;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="wait-until">Until (ISO 8601)</FieldLabel>
        <input
          id="wait-until"
          value={until}
          onChange={(e) => setParam("until", e.target.value)}
          placeholder="2026-06-01T09:00:00Z"
          className={`${inputClass} font-mono text-xs`}
        />
        <p className="text-[11px] text-muted-foreground">
          Supports <code>{"{{ctx.key}}"}</code>; capped at one week.
        </p>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="wait-max">Max wait (seconds)</FieldLabel>
        <input
          id="wait-max"
          type="number"
          min={1}
          max={604800}
          value={max}
          onChange={(e) => setParam("max_wait_s", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── filter ────────────────────────────────────────────────────────────────

function FilterFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const left  = (node.params.left as string | undefined) ?? "";
  const op    = (node.params.op as string | undefined) ?? "==";
  const right = (node.params.right as string | undefined) ?? "";
  const needsRight = !["is_empty", "is_not_empty"].includes(op);
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="filter-left">Left</FieldLabel>
        <input
          id="filter-left"
          value={left}
          onChange={(e) => setParam("left", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="filter-op">Operator</FieldLabel>
        <select
          id="filter-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="==">equals (==)</option>
          <option value="!=">not equals (!=)</option>
          <option value="contains">contains</option>
          <option value="starts_with">starts with</option>
          <option value="ends_with">ends with</option>
          <option value="is_empty">is empty</option>
          <option value="is_not_empty">is not empty</option>
        </select>
      </div>
      {needsRight && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="filter-right">Right</FieldLabel>
          <input
            id="filter-right"
            value={right}
            onChange={(e) => setParam("right", e.target.value)}
            className={inputClass}
          />
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        If false, every downstream node is skipped.
      </p>
    </div>
  );
}

// ─── merge ─────────────────────────────────────────────────────────────────

function MergeFields() {
  return (
    <p className="text-xs text-muted-foreground">
      No configuration. Output is a dict of{" "}
      <code>{"{sourceNodeId: output}"}</code> for every incoming edge that
      reached this node.
    </p>
  );
}

// ─── math ──────────────────────────────────────────────────────────────────

function MathFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const op    = (node.params.op as string | undefined) ?? "+";
  const left  = (node.params.left as string | undefined) ?? "";
  const right = (node.params.right as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="math-op">Operator</FieldLabel>
        <select
          id="math-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="+">+ (add)</option>
          <option value="-">− (subtract)</option>
          <option value="*">× (multiply)</option>
          <option value="/">÷ (divide)</option>
          <option value="%">% (modulo)</option>
          <option value="**">** (power)</option>
          <option value="min">min</option>
          <option value="max">max</option>
        </select>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="math-left">Left</FieldLabel>
          <input
            id="math-left"
            value={left}
            onChange={(e) => setParam("left", e.target.value)}
            className={inputClass}
          />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="math-right">Right</FieldLabel>
          <input
            id="math-right"
            value={right}
            onChange={(e) => setParam("right", e.target.value)}
            className={inputClass}
          />
        </div>
      </div>
    </div>
  );
}

// ─── string ────────────────────────────────────────────────────────────────

function StringFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const op       = (node.params.op as string | undefined) ?? "trim";
  const value    = (node.params.value as string | undefined) ?? "";
  const start    = (node.params.start as number | undefined) ?? 0;
  const end      = (node.params.end as number | undefined) ?? 0;
  const find     = (node.params.find as string | undefined) ?? "";
  const replace  = (node.params.replace as string | undefined) ?? "";
  const separator= (node.params.separator as string | undefined) ?? ",";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="str-op">Operation</FieldLabel>
        <select
          id="str-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="upper">upper</option>
          <option value="lower">lower</option>
          <option value="title">title</option>
          <option value="trim">trim</option>
          <option value="reverse">reverse</option>
          <option value="length">length</option>
          <option value="slice">slice</option>
          <option value="replace">replace</option>
          <option value="split">split</option>
          <option value="join">join</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="str-value">Value</FieldLabel>
        <input
          id="str-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
      {op === "slice" && (
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <FieldLabel htmlFor="str-start">Start</FieldLabel>
            <input
              id="str-start"
              type="number"
              value={start}
              onChange={(e) => setParam("start", Number(e.target.value))}
              className={inputClass}
            />
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="str-end">End</FieldLabel>
            <input
              id="str-end"
              type="number"
              value={end}
              onChange={(e) => setParam("end", Number(e.target.value))}
              className={inputClass}
            />
          </div>
        </div>
      )}
      {op === "replace" && (
        <>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="str-find">Find</FieldLabel>
            <input
              id="str-find"
              value={find}
              onChange={(e) => setParam("find", e.target.value)}
              className={inputClass}
            />
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="str-repl">Replace with</FieldLabel>
            <input
              id="str-repl"
              value={replace}
              onChange={(e) => setParam("replace", e.target.value)}
              className={inputClass}
            />
          </div>
        </>
      )}
      {(op === "split" || op === "join") && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="str-sep">Separator</FieldLabel>
          <input
            id="str-sep"
            value={separator}
            onChange={(e) => setParam("separator", e.target.value)}
            className={`${inputClass} font-mono text-xs`}
          />
        </div>
      )}
    </div>
  );
}

// ─── regex ─────────────────────────────────────────────────────────────────

function RegexFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const op      = (node.params.op as string | undefined) ?? "extract";
  const pattern = (node.params.pattern as string | undefined) ?? "";
  const value   = (node.params.value as string | undefined) ?? "";
  const replace = (node.params.replace as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="re-op">Operation</FieldLabel>
        <select
          id="re-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="extract">extract (first match)</option>
          <option value="extract_all">extract all</option>
          <option value="replace">replace</option>
          <option value="test">test (boolean)</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="re-pattern">Pattern</FieldLabel>
        <input
          id="re-pattern"
          value={pattern}
          onChange={(e) => setParam("pattern", e.target.value)}
          placeholder="\d+"
          className={`${inputClass} font-mono text-xs`}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="re-value">Value</FieldLabel>
        <input
          id="re-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
      {op === "replace" && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="re-repl">Replace with</FieldLabel>
          <input
            id="re-repl"
            value={replace}
            onChange={(e) => setParam("replace", e.target.value)}
            className={inputClass}
          />
        </div>
      )}
    </div>
  );
}

// ─── json.parse / json.stringify ───────────────────────────────────────────

function JsonParseFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const value = (node.params.value as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="jp-value">JSON string</FieldLabel>
        <textarea
          id="jp-value"
          rows={5}
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} font-mono text-xs resize-y`}
        />
      </div>
    </div>
  );
}

function JsonStringifyFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const value  = (node.params.value as string | undefined) ?? "";
  const pretty = (node.params.pretty as boolean | undefined) ?? false;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="js-value">Source ref</FieldLabel>
        <input
          id="js-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="n1.output  or  ctx.user"
          className={`${inputClass} font-mono text-xs`}
        />
        <p className="text-[11px] text-muted-foreground">
          Path to the structured value to serialize. Use{" "}
          <code>nN.output[.key]</code> or <code>ctx.key</code>.
        </p>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={pretty}
          onChange={(e) => setParam("pretty", e.target.checked)}
        />
        Pretty-print (2-space indent)
      </label>
    </div>
  );
}

// ─── hash / base64 ─────────────────────────────────────────────────────────

function HashFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const algo  = (node.params.algo as string | undefined) ?? "sha256";
  const value = (node.params.value as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="hash-algo">Algorithm</FieldLabel>
        <select
          id="hash-algo"
          value={algo}
          onChange={(e) => setParam("algo", e.target.value)}
          className={inputClass}
        >
          <option value="md5">md5</option>
          <option value="sha1">sha1</option>
          <option value="sha256">sha256</option>
          <option value="sha512">sha512</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="hash-value">Value</FieldLabel>
        <input
          id="hash-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          placeholder="{{n1.output}}"
          className={inputClass}
        />
      </div>
    </div>
  );
}

function Base64Fields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const op    = (node.params.op as string | undefined) ?? "encode";
  const value = (node.params.value as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="b64-op">Operation</FieldLabel>
        <select
          id="b64-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="encode">encode</option>
          <option value="decode">decode</option>
        </select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="b64-value">Value</FieldLabel>
        <input
          id="b64-value"
          value={value}
          onChange={(e) => setParam("value", e.target.value)}
          className={inputClass}
        />
      </div>
    </div>
  );
}

// ─── datetime ──────────────────────────────────────────────────────────────

function DateTimeFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const op      = (node.params.op as string | undefined) ?? "now";
  const value   = (node.params.value as string | undefined) ?? "";
  const format  = (node.params.format as string | undefined) ?? "%Y-%m-%d %H:%M:%S";
  const seconds = (node.params.seconds as number | undefined) ?? 0;
  const a       = (node.params.a as string | undefined) ?? "";
  const b       = (node.params.b as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="dt-op">Operation</FieldLabel>
        <select
          id="dt-op"
          value={op}
          onChange={(e) => setParam("op", e.target.value)}
          className={inputClass}
        >
          <option value="now">now</option>
          <option value="format">format</option>
          <option value="add">add seconds</option>
          <option value="diff">diff (a − b)</option>
        </select>
      </div>
      {(op === "format" || op === "add") && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="dt-value">Source ISO datetime</FieldLabel>
          <input
            id="dt-value"
            value={value}
            onChange={(e) => setParam("value", e.target.value)}
            placeholder="{{n1.output.iso}}"
            className={`${inputClass} font-mono text-xs`}
          />
        </div>
      )}
      {op === "format" && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="dt-format">strftime format</FieldLabel>
          <input
            id="dt-format"
            value={format}
            onChange={(e) => setParam("format", e.target.value)}
            className={`${inputClass} font-mono text-xs`}
          />
        </div>
      )}
      {op === "add" && (
        <div className="space-y-1.5">
          <FieldLabel htmlFor="dt-seconds">Seconds to add</FieldLabel>
          <input
            id="dt-seconds"
            type="number"
            value={seconds}
            onChange={(e) => setParam("seconds", Number(e.target.value))}
            className={inputClass}
          />
        </div>
      )}
      {op === "diff" && (
        <>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="dt-a">A (ISO)</FieldLabel>
            <input
              id="dt-a"
              value={a}
              onChange={(e) => setParam("a", e.target.value)}
              className={`${inputClass} font-mono text-xs`}
            />
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor="dt-b">B (ISO)</FieldLabel>
            <input
              id="dt-b"
              value={b}
              onChange={(e) => setParam("b", e.target.value)}
              className={`${inputClass} font-mono text-xs`}
            />
          </div>
        </>
      )}
    </div>
  );
}

// ─── file.read / file.write ────────────────────────────────────────────────

function FileReadFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const path = (node.params.path as string | undefined) ?? "";
  const max  = (node.params.max_bytes as number | undefined) ?? 262144;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="fr-path">Path (workspace-relative)</FieldLabel>
        <input
          id="fr-path"
          value={path}
          onChange={(e) => setParam("path", e.target.value)}
          placeholder="notes/today.md"
          className={`${inputClass} font-mono text-xs`}
        />
        <p className="text-[11px] text-muted-foreground">
          Restricted to the recipient's workspace dir; paths escaping it are
          rejected.
        </p>
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="fr-max">Max bytes</FieldLabel>
        <input
          id="fr-max"
          type="number"
          min={1}
          max={10 * 1024 * 1024}
          value={max}
          onChange={(e) => setParam("max_bytes", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

function FileWriteFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const path    = (node.params.path as string | undefined) ?? "";
  const content = (node.params.content as string | undefined) ?? "";
  const append  = (node.params.append as boolean | undefined) ?? false;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="fw-path">Path (workspace-relative)</FieldLabel>
        <input
          id="fw-path"
          value={path}
          onChange={(e) => setParam("path", e.target.value)}
          placeholder="outputs/result.json"
          className={`${inputClass} font-mono text-xs`}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="fw-content">Content</FieldLabel>
        <textarea
          id="fw-content"
          rows={6}
          value={content}
          onChange={(e) => setParam("content", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={append}
          onChange={(e) => setParam("append", e.target.checked)}
        />
        Append (otherwise overwrite)
      </label>
    </div>
  );
}

// ─── AI nodes ──────────────────────────────────────────────────────────────

function AIClassifyFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const input = (node.params.input as string | undefined) ?? "";
  const cats  = (node.params.categories as string[] | undefined) ?? [];
  function update(i: number, v: string) {
    setParam("categories", cats.map((c, idx) => (idx === i ? v : c)));
  }
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="cls-input">Input</FieldLabel>
        <textarea
          id="cls-input"
          rows={4}
          value={input}
          onChange={(e) => setParam("input", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
      <div className="space-y-2">
        <FieldLabel>Categories</FieldLabel>
        {cats.map((c, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <input
              value={c}
              onChange={(e) => update(i, e.target.value)}
              className={inputClass}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => setParam("categories", cats.filter((_, idx) => idx !== i))}
              aria-label="Remove category"
              className="h-8 w-8 shrink-0"
            >
              <X className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setParam("categories", [...cats, ""])}
          className="w-full"
        >
          <Plus className="size-3.5" /> Add category
        </Button>
      </div>
    </div>
  );
}

function AIExtractFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const input = (node.params.input as string | undefined) ?? "";
  const schema = node.params.schema;
  const initial =
    typeof schema === "string"
      ? schema
      : schema == null
        ? ""
        : JSON.stringify(schema, null, 2);
  const [text, setText] = useState<string>(initial);
  const [err, setErr] = useState<string | null>(null);
  function onChange(value: string) {
    setText(value);
    if (value.trim() === "") {
      setErr(null);
      setParam("schema", {});
      return;
    }
    try {
      setParam("schema", JSON.parse(value));
      setErr(null);
    } catch {
      setErr("Invalid JSON");
      setParam("schema", value);
    }
  }
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="ext-input">Input</FieldLabel>
        <textarea
          id="ext-input"
          rows={4}
          value={input}
          onChange={(e) => setParam("input", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="ext-schema">Schema (JSON)</FieldLabel>
        <textarea
          id="ext-schema"
          rows={6}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          placeholder='{"name":"string","email":"string"}'
          className={`${inputClass} font-mono text-xs resize-y`}
        />
        {err && <p className="text-[11px] text-destructive">{err}</p>}
      </div>
    </div>
  );
}

function AISummarizeFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const input = (node.params.input as string | undefined) ?? "";
  const words = (node.params.max_words as number | undefined) ?? 50;
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="sum-input">Input</FieldLabel>
        <textarea
          id="sum-input"
          rows={5}
          value={input}
          onChange={(e) => setParam("input", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="sum-words">Max words</FieldLabel>
        <input
          id="sum-words"
          type="number"
          min={5}
          max={1000}
          value={words}
          onChange={(e) => setParam("max_words", Number(e.target.value))}
          className={inputClass}
        />
      </div>
    </div>
  );
}

function AIJudgeFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const input     = (node.params.input as string | undefined) ?? "";
  const criterion = (node.params.criterion as string | undefined) ?? "";
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="judge-input">Input</FieldLabel>
        <textarea
          id="judge-input"
          rows={4}
          value={input}
          onChange={(e) => setParam("input", e.target.value)}
          placeholder="{{n1.output}}"
          className={`${inputClass} resize-y`}
        />
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor="judge-criterion">Criterion</FieldLabel>
        <textarea
          id="judge-criterion"
          rows={3}
          value={criterion}
          onChange={(e) => setParam("criterion", e.target.value)}
          placeholder="Clarity, factual accuracy, tone…"
          className={`${inputClass} resize-y`}
        />
        <p className="text-[11px] text-muted-foreground">
          Output: <code>{"{score: 1-10, reason, raw}"}</code>.
        </p>
      </div>
    </div>
  );
}

// ─── notify.sound ──────────────────────────────────────────────────────────

function SoundFields({
  node, setParam,
}: { node: WorkflowNode; setParam: <T>(k: string, v: T) => void }) {
  const sound = (node.params.sound as string | undefined) ?? "Ping";
  const SOUNDS = [
    "Basso", "Blow", "Bottle", "Frog", "Funk", "Glass", "Hero",
    "Morse", "Ping", "Pop", "Purr", "Sosumi", "Submarine", "Tink",
  ];
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <FieldLabel htmlFor="snd-name">Sound</FieldLabel>
        <select
          id="snd-name"
          value={sound}
          onChange={(e) => setParam("sound", e.target.value)}
          className={inputClass}
        >
          {SOUNDS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <p className="text-[11px] text-muted-foreground">
          macOS system sounds in <code>/System/Library/Sounds</code>.
        </p>
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
