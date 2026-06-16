import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Plus, Workflow as WorkflowIcon } from "@/lib/icons";

import { workflows, type WorkflowSummary } from "@/lib/workflowApi";
import { relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";

export default function Workflows() {
  const navigate = useNavigate();
  const list = useQuery({
    queryKey: ["workflows"],
    queryFn: () => workflows.list(),
  });

  const items = list.data?.workflows ?? [];

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-start justify-between mb-1">
        <div>
          <h1 className="text-2xl font-semibold">Workflows</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Visual chains of dispatches
          </p>
        </div>
        <Button onClick={() => navigate("/workflows/new")}>
          <Plus className="size-4" /> New workflow
        </Button>
      </div>

      <div className="mt-6">
        {list.isLoading ? (
          <div className="rounded-lg border p-10 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : items.length === 0 ? (
          <EmptyState onCreate={() => navigate("/workflows/new")} />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {items.map((w) => (
              <WorkflowCard
                key={w.workflow_id}
                workflow={w}
                onClick={() => navigate(`/workflows/${w.workflow_id}/edit`)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function WorkflowCard({
  workflow,
  onClick,
}: {
  workflow: WorkflowSummary;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-left rounded-lg border bg-background px-5 py-4 hover:border-foreground/20 hover:shadow-sm transition-all focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <div className="flex items-center gap-3 mb-2">
        <div className="grid place-items-center size-9 rounded-md bg-secondary text-foreground shrink-0">
          <WorkflowIcon className="size-4" />
        </div>
        <div className="font-semibold truncate">{workflow.name}</div>
      </div>
      <div className="text-xs text-muted-foreground flex items-center gap-2">
        <span>{workflow.node_count} {workflow.node_count === 1 ? "node" : "nodes"}</span>
        <span>·</span>
        <span>Edited {relativeTime(workflow.updated_at)}</span>
      </div>
    </button>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="rounded-lg border p-10 text-center">
      <WorkflowIcon className="size-8 mx-auto mb-3 text-muted-foreground/50" />
      <div className="text-sm text-muted-foreground mb-4">
        No workflows yet. Chain dispatches together to automate multi-step tasks.
      </div>
      <Button onClick={onCreate}>
        <Plus className="size-4" /> New workflow
      </Button>
    </div>
  );
}
