import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
  type Connection,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Save, Play, ArrowLeft, Trash2 } from "lucide-react";

import {
  workflows,
  type Workflow,
  type WorkflowDefinition,
  type WorkflowNode,
  type WorkflowEdge,
} from "@/lib/workflowApi";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { NODE_TYPES, PALETTE, type PaletteItem } from "@/components/workflow/nodes";

const DRAG_MIME = "application/x-dispatch-node";

export default function WorkflowEditor() {
  return (
    <ReactFlowProvider>
      <WorkflowEditorInner />
    </ReactFlowProvider>
  );
}

function WorkflowEditorInner() {
  const { id } = useParams<{ id: string }>();
  const isNew = !id || id === "new";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const reactFlow = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement>(null);

  const existing = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => workflows.get(id!),
    enabled: !isNew,
  });

  const [name, setName] = useState("Untitled workflow");
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [runDialog, setRunDialog] = useState(false);

  // Load existing workflow → seed canvas state.
  useEffect(() => {
    if (!existing.data) return;
    setName(existing.data.name);
    const { nodes: rfNodes, edges: rfEdges } = defToReactFlow(existing.data);
    setNodes(rfNodes);
    setEdges(rfEdges);
  }, [existing.data, setNodes, setEdges]);

  const save = useMutation({
    mutationFn: async () => {
      const definition = reactFlowToDef(nodes, edges);
      if (isNew) {
        const res = await workflows.create({ name, definition });
        return res.workflow_id;
      }
      await workflows.update(id!, { name, definition });
      return id!;
    },
    onSuccess: (workflowId) => {
      qc.invalidateQueries({ queryKey: ["workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow", workflowId] });
      if (isNew) navigate(`/workflows/${workflowId}/edit`, { replace: true });
    },
  });

  const onConnect = useCallback(
    (c: Connection) =>
      setEdges((eds) =>
        addEdge({ ...c, type: "smoothstep", animated: true }, eds),
      ),
    [setEdges],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const raw = e.dataTransfer.getData(DRAG_MIME);
      if (!raw) return;
      const item: PaletteItem = JSON.parse(raw);
      const position = reactFlow.screenToFlowPosition({
        x: e.clientX,
        y: e.clientY,
      });
      const newNode: Node = {
        id: makeNodeId(item.type, nodes),
        type: item.type,
        position,
        data: {
          label: item.label,
          params: { ...item.defaultParams },
        },
      };
      setNodes((nds) => nds.concat(newNode));
    },
    [reactFlow, nodes, setNodes],
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedId(node.id);
  }, []);

  const onPaneClick = useCallback(() => setSelectedId(null), []);

  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selectedId) ?? null,
    [nodes, selectedId],
  );

  const updateSelectedParams = useCallback(
    (patch: Record<string, unknown>) => {
      if (!selectedId) return;
      setNodes((nds) =>
        nds.map((n) =>
          n.id === selectedId
            ? {
                ...n,
                data: {
                  ...(n.data as Record<string, unknown>),
                  params: {
                    ...((n.data as { params?: Record<string, unknown> }).params ?? {}),
                    ...patch,
                  },
                },
              }
            : n,
        ),
      );
    },
    [selectedId, setNodes],
  );

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    setNodes((nds) => nds.filter((n) => n.id !== selectedId));
    setEdges((eds) =>
      eds.filter((e) => e.source !== selectedId && e.target !== selectedId),
    );
    setSelectedId(null);
  }, [selectedId, setNodes, setEdges]);

  const triggerNode = useMemo(
    () => nodes.find((n) => n.type === "trigger.manual"),
    [nodes],
  );

  return (
    <div className="h-full flex flex-col">
      <TopBar
        name={name}
        onNameChange={setName}
        onBack={() => navigate("/workflows")}
        onSave={() => save.mutate()}
        onRun={() => setRunDialog(true)}
        saving={save.isPending}
        canRun={!isNew && !!triggerNode}
        saveError={save.error instanceof Error ? save.error.message : null}
      />
      <div className="flex flex-1 min-h-0">
        <PaletteSidebar />
        <div className="flex-1 min-w-0 relative" ref={wrapperRef}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onDrop={onDrop}
            onDragOver={onDragOver}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            onInit={(_: ReactFlowInstance) => {}}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        <PropertiesPanel
          node={selectedNode}
          onChange={updateSelectedParams}
          onDelete={deleteSelected}
        />
      </div>

      {runDialog && triggerNode && (
        <RunDialog
          workflowId={id!}
          inputSchema={
            ((triggerNode.data as { params?: Record<string, unknown> }).params
              ?.input_schema as Record<string, unknown>) ?? {}
          }
          onClose={() => setRunDialog(false)}
          onStarted={(runId) => navigate(`/runs/${runId}`)}
        />
      )}
    </div>
  );
}

