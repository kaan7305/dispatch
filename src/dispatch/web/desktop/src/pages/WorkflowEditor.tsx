import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
  type Connection,
  type ReactFlowInstance,
  type DefaultEdgeOptions,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Save, Play, ArrowLeft } from "lucide-react";

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
import { PropertiesPanel as RichPropertiesPanel } from "@/components/workflow/PropertiesPanel";
import { cn } from "@/lib/utils";

const DRAG_MIME = "application/x-dispatch-node";

// n8n-style soft bezier curves in a calm neutral so nodes pop, not edges.
const DEFAULT_EDGE_OPTIONS: DefaultEdgeOptions = {
  type: "default",
  animated: false,
  style: { stroke: "#a1a1aa", strokeWidth: 1.75 },
};

const CONNECTION_LINE_STYLE: React.CSSProperties = {
  stroke: "#6366f1",
  strokeWidth: 2,
};

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
            defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
            connectionLineStyle={CONNECTION_LINE_STYLE}
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={18}
              size={1.5}
              color="#d4d4d8"
            />
            <Controls
              position="bottom-left"
              showInteractive={false}
              className="!rounded-lg !border !border-zinc-200 !bg-white !shadow-sm"
            />
          </ReactFlow>
          {!isNew && <RecentRunsPanel workflowId={id!} />}
        </div>
        <RichPropertiesPanel
          node={
            selectedNode
              ? {
                  id: selectedNode.id,
                  type: String(selectedNode.type ?? ""),
                  pos: [selectedNode.position.x, selectedNode.position.y],
                  params:
                    ((selectedNode.data as { params?: Record<string, unknown> })
                      .params) ?? {},
                }
              : null
          }
          onChange={(updated) =>
            updateSelectedParams(updated.params as Record<string, unknown>)
          }
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
    <div className="flex items-center gap-3 border-b border-zinc-200 bg-white px-4 h-14 shrink-0">
      <Button variant="ghost" size="sm" onClick={onBack} className="text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Workflows
      </Button>
      <div className="h-5 w-px bg-zinc-200" />
      <input
        value={name}
        onChange={(e) => onNameChange(e.target.value)}
        placeholder="Untitled workflow"
        className="flex-1 max-w-sm bg-transparent border-0 text-[15px] font-semibold focus:outline-none focus:ring-0 placeholder:text-muted-foreground/60"
      />
      <span className="text-xs text-muted-foreground hidden sm:inline">
        {saving ? "Saving…" : saveError ? "" : "Saved"}
      </span>
      {saveError && (
        <span className="text-xs text-destructive truncate max-w-xs">{saveError}</span>
      )}
      <div className="ml-auto flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={onSave} disabled={saving}>
          <Save className="size-3.5" /> Save
        </Button>
        <Button size="sm" onClick={onRun} disabled={!canRun}>
          <Play className="size-3.5" /> Run
        </Button>
      </div>
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
    <aside className="w-64 shrink-0 border-r border-zinc-200 overflow-y-auto bg-zinc-50/40">
      <div className="sticky top-0 z-10 border-b border-zinc-200 bg-zinc-50/80 backdrop-blur px-4 py-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          Nodes
        </div>
        <div className="text-[11px] text-muted-foreground/80 mt-0.5">
          Drag onto the canvas
        </div>
      </div>
      <div className="flex flex-col gap-1.5 px-3 py-3">
        {PALETTE.map((item) => (
          <div
            key={item.type}
            draggable
            onDragStart={(e) => onDragStart(e, item)}
            className="group flex items-center gap-3 rounded-lg border border-transparent bg-white shadow-sm px-3 py-2.5 cursor-grab active:cursor-grabbing hover:border-zinc-300 hover:shadow-md transition-all"
          >
            <div
              className={cn(
                "grid place-items-center size-9 rounded-lg border shrink-0",
                item.accent,
              )}
            >
              {item.icon}
            </div>
            <div className="min-w-0 flex-1">
              <div className="font-medium text-[13px] text-foreground truncate">
                {item.label}
              </div>
              <div className="text-[11px] text-muted-foreground truncate">
                {item.description}
              </div>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

// ─── Recent runs overlay ─────────────────────────────────────────────────

function RecentRunsPanel({ workflowId }: { workflowId: string }) {
  const navigate = useNavigate();
  const runsQ = useQuery({
    queryKey: ["workflow-runs", workflowId],
    queryFn: () => workflows.listRuns(workflowId),
    refetchInterval: 4000,
  });
  const runs = (runsQ.data?.runs ?? []).slice(0, 6);
  if (runs.length === 0) return null;

  return (
    <div className="absolute bottom-4 right-4 w-72 rounded-lg border border-zinc-200 bg-white shadow-lg overflow-hidden">
      <div className="px-3 py-2 border-b border-zinc-200 bg-zinc-50/60">
        <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          Recent runs
        </div>
      </div>
      <ul className="divide-y divide-zinc-100 max-h-72 overflow-y-auto">
        {runs.map((r) => (
          <li key={r.run_id}>
            <button
              type="button"
              onClick={() => navigate(`/runs/${r.run_id}`)}
              className="w-full text-left px-3 py-2 hover:bg-zinc-50 flex items-center gap-2"
            >
              <RunStatusDot status={r.status} />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium truncate">
                  {new Date(r.started_at).toLocaleString()}
                </div>
                <div className="text-[10px] text-muted-foreground capitalize">
                  {r.status}
                </div>
              </div>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RunStatusDot({ status }: { status: string }) {
  const color =
    status === "completed" ? "bg-emerald-500"
    : status === "running" ? "bg-indigo-500 animate-pulse"
    : status === "failed"  ? "bg-red-500"
    : status === "cancelled" ? "bg-zinc-400"
    : "bg-zinc-300";
  return <span className={`size-2 rounded-full ${color}`} />;
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
              <label key={k} className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-foreground">{k}</span>
                <input
                  value={values[k] ?? ""}
                  onChange={(e) =>
                    setValues((cur) => ({ ...cur, [k]: e.target.value }))
                  }
                  className="rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
                />
              </label>
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
