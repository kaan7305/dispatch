import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";

import { ApiError, api, type ComposeFanOutResult } from "@/lib/api";

import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props { children: React.ReactNode; }

export function ComposeDialog({ children }: Props) {
  const [open, setOpen]       = useState(false);
  const [recipients, setRecipients] = useState<string[]>([]);
  const [draft, setDraft]     = useState("");
  const [task, setTask]       = useState("");
  const [error, setError]     = useState<string | null>(null);
  const [partial, setPartial] = useState<ComposeFanOutResult["failures"]>([]);

  const qc = useQueryClient();
  const trust = useQuery({
    queryKey: ["trust"],
    queryFn: () => api.trust(),
    enabled: open,
  });

  const outgoing = useMemo(
    () => trust.data?.trust.filter((t) => t.direction === "outgoing") ?? [],
    [trust.data],
  );
  const availablePeers = useMemo(
    () => outgoing.map((t) => t.peer).filter((p) => !recipients.includes(p)),
    [outgoing, recipients],
  );

  const compose = useMutation({
    mutationFn: () =>
      api.compose({
        recipient_ids: recipients,
        task: task.trim(),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["sent"] });
      qc.invalidateQueries({ queryKey: ["history", "sent"] });
      // The broker returns either the fan-out shape (dispatches + failures)
      // or the legacy single-dispatch shape. We always use the array path
      // because we send recipient_ids, so res is ComposeFanOutResult.
      const fan = res as ComposeFanOutResult;
      if (fan.failures && fan.failures.length > 0) {
        setPartial(fan.failures);
        if (fan.dispatches.length === 0) return; // total failure — keep dialog open
      }
      setOpen(false);
      reset();
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  function reset() {
    setRecipients([]);
    setDraft("");
    setTask("");
    setError(null);
    setPartial([]);
  }

  function addRecipient(value: string) {
    const v = value.trim().toLowerCase();
    if (!v || recipients.includes(v)) {
      setDraft("");
      return;
    }
    setRecipients((rs) => [...rs, v]);
    setDraft("");
  }

  function removeRecipient(value: string) {
    setRecipients((rs) => rs.filter((r) => r !== value));
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      if (draft) addRecipient(draft);
    } else if (e.key === "Backspace" && !draft && recipients.length > 0) {
      setRecipients((rs) => rs.slice(0, -1));
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New dispatch</DialogTitle>
          <DialogDescription>
            Send the same task to one or more trusted teammates. The agent
            runs on each recipient's machine with their per-tool approval.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            const pending = draft.trim();
            const finalRecipients = pending && !recipients.includes(pending)
              ? [...recipients, pending]
              : recipients;
            if (pending) { addRecipient(pending); }
            if (finalRecipients.length === 0 || !task.trim()) return;
            setRecipients(finalRecipients);
            setError(null);
            setPartial([]);
            compose.mutate();
          }}
          className="space-y-4"
        >
          <div className="space-y-1.5">
            <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Recipients
            </label>
            <div className="flex flex-wrap items-center gap-1.5 rounded-md border bg-background px-2 py-1.5 focus-within:ring-1 focus-within:ring-ring min-h-[42px]">
              {recipients.map((r) => (
                <span
                  key={r}
                  className="inline-flex items-center gap-1 rounded-full bg-secondary px-2.5 py-1 text-xs font-medium"
                >
                  {r}
                  <button
                    type="button"
                    onClick={() => removeRecipient(r)}
                    className="text-muted-foreground hover:text-foreground"
                    aria-label={`Remove ${r}`}
                  >
                    <X className="size-3" />
                  </button>
                </span>
              ))}
              <input
                type="email"
                list="recipients"
                autoComplete="off"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={handleKeyDown}
                onBlur={() => { if (draft.trim()) addRecipient(draft); }}
                placeholder={recipients.length === 0 ? "teammate@example.com" : "add another…"}
                className="flex-1 min-w-[140px] bg-transparent px-1 py-0.5 text-sm focus:outline-none"
              />
              <datalist id="recipients">
                {availablePeers.map((p) => <option key={p} value={p} />)}
              </datalist>
            </div>
            <p className="text-xs text-muted-foreground">
              Press <kbd className="rounded bg-muted px-1">Enter</kbd> or
              <kbd className="rounded bg-muted px-1 ml-1">,</kbd> to add. Only
              people who accepted your invitation will receive it.
            </p>
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

          {partial.length > 0 && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm space-y-1">
              <div className="font-medium text-amber-900">
                Couldn't deliver to {partial.length} recipient{partial.length === 1 ? "" : "s"}:
              </div>
              <ul className="space-y-0.5">
                {partial.map((f) => (
                  <li key={f.recipient_id} className="text-xs text-amber-900/90">
                    <strong>{f.recipient_id}</strong>: {f.error}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">Cancel</Button>
            </DialogClose>
            <Button
              type="submit"
              disabled={compose.isPending || (recipients.length === 0 && !draft.trim()) || !task.trim()}
            >
              {compose.isPending
                ? "Sending…"
                : recipients.length > 1
                ? `Send to ${recipients.length}`
                : "Send dispatch"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
