import { useEffect, useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ArrowLeft } from "lucide-react";

import {
  workflows,
  type Workflow,
  type WorkflowRun,
  type WorkflowRunStatus,
  type NodeState,
} from "@/lib/workflowApi";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { NODE_TYPES } from "@/components/workflow/nodes";

const TERMINAL: WorkflowRunStatus[] = ["completed", "failed", "cancelled"];

export default function WorkflowRunPage() {
  return (
    <ReactFlowProvider>
      <WorkflowRunInner />
    </ReactFlowProvider>
  );
}

function WorkflowRunInner() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => workflows.getRun(runId!),
    enabled: !!runId,
    refetchInterval: (q) => {
      const data = q.state.data as WorkflowRun | undefined;
      if (!data) return 1000;
      return TERMINAL.includes(data.status) ? false : 1000;
    },
  });

  const workflowId = run.data?.workflow_id;
  const wf = useQuery({
    queryKey: ["workflow", workflowId],
    queryFn: () => workflows.get(workflowId!),
    enabled: !!workflowId,
  });

  return (
    <div className="h-full flex flex-col">
      <TopBar
        name={wf.data?.name ?? "Loading…"}
        status={run.data?.status}
        error={run.data?.error}
        onBack={() => navigate("/workflows")}
      />
      <div className="flex-1 min-h-0">
        {wf.data && run.data ? (
          <Canvas workflow={wf.data} run={run.data} />
        ) : (
          <div className="h-full grid place-items-center text-sm text-muted-foreground">
            {run.isLoading || wf.isLoading
              ? "Loading run…"
              : run.error instanceof Error
              ? run.error.message
              : "Run not found."}
          </div>
        )}
      </div>
    </div>
  );
}

function TopBar({
  name,
  status,
  error,
  onBack,
}: {
  name: string;
  status?: WorkflowRunStatus;
  error?: string | null;
  onBack: () => void;
}) {
  return (
    <div className="flex items-center gap-3 border-b px-4 h-14 shrink-0">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft className="size-4" /> Workflows
      </Button>
      <div className="font-semibold flex-1 truncate">{name}</div>
      {error && (
        <span className="text-xs text-destructive truncate max-w-xs">{error}</span>
      )}
      {status && <StatusPill status={status} />}
    </div>
  );
}

function StatusPill({ status }: { status: WorkflowRunStatus }) {
  if (status === "running") return <Badge variant="running">Running</Badge>;
  if (status === "completed") return <Badge variant="success">Completed</Badge>;
  if (status === "failed") return <Badge variant="destructive">Failed</Badge>;
  if (status === "cancelled") return <Badge variant="muted">Cancelled</Badge>;
  return <Badge variant="muted">Pending</Badge>;
}

function Canvas({ workflow, run }: { workflow: Workflow; run: WorkflowRun }) {
  // Render the saved workflow definition; node colors come from run.node_states.
  const baseNodes: Node[] = useMemo(
    () =>
      (workflow.definition.nodes ?? []).map((n) => ({
        id: n.id,
        type: n.type,
        position: { x: n.pos?.[0] ?? 0, y: n.pos?.[1] ?? 0 },
        data: { params: n.params ?? {} },
        draggable: false,
        selectable: false,
      })),
    [workflow],
  );

  const baseEdges: Edge[] = useMemo(
    () =>
      (workflow.definition.edges ?? []).map((e, i) => ({
        id: `e-${i}-${e.from}-${e.to}`,
        source: e.from,
        target: e.to,
        type: "smoothstep",
        animated: nodeIsRunning(run.node_states, e.from, e.to),
      })),
    [workflow, run.node_states],
  );

  const [nodes, setNodes] = useNodesState<Node>(baseNodes);
  const [edges, setEdges] = useEdgesState<Edge>(baseEdges);

  // Re-merge live status into node data whenever the run snapshot changes.
  useEffect(() => {
    setNodes((curr) => {
      const byId = new Map(curr.map((n) => [n.id, n]));
      const merged = baseNodes.map((bn) => {
        const prev = byId.get(bn.id);
        const state: NodeState | undefined = run.node_states?.[bn.id];
        return {
          ...bn,
          position: prev?.position ?? bn.position,
          data: {
            ...(bn.data as Record<string, unknown>),
            status: state?.status ?? "pending",
            output: state?.output,
            error: state?.error,
          },
        };
      });
      return merged;
    });
    setEdges(baseEdges);
  }, [baseNodes, baseEdges, run.node_states, setNodes, setEdges]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodesChange={() => {}}
      onEdgesChange={() => {}}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      fitView
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable />
    </ReactFlow>
  );
}

function nodeIsRunning(
  states: Record<string, NodeState>,
  from: string,
  to: string,
): boolean {
  const f = states?.[from]?.status;
  const t = states?.[to]?.status;
  return f === "running" || t === "running";
}
