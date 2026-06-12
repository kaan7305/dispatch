import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, X } from "lucide-react";

import { api, ApiError, type TrustEdge } from "@/lib/api";
import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props {
  /** An INCOMING edge - the memory for an outgoing edge lives on the peer's
   *  machine, so this dialog is only offered where you're the recipient. */
  edge: TrustEdge;
  children: React.ReactNode;
}

/** What this machine has learned across dispatches on this edge's capability
 *  bucket: project directories previous runs located, injected into the next
 *  run so its agent skips the cold-start filesystem search. Advisory only -
 *  entries grant nothing; every tool call still passes the approval gate. */
export function EdgeMemoryDialog({ edge, children }: Props) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const memory = useQuery({
    queryKey: ["edge-memory", edge.trust_link_id],
    queryFn: () => api.edgeMemory(edge.trust_link_id),
    enabled: open,
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["edge-memory", edge.trust_link_id] });
  const onError = (err: unknown) =>
    setError(err instanceof ApiError ? err.message : String(err));

  const forgetOne = useMutation({
    mutationFn: (path: string) =>
      api.forgetEdgeMemoryEntry(edge.trust_link_id, path),
    onSuccess: invalidate,
    onError,
  });
  const forgetAll = useMutation({
    mutationFn: () => api.forgetEdgeMemory(edge.trust_link_id),
    onSuccess: invalidate,
    onError,
  });

  const entries = memory.data?.entries ?? [];
  const sharedWith = memory.data?.shared_with ?? [];

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); setError(null); }}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Learned context</DialogTitle>
          <DialogDescription>
            Project directories remembered from <strong>{edge.peer}</strong>'s
            previous dispatches, injected into the next run so the agent starts
            in the right place instead of searching your disk. Advisory only -
            remembering a path grants nothing; every tool call is still gated
            by this edge's scope and approvals.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {memory.data?.unavailable ? (
            <div className="text-sm text-muted-foreground px-1 py-4 text-center">
              Open the local Dispatch app to view learned context - it lives on
              your machine, not the broker.
            </div>
          ) : memory.isLoading ? (
            <div className="text-sm text-muted-foreground px-1 py-4 text-center">
              Loading…
            </div>
          ) : entries.length === 0 ? (
            <div className="text-sm text-muted-foreground px-1 py-4 text-center">
              Nothing learned yet. This fills in after dispatches on this edge
              successfully touch a project directory.
            </div>
          ) : (
            <div className="rounded-md border divide-y">
              {entries.map((e) => (
                <div key={e.path} className="flex items-center gap-3 px-3 py-2">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-mono break-all">{e.path}</div>
                    <div className="text-xs text-muted-foreground">
                      last used {e.last_seen ? e.last_seen.slice(0, 10) : "?"}
                      {e.hits ? ` · ${e.hits} run${e.hits === 1 ? "" : "s"}` : ""}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => forgetOne.mutate(e.path)}
                    disabled={forgetOne.isPending}
                    title="Forget this directory"
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}

          {sharedWith.length > 0 && (
            <div className="text-xs text-muted-foreground">
              Shared memory: {sharedWith.join(", ")}{" "}
              {sharedWith.length === 1 ? "has" : "have"} the identical
              permission set, so dispatches from them read and grow this same
              list. Forgetting here forgets it for them too.
            </div>
          )}

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          {entries.length > 0 && (
            <Button
              variant="ghost"
              className="text-destructive mr-auto"
              onClick={() => forgetAll.mutate()}
              disabled={forgetAll.isPending}
            >
              <Trash2 className="size-4" /> Forget all
            </Button>
          )}
          <DialogClose asChild>
            <Button type="button" variant="ghost">Close</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
