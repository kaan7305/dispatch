import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api";
import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props { children: React.ReactNode; }

export function InviteDialog({ children }: Props) {
  const [open, setOpen]       = useState(false);
  const [email, setEmail]     = useState("");
  const [error, setError]     = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const qc = useQueryClient();
  const invite = useMutation({
    mutationFn: () => api.invite(email.trim()),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["trust"] });
      qc.invalidateQueries({ queryKey: ["invitations"] });
      setError(null);
      if (result.delivered) {
        setSuccess(`Invite emailed to ${result.to_email}.`);
      } else if (result.dev_link) {
        setSuccess(
          `Dev mode — share this link with ${result.to_email}: ${result.dev_link}`,
        );
      } else {
        setSuccess(`Invitation created for ${result.to_email}.`);
      }
      setEmail("");
    },
    onError: (err) => {
      setSuccess(null);
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  function reset() {
    setEmail("");
    setError(null);
    setSuccess(null);
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) reset(); }}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Invite a person</DialogTitle>
          <DialogDescription>
            Sends them an invitation. After they accept they can send dispatches
            to you and (optionally, scoped) you can send to them.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!email.trim()) return;
            invite.mutate();
          }}
          className="space-y-3"
        >
          <div className="space-y-1.5">
            <label htmlFor="invite-email" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Email
            </label>
            <input
              id="invite-email"
              type="email"
              required
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="teammate@example.com"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>

          {success && (
            <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm break-all">
              {success}
            </div>
          )}
          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">Close</Button>
            </DialogClose>
            <Button type="submit" disabled={invite.isPending}>
              {invite.isPending ? "Sending…" : "Send invite"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
