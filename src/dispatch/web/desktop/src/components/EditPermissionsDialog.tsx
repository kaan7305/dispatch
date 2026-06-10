import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";

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

const ALL_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Write", "Edit", "Bash"] as const;

/** The grant pattern for one tool on a server: `mcp__<server>__<tool>`. */
function toolGrant(server: string, tool: string): string {
  return `mcp__${server}__${tool}`;
}

/** Split an edge's `mcp` list into whole-server grants (bare names or
 *  `mcp__server__*`) and explicit per-tool grants (`mcp__server__tool`). The
 *  "*" wildcard is handled separately by the Allow-all toggle. */
function parseMcpGrants(mcp: string[]): {
  whole: Set<string>;
  tools: Record<string, Set<string>>;
} {
  const whole = new Set<string>();
  const tools: Record<string, Set<string>> = {};
  for (const g of mcp) {
    if (g === "*") continue;
    if (!g.includes("__")) {
      whole.add(g);
      continue;
    }
    if (!g.startsWith("mcp__")) continue;
    const parts = g.split("__");
    const server = parts[1];
    const tool = parts.slice(2).join("__");
    if (!server) continue;
    if (tool === "" || tool === "*") whole.add(server);
    else (tools[server] ??= new Set()).add(tool);
  }
  return { whole, tools };
}

