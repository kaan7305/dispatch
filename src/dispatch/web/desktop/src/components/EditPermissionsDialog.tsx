import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Scopes, type TrustEdge } from "@/lib/api";
import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props {
  edge: TrustEdge;
  children: React.ReactNode;
}

const ALL_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash"] as const;

export function EditPermissionsDialog({ edge, children }: Props) {
  const [open, setOpen] = useState(false);
  const [tools, setTools] = useState<string[]>(edge.scopes.tools ?? []);
  const [approval, setApproval] = useState<"manual" | "auto">(
    edge.scopes.approval ?? "manual",
  );
  const [error, setError] = useState<string | null>(null);

  const qc = useQueryClient();

  // Reset state whenever the dialog opens against fresh scopes.
  useEffect(() => {
    if (!open) return;
    setTools(edge.scopes.tools ?? []);
    setApproval(edge.scopes.approval ?? "manual");
    setError(null);
  }, [open, edge.scopes.tools, edge.scopes.approval]);

  const save = useMutation({
    mutationFn: () => {
      const next: Scopes = {
        ...edge.scopes,
        tools,
        approval,
      };
      return api.updateTrust(edge.trust_link_id, next);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trust"] });
      setOpen(false);
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  function toggleTool(t: string) {
    setTools((prev) =>
      prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t],
    );
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit permissions</DialogTitle>
          <DialogDescription>
            What can <strong>{edge.peer}</strong>'s dispatches do on your machine?
            You're the recipient on this edge, so you set the rules.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
              Tools their dispatches may use
            </div>
            <div className="grid grid-cols-3 gap-2">
              {ALL_TOOLS.map((t) => (
                <label
                  key={t}
                  className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm cursor-pointer hover:bg-muted/40"
                >
                  <input
                    type="checkbox"
                    checked={tools.includes(t)}
                    onChange={() => toggleTool(t)}
                    className="size-4 accent-foreground"
                  />
                  {t}
                </label>
              ))}
            </div>
          </div>

          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
              Approval
            </div>
            <div className="space-y-1.5">
              <label className="flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/40">
                <input
                  type="radio"
                  name="approval"
                  checked={approval === "manual"}
                  onChange={() => setApproval("manual")}
                  className="mt-0.5 size-4 accent-foreground"
                />
                <div>
                  <div className="text-sm font-medium">Manual</div>
                  <div className="text-xs text-muted-foreground">
                    Approve every tool call individually.
                  </div>
                </div>
              </label>
              <label className="flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/40">
                <input
                  type="radio"
                  name="approval"
                  checked={approval === "auto"}
                  onChange={() => setApproval("auto")}
                  className="mt-0.5 size-4 accent-foreground"
                />
                <div>
                  <div className="text-sm font-medium">Auto</div>
                  <div className="text-xs text-muted-foreground">
                    Run within scope without per-tool prompts. Top-level Accept/Reject
                    still applies.
                  </div>
                </div>
              </label>
            </div>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="ghost">Cancel</Button>
          </DialogClose>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
