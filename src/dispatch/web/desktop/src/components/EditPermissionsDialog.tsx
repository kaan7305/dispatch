import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Scopes, type TrustEdge } from "@/lib/api";
import { isBroker } from "@/lib/config";
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
  // MCP grants split into a wildcard flag + an explicit set of server names.
  const grantedMcp = useMemo(() => edge.scopes.mcp ?? [], [edge.scopes.mcp]);
  const [allowAllMcp, setAllowAllMcp] = useState(grantedMcp.includes("*"));
  const [servers, setServers] = useState<string[]>(
    grantedMcp.filter((m) => m !== "*"),
  );
  const [error, setError] = useState<string | null>(null);

  const qc = useQueryClient();

  // Installed servers to browse. Daemon-local; empty in broker mode / offline.
  const installed = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: api.mcpServers,
    enabled: open,
    staleTime: 30_000,
  });

  // The checklist = installed servers UNION any already-granted server (so a
  // grant for a server you've since uninstalled is shown + preserved, never
  // silently dropped). Mirrors the in-session picker's union-preserve.
  const knownServers = useMemo(() => {
    const names = new Set<string>((installed.data ?? []).map((s) => s.name));
    for (const g of grantedMcp) if (g !== "*") names.add(g);
    return Array.from(names).sort();
  }, [installed.data, grantedMcp]);

  // Reset state whenever the dialog opens against fresh scopes.
  useEffect(() => {
    if (!open) return;
    setTools(edge.scopes.tools ?? []);
    setApproval(edge.scopes.approval ?? "manual");
    setAllowAllMcp(grantedMcp.includes("*"));
    setServers(grantedMcp.filter((m) => m !== "*"));
    setError(null);
  }, [open, edge.scopes.tools, edge.scopes.approval, grantedMcp]);

  const save = useMutation({
    mutationFn: () => {
      const mcp = allowAllMcp ? ["*"] : servers;
      const next: Scopes = {
        ...edge.scopes,
        tools,
        mcp,
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

  function toggleServer(name: string) {
    setServers((prev) =>
      prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name],
    );
  }

  // Can't browse-and-add (no daemon to enumerate) but there may still be named
  // grants to view/remove and the Allow-all toggle to flip.
  const cannotEnumerate =
    !installed.isLoading && (installed.data ?? []).length === 0;

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
              MCP servers their dispatches may use
            </div>
            <label className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm cursor-pointer hover:bg-muted/40 mb-2">
              <input
                type="checkbox"
                checked={allowAllMcp}
                onChange={() => setAllowAllMcp((v) => !v)}
                className="size-4 accent-foreground"
              />
              <span>
                <span className="font-medium">Allow all</span>
                <span className="text-muted-foreground">
                  {" "}— every installed MCP server, including ones you add later
                </span>
              </span>
            </label>
            {!allowAllMcp && (
              <>
                {knownServers.length > 0 ? (
                  <div className="grid grid-cols-2 gap-2">
                    {knownServers.map((name) => {
                      // Only flag "not installed" when we actually enumerated a
                      // non-empty list — otherwise an empty/failed fetch (broker
                      // mode, daemon still starting) would falsely label every
                      // granted server.
                      const enumerated =
                        installed.isSuccess && (installed.data ?? []).length > 0;
                      const uninstalled =
                        enumerated && !(installed.data ?? []).some((s) => s.name === name);
                      return (
                        <label
                          key={name}
                          className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm cursor-pointer hover:bg-muted/40"
                        >
                          <input
                            type="checkbox"
                            checked={servers.includes(name)}
                            onChange={() => toggleServer(name)}
                            className="size-4 accent-foreground"
                          />
                          <span className="truncate">{name}</span>
                          {uninstalled && (
                            <span className="text-[10px] uppercase tracking-wide text-amber-600">
                              not installed
                            </span>
                          )}
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <div className="text-sm text-muted-foreground rounded-md border border-dashed px-3 py-2">
                    {installed.isLoading
                      ? "Loading servers…"
                      : "No MCP servers granted."}
                  </div>
                )}
                {cannotEnumerate && (
                  <div className="text-xs text-muted-foreground mt-2">
                    {isBroker
                      ? "Open the local Dispatch app to browse and add installed MCP servers — they aren't visible from the web."
                      : "No installed MCP servers detected on this machine."}
                  </div>
                )}
              </>
            )}
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