export function EditPermissionsDialog({ edge, children }: Props) {
  const [open, setOpen] = useState(false);
  const [tools, setTools] = useState<string[]>(edge.scopes.tools ?? []);
  const [approval, setApproval] = useState<"manual" | "auto">(
    edge.scopes.approval ?? "manual",
  );
  // MCP grants split into: a wildcard flag ("*"), whole-server grants (bare
  // names), and explicit per-tool grants (`mcp__server__tool`).
  const grantedMcp = useMemo(() => edge.scopes.mcp ?? [], [edge.scopes.mcp]);
  const parsed = useMemo(() => parseMcpGrants(grantedMcp), [grantedMcp]);
  const [allowAllMcp, setAllowAllMcp] = useState(grantedMcp.includes("*"));
  // Whole-server grants (parent checkbox = "every tool, including future ones").
  const [servers, setServers] = useState<string[]>(() => Array.from(parsed.whole));
  // Per-server granted tool names (leaf checkboxes). server → raw tool names.
  const [toolGrants, setToolGrants] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(
      Object.entries(parsed.tools).map(([s, set]) => [s, Array.from(set)]),
    ),
  );
  // Tools the recipient previously said "always allow" for (grown JIT from live
  // approvals). Shown here so they can be revoked — removing one means that tool
  // prompts again on the next dispatch.
  const [autoTools, setAutoTools] = useState<string[]>(edge.scopes.auto_tools ?? []);
  const [resultVisibility, setResultVisibility] = useState<"full" | "redacted">(
    edge.scopes.result_visibility ?? "redacted",
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
    for (const g of parsed.whole) names.add(g);
    for (const s of Object.keys(parsed.tools)) names.add(s);
    return Array.from(names).sort();
  }, [installed.data, parsed]);

  // Reset state whenever the dialog opens against fresh scopes.
  useEffect(() => {
    if (!open) return;
    setTools(edge.scopes.tools ?? []);
    setApproval(edge.scopes.approval ?? "manual");
    setAllowAllMcp(grantedMcp.includes("*"));
    setServers(Array.from(parsed.whole));
    setToolGrants(
      Object.fromEntries(
        Object.entries(parsed.tools).map(([s, set]) => [s, Array.from(set)]),
      ),
    );
    setAutoTools(edge.scopes.auto_tools ?? []);
    setResultVisibility(edge.scopes.result_visibility ?? "redacted");
    setError(null);
  }, [open, edge.scopes.tools, edge.scopes.approval, edge.scopes.auto_tools, edge.scopes.result_visibility, grantedMcp, parsed]);

  const save = useMutation({
    mutationFn: () => {
      // Whole-server grants stay bare; per-tool grants become `mcp__server__tool`
      // patterns. A server granted wholesale ignores its per-tool selections
      // (the bare name already covers every tool, present and future).
      const mcp = allowAllMcp
        ? ["*"]
        : Array.from(
            new Set([
              ...servers,
              ...Object.entries(toolGrants)
                .filter(([s, ts]) => !servers.includes(s) && ts.length > 0)
                .flatMap(([s, ts]) => ts.map((t) => toolGrant(s, t))),
            ]),
          );
      const next: Scopes = {
        ...edge.scopes,
        tools,
        mcp,
        approval,
        auto_tools: autoTools,
        result_visibility: resultVisibility,
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

  function toggleToolGrant(server: string, tool: string) {
    setToolGrants((prev) => {
      const cur = prev[server] ?? [];
      const next = cur.includes(tool)
        ? cur.filter((t) => t !== tool)
        : [...cur, tool];
      return { ...prev, [server]: next };
    });
  }

  // Can't browse-and-add (no daemon to enumerate) but there may still be named
  // grants to view/remove and the Allow-all toggle to flip.
  const cannotEnumerate =
    !installed.isLoading && (installed.data ?? []).length === 0;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit permissions</DialogTitle>
          <DialogDescription>
            What can <strong>{edge.peer}</strong>'s dispatches do on your machine?
            You're the recipient, so you set the rules.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
              Tools their dispatches may use
            </div>
            <div className="grid grid-cols-4 gap-2">
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
              <span className="font-medium">Allow all</span>
            </label>
            {!allowAllMcp && (
              <>
                {knownServers.length > 0 ? (
                  <div className="space-y-2">
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
                        <McpServerRow
                          key={name}
                          name={name}
                          uninstalled={uninstalled}
                          wholeGranted={servers.includes(name)}
                          grantedTools={toolGrants[name] ?? []}
                          onToggleWhole={() => toggleServer(name)}
                          onToggleTool={(tool) => toggleToolGrant(name, tool)}
                        />
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

          {autoTools.length > 0 && (
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
                Always-allowed tools
              </div>
              <div className="text-xs text-muted-foreground mb-2">
                These skip the per-call prompt on a manual edge. Remove one to be
                asked again next time.
              </div>
              <div className="flex flex-wrap gap-2">
                {autoTools.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setAutoTools((prev) => prev.filter((x) => x !== t))}
                    className="group flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-mono hover:bg-destructive/10 hover:border-destructive/40"
                    title="Remove this always-allow"
                  >
                    <span className="truncate max-w-[18rem]">{t}</span>
                    <span className="text-muted-foreground group-hover:text-destructive">×</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
              Approval
            </div>
            <div className="grid grid-cols-2 gap-2">
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
                    Approve every tool call.
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
                    Runs in scope without prompts. Accept/Reject still applies.
                  </div>
                </div>
              </label>
            </div>
          </div>

          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
              Tool results the sender sees
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/40">
                <input
                  type="radio"
                  name="result_visibility"
                  checked={resultVisibility === "redacted"}
                  onChange={() => setResultVisibility("redacted")}
                  className="mt-0.5 size-4 accent-foreground"
                />
                <div>
                  <div className="text-sm font-medium">Redacted</div>
                  <div className="text-xs text-muted-foreground">
                    Sender sees each call and its status — never the contents.
                    They still get the final reply.
                  </div>
                </div>
              </label>
              <label className="flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/40">
                <input
                  type="radio"
                  name="result_visibility"
                  checked={resultVisibility === "full"}
                  onChange={() => setResultVisibility("full")}
                  className="mt-0.5 size-4 accent-foreground"
                />
                <div>
                  <div className="text-sm font-medium">Full</div>
                  <div className="text-xs text-muted-foreground">
                    Stream complete tool results to the sender's watch view.
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

interface McpServerRowProps {
  name: string;
  uninstalled: boolean;
  /** Whole server granted — every tool, including ones added later. */
  wholeGranted: boolean;
  /** Raw tool names individually granted on this server. */
  grantedTools: string[];
  onToggleWhole: () => void;
  onToggleTool: (tool: string) => void;
}

/** One MCP server in the grant list: a whole-server checkbox plus an expander
 *  that lazily enumerates the server's tools for per-tool checkboxes. Granting
 *  the whole server implicitly covers every tool, so when it's on the per-tool
 *  boxes show checked+disabled. Enumeration is best-effort — a server that needs
 *  auth or is offline degrades to just the whole-server checkbox. */
function McpServerRow({
  name, uninstalled, wholeGranted, grantedTools, onToggleWhole, onToggleTool,
}: McpServerRowProps) {
  const [expanded, setExpanded] = useState(false);

  // Lazy: only handshake the server once its row is actually opened.
  const toolsQuery = useQuery({
    queryKey: ["mcp-tools", name],
    queryFn: () => api.mcpServerTools(name),
    enabled: expanded,
    staleTime: 60_000,
    retry: false,
  });

  const Chevron = expanded ? ChevronDown : ChevronRight;
  const selectedCount = grantedTools.length;

  return (
    <div className="rounded-md border">
      <div className="flex items-center gap-2 px-3 py-2 text-sm">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="grid place-items-center size-5 rounded hover:bg-muted/60 shrink-0"
          aria-label={expanded ? "Collapse" : "Expand"}
          aria-expanded={expanded}
        >
          <Chevron className="size-4 text-muted-foreground" />
        </button>
        <label className="flex items-center gap-2 cursor-pointer flex-1 min-w-0">
          <input
            type="checkbox"
            checked={wholeGranted}
            onChange={onToggleWhole}
            className="size-4 accent-foreground"
          />
          <span className="truncate font-medium">{name}</span>
        </label>
        {!wholeGranted && selectedCount > 0 && (
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground shrink-0">
            {selectedCount} tool{selectedCount === 1 ? "" : "s"}
          </span>
        )}
        {uninstalled && (
          <span className="text-[10px] uppercase tracking-wide text-amber-600 shrink-0">
            not installed
          </span>
        )}
      </div>

      {expanded && (
        <div className="border-t px-3 py-2">
          {wholeGranted && (
            <div className="text-xs text-muted-foreground mb-2">
              Whole server granted — every tool below is allowed, including ones
              added later. Uncheck the server to pick individual tools.
            </div>
          )}
          {toolsQuery.isLoading ? (
            <div className="text-xs text-muted-foreground py-1">Loading tools…</div>
          ) : toolsQuery.data && !toolsQuery.data.ok ? (
            <div className="text-xs text-muted-foreground py-1">
              Couldn't list tools{toolsQuery.data.reason ? ` — ${toolsQuery.data.reason}` : ""}.
              You can still grant the whole server above.
            </div>
          ) : toolsQuery.isError ? (
            <div className="text-xs text-muted-foreground py-1">
              Couldn't list tools. You can still grant the whole server above.
            </div>
          ) : (toolsQuery.data?.tools.length ?? 0) === 0 ? (
            <div className="text-xs text-muted-foreground py-1">
              This server reports no tools.
            </div>
          ) : (
            <div className="space-y-1">
              {toolsQuery.data!.tools.map((t) => {
                const checked = wholeGranted || grantedTools.includes(t.name);
                return (
                  <label
                    key={t.name}
                    className="flex items-start gap-2 rounded px-1.5 py-1 text-sm hover:bg-muted/40 cursor-pointer"
                    title={t.description || undefined}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={wholeGranted}
                      onChange={() => onToggleTool(t.name)}
                      className="mt-0.5 size-4 accent-foreground disabled:opacity-50"
                    />
                    <span className="min-w-0">
                      <span className="font-mono text-xs">{t.name}</span>
                      {t.description && (
                        <span className="block text-xs text-muted-foreground line-clamp-1">
                          {t.description}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
