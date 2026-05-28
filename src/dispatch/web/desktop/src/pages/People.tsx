import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Settings, Trash2, UserPlus } from "lucide-react";

import { api, type TrustEdge } from "@/lib/api";
import { initials } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { InviteDialog } from "@/components/InviteDialog";
import { EditPermissionsDialog } from "@/components/EditPermissionsDialog";

interface PersonRowData {
  peer: string;
  outgoing?: TrustEdge;  // edge me → them
  incoming?: TrustEdge;  // edge them → me
}

export default function People() {
  const qc = useQueryClient();
  const trust = useQuery({ queryKey: ["trust"], queryFn: () => api.trust() });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeTrust(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["trust"] }),
  });

  // Collapse outgoing + incoming edges into one row per peer.
  const rows = useMemo<PersonRowData[]>(() => {
    const by: Map<string, PersonRowData> = new Map();
    for (const edge of trust.data?.trust ?? []) {
      const r = by.get(edge.peer) ?? { peer: edge.peer };
      if (edge.direction === "outgoing") r.outgoing = edge;
      else r.incoming = edge;
      by.set(edge.peer, r);
    }
    return [...by.values()];
  }, [trust.data]);

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold">Trusted People</h1>
        <InviteDialog>
          <Button>
            <UserPlus className="size-4" /> Invite person
          </Button>
        </InviteDialog>
      </div>

      <div className="rounded-lg border">
        {rows.length ? (
          rows.map((r) => (
            <PersonRow
              key={r.peer}
              row={r}
              onRevoke={(id) => revoke.mutate(id)}
            />
          ))
        ) : (
          <div className="px-6 py-10 text-sm text-muted-foreground">
            {trust.isLoading ? "Loading…" : "No contacts yet. Invite someone to get started."}
          </div>
        )}
      </div>
    </div>
  );
}

function PersonRow({
  row, onRevoke,
}: {
  row: PersonRowData;
  onRevoke: (trustLinkId: string) => void;
}) {
  // Online presence: prefer whichever edge knows
  const online = row.outgoing?.peer_online ?? row.incoming?.peer_online ?? false;
  return (
    <div className="flex items-center gap-4 px-5 py-4 border-b last:border-b-0">
      <div className="relative">
        <div className="grid place-items-center size-10 rounded-full bg-muted text-sm font-semibold">
          {initials(row.peer)}
        </div>
        <span
          className={
            "absolute -bottom-0.5 -right-0.5 size-2.5 rounded-full ring-2 ring-background " +
            (online ? "bg-green-500" : "bg-amber-500")
          }
        />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{nameFromEmail(row.peer)}</div>
        <div className="text-sm text-muted-foreground truncate">{row.peer}</div>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        {row.outgoing && <Badge variant="outline">You can dispatch to them</Badge>}
        {row.incoming && <Badge variant="outline">They can dispatch to you</Badge>}
      </div>
      {row.incoming ? (
        <EditPermissionsDialog edge={row.incoming}>
          <Button variant="ghost" size="sm">
            <Settings className="size-4" /> Edit permissions
          </Button>
        </EditPermissionsDialog>
      ) : (
        <Button variant="ghost" size="sm" disabled title="Only the recipient sets scopes — that's them on this edge">
          <Settings className="size-4" /> Edit permissions
        </Button>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="text-destructive"
        onClick={() => {
          // Revoke ALL edges for this peer in one click.
          if (row.outgoing) onRevoke(row.outgoing.trust_link_id);
          if (row.incoming) onRevoke(row.incoming.trust_link_id);
        }}
      >
        <Trash2 className="size-4" /> Revoke
      </Button>
    </div>
  );
}

function nameFromEmail(email: string): string {
  const local = email.split("@")[0] ?? email;
  return local
    .split(/[._-]/)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}
