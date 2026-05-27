import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";

import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props { children: React.ReactNode; }

export function ComposeDialog({ children }: Props) {
  const [open, setOpen]         = useState(false);
  const [recipient, setRecipient] = useState("");
  const [task, setTask]         = useState("");
  const [error, setError]       = useState<string | null>(null);

  const qc = useQueryClient();
  const trust = useQuery({
    queryKey: ["trust"],
    queryFn: () => api.trust(),
    enabled: open,
  });

  const compose = useMutation({
    mutationFn: () => api.compose({ recipient_id: recipient.trim(), task: task.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sent"] });
      qc.invalidateQueries({ queryKey: ["history", "sent"] });
      setOpen(false);
      setRecipient("");
      setTask("");
      setError(null);
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  const outgoing = trust.data?.trust.filter((t) => t.direction === "outgoing") ?? [];

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New dispatch</DialogTitle>
          <DialogDescription>
            They have to have accepted your invitation. The agent runs on their
            machine, with their per-tool approval.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!recipient.trim() || !task.trim()) return;
            compose.mutate();
          }}
          className="space-y-4"
        >
          <div className="space-y-1.5">
            <label htmlFor="recipient" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Recipient email
            </label>
            <input
              id="recipient"
              type="email"
              list="recipients"
              required
              autoComplete="off"
              value={recipient}
              onChange={(e) => setRecipient(e.target.value)}
              placeholder="teammate@example.com"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <datalist id="recipients">
              {outgoing.map((t) => (
                <option key={t.trust_link_id} value={t.peer} />
              ))}
            </datalist>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="task" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Task
            </label>
            <textarea
              id="task"
              required
              rows={5}
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="What should their agent do?"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
            />
          </div>

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">Cancel</Button>
            </DialogClose>
            <Button type="submit" disabled={compose.isPending}>
              {compose.isPending ? "Sending…" : "Send dispatch"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
