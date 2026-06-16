import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Plus, BookText, FileText } from "lucide-react";

import { contexts, type ContextSummary } from "@/lib/contextApi";
import { relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";

export default function Contexts() {
  const navigate = useNavigate();
  const list = useQuery({
    queryKey: ["contexts"],
    queryFn: () => contexts.list(),
  });

  const items = list.data?.contexts ?? [];

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-start justify-between mb-1">
        <div>
          <h1 className="text-2xl font-semibold">Context</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Reusable system prompts + files you can attach to any workflow
          </p>
        </div>
        <Button onClick={() => navigate("/contexts/new")}>
          <Plus className="size-4" /> New context
        </Button>
      </div>

      <div className="mt-6">
        {list.isLoading ? (
          <div className="rounded-lg border p-10 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : items.length === 0 ? (
          <EmptyState onCreate={() => navigate("/contexts/new")} />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {items.map((c) => (
              <ContextCard
                key={c.context_id}
                context={c}
                onClick={() => navigate(`/contexts/${c.context_id}/edit`)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ContextCard({
  context,
  onClick,
}: {
  context: ContextSummary;
  onClick: () => void;
}) {
  const subline: string[] = [];
  if (context.has_system_prompt) subline.push("system prompt");
  if (context.file_count > 0) {
    subline.push(`${context.file_count} file${context.file_count === 1 ? "" : "s"}`);
  }
  if (subline.length === 0) subline.push("empty");

  return (
    <button
      type="button"
      onClick={onClick}
      className="text-left rounded-lg border bg-background px-5 py-4 hover:border-foreground/20 hover:shadow-sm transition-all focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <div className="flex items-center gap-3 mb-2">
        <div className="grid place-items-center size-9 rounded-md bg-gradient-to-br from-emerald-50 to-teal-50 text-emerald-700 shrink-0">
          <BookText className="size-4" />
        </div>
        <div className="font-semibold truncate">{context.name}</div>
      </div>
      {context.description && (
        <div className="text-xs text-foreground/70 line-clamp-2 mb-2">
          {context.description}
        </div>
      )}
      <div className="text-xs text-muted-foreground flex items-center gap-2">
        <span>{subline.join(" · ")}</span>
        <span>·</span>
        <span>Edited {relativeTime(context.updated_at)}</span>
      </div>
    </button>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="rounded-lg border p-10 text-center">
      <FileText className="size-8 mx-auto mb-3 text-muted-foreground/50" />
      <div className="text-sm text-muted-foreground mb-4 max-w-md mx-auto">
        No contexts yet. A context bundles a system prompt and reference files
        you can ship with any workflow so the recipient's agent starts informed.
      </div>
      <Button onClick={onCreate}>
        <Plus className="size-4" /> New context
      </Button>
    </div>
  );
}
