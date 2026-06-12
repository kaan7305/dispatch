import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";

import { api } from "@/lib/api";
import { Button } from "./ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "./ui/dialog";

interface Props { children: React.ReactNode; }

export function EnrollDeviceDialog({ children }: Props) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const cmd = useQuery({
    queryKey: ["install-command"],
    queryFn: () => api.installCommand(),
    enabled: open,
  });

  async function copy() {
    if (!cmd.data) return;
    try {
      await navigator.clipboard.writeText(cmd.data.command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Enroll a new device</DialogTitle>
          <DialogDescription>
            On the new machine, paste this into a terminal. It installs the
            daemon, registers a fresh Ed25519 keypair, and starts the tray
            app under your account. Each device gets its own keypair - no
            shared secrets between machines.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {cmd.isLoading && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}
          {cmd.data && (
            <>
              <pre className="rounded-md border bg-muted/40 px-3 py-2 text-xs font-mono whitespace-pre-wrap break-all max-h-40 overflow-y-auto">
                {cmd.data.command}
              </pre>
              <Button onClick={copy} variant="outline" size="sm">
                <Copy className="size-4" /> {copied ? "Copied" : "Copy command"}
              </Button>
              <div className="text-xs text-muted-foreground pt-2 border-t">
                Anyone who runs this command becomes you on that machine -
                only paste it on devices you trust.
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="ghost">Done</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