// ─── Top bar ─────────────────────────────────────────────────────────────

function TopBar({
  name,
  onNameChange,
  onBack,
  onSave,
  onRun,
  saving,
  canRun,
  saveError,
}: {
  name: string;
  onNameChange: (s: string) => void;
  onBack: () => void;
  onSave: () => void;
  onRun: () => void;
  saving: boolean;
  canRun: boolean;
  saveError: string | null;
}) {
  return (
    <div className="flex items-center gap-3 border-b px-4 h-14 shrink-0">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft className="size-4" /> Workflows
      </Button>
      <input
        value={name}
        onChange={(e) => onNameChange(e.target.value)}
        className="flex-1 max-w-sm bg-transparent border-0 text-base font-semibold focus:outline-none focus:ring-0"
      />
      {saveError && (
        <span className="text-xs text-destructive truncate max-w-xs">{saveError}</span>
      )}
      <Button variant="outline" onClick={onSave} disabled={saving}>
        <Save className="size-4" /> {saving ? "Saving…" : "Save"}
      </Button>
      <Button onClick={onRun} disabled={!canRun}>
        <Play className="size-4" /> Run
      </Button>
    </div>
  );
}

// ─── Palette ─────────────────────────────────────────────────────────────

function PaletteSidebar() {
  const onDragStart = (e: React.DragEvent, item: PaletteItem) => {
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(item));
    e.dataTransfer.effectAllowed = "move";
  };
  return (
    <aside className="w-64 shrink-0 border-r overflow-y-auto px-3 py-4">
      <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-3 px-1">
        Nodes
      </div>
      <div className="flex flex-col gap-2">
        {PALETTE.map((item) => (
          <div
            key={item.type}
            draggable
            onDragStart={(e) => onDragStart(e, item)}
            className="rounded-md border bg-background px-3 py-2.5 cursor-grab active:cursor-grabbing hover:border-foreground/30 hover:shadow-sm transition-all"
          >
            <div className="flex items-center gap-2 mb-0.5">
              <div className="grid place-items-center size-6 rounded bg-secondary text-foreground shrink-0">
                {item.icon}
              </div>
              <div className="font-medium text-sm">{item.label}</div>
            </div>
            <div className="text-xs text-muted-foreground pl-8">
              {item.description}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-6 text-xs text-muted-foreground px-1">
        Drag a node onto the canvas to add it. Connect handles to wire steps together.
      </div>
    </aside>
  );
}

// ─── Properties panel ────────────────────────────────────────────────────

function PropertiesPanel({
  node,
  onChange,
  onDelete,
}: {
  node: Node | null;
  onChange: (patch: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  if (!node) {
    return (
      <aside className="w-72 shrink-0 border-l px-4 py-4 text-sm text-muted-foreground">
        Select a node to edit its parameters.
      </aside>
    );
  }
  const data = (node.data ?? {}) as { label?: string; params?: Record<string, unknown> };
  const params = data.params ?? {};
  const fields = PARAM_FIELDS[node.type as keyof typeof PARAM_FIELDS] ?? [];

  return (
    <aside className="w-72 shrink-0 border-l overflow-y-auto">
      <div className="px-4 py-4 border-b">
        <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-1">
          {String(node.type ?? "node")}
        </div>
        <div className="font-semibold">{data.label || node.id}</div>
        <div className="text-xs text-muted-foreground mt-0.5">id: {node.id}</div>
      </div>
      <div className="px-4 py-4 flex flex-col gap-3">
        {fields.length === 0 ? (
          <div className="text-sm text-muted-foreground">No parameters.</div>
        ) : (
          fields.map((f) => (
            <Field
              key={f.key}
              label={f.label}
              placeholder={f.placeholder}
              multiline={f.multiline}
              value={String(params[f.key] ?? "")}
              onChange={(v) => onChange({ [f.key]: v })}
            />
          ))
        )}
      </div>
      <div className="px-4 pb-4">
        <Button
          variant="ghost"
          size="sm"
          className="w-full text-destructive"
          onClick={onDelete}
        >
          <Trash2 className="size-4" /> Delete node
        </Button>
      </div>
    </aside>
  );
}

interface ParamField {
  key: string;
  label: string;
  placeholder?: string;
  multiline?: boolean;
}

const PARAM_FIELDS: Record<string, ParamField[]> = {
  "trigger.manual": [
    { key: "input_label", label: "Input label", placeholder: "What's the task?" },
  ],
  dispatch: [
    { key: "recipient_id", label: "Recipient (email)", placeholder: "alice@example.com" },
    { key: "task", label: "Task", placeholder: "Use {ctx.foo} for inputs", multiline: true },
  ],
  notify: [
    { key: "title", label: "Title", placeholder: "Dispatch finished" },
    { key: "message", label: "Message", placeholder: "{ctx.previous}", multiline: true },
  ],
  wait_reply: [
    { key: "from_recipient_id", label: "From recipient", placeholder: "alice@example.com" },
  ],
};

function Field({
  label,
  value,
  onChange,
  placeholder,
  multiline,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  multiline?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">{label}</span>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          rows={3}
          className="rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
        />
      ) : (
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
        />
      )}
    </label>
  );
}

// ─── Run dialog ──────────────────────────────────────────────────────────

function RunDialog({
  workflowId,
  inputSchema,
  onClose,
  onStarted,
}: {
  workflowId: string;
  inputSchema: Record<string, unknown>;
  onClose: () => void;
  onStarted: (runId: string) => void;
}) {
  const keys = useMemo(() => Object.keys(inputSchema), [inputSchema]);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(keys.map((k) => [k, ""])),
  );

  const start = useMutation({
    mutationFn: () => workflows.run(workflowId, values),
    onSuccess: (res) => onStarted(res.run_id),
  });

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Run workflow</DialogTitle>
          <DialogDescription>
            Provide inputs for this run. Reference them as {"{ctx.<key>}"} inside dispatch tasks.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          {keys.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              No inputs declared on the trigger. Starting will run the workflow as-is.
            </div>
          ) : (
            keys.map((k) => (
              <Field
                key={k}
                label={k}
                value={values[k] ?? ""}
                onChange={(v) => setValues((cur) => ({ ...cur, [k]: v }))}
              />
            ))
          )}
          {start.error instanceof Error && (
            <div className="text-sm text-destructive">{start.error.message}</div>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={start.isPending}>
            Cancel
          </Button>
          <Button onClick={() => start.mutate()} disabled={start.isPending}>
            <Play className="size-4" />
            {start.isPending ? "Starting…" : "Start run"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────

function defToReactFlow(wf: Workflow): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = (wf.definition.nodes ?? []).map((n) => ({
    id: n.id,
    type: n.type,
    position: { x: n.pos?.[0] ?? 0, y: n.pos?.[1] ?? 0 },
    data: { label: defaultLabel(n.type), params: n.params ?? {} },
  }));
  const edges: Edge[] = (wf.definition.edges ?? []).map((e, i) => ({
    id: `e-${i}-${e.from}-${e.to}`,
    source: e.from,
    target: e.to,
    type: "smoothstep",
    animated: true,
  }));
  return { nodes, edges };
}

function reactFlowToDef(nodes: Node[], edges: Edge[]): WorkflowDefinition {
  const defNodes: WorkflowNode[] = nodes.map((n) => ({
    id: n.id,
    type: String(n.type ?? "dispatch"),
    pos: [n.position.x, n.position.y],
    params:
      ((n.data as { params?: Record<string, unknown> })?.params as Record<string, unknown>) ??
      {},
  }));
  const defEdges: WorkflowEdge[] = edges.map((e) => ({
    from: e.source,
    from_port: "out",
    to: e.target,
    to_port: "in",
  }));
  return { nodes: defNodes, edges: defEdges };
}

function defaultLabel(type: string): string {
  const item = PALETTE.find((p) => p.type === type);
  return item?.label ?? type;
}

function makeNodeId(type: string, existing: Node[]): string {
  const base = type.replace(/\./g, "_");
  let i = 1;
  while (existing.some((n) => n.id === `${base}_${i}`)) i++;
  return `${base}_${i}`;
}
