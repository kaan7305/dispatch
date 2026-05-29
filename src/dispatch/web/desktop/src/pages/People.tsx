import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Settings, Trash2, UserPlus, X } from "lucide-react";

import { api, type Invitation, type TrustEdge } from "@/lib/api";
import { initials } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { InviteDialog } from "@/components/InviteDialog";
import { EditPermissionsDialog } from "@/components/EditPermissionsDialog";
import { SegmentedTabs } from "@/components/SegmentedTabs";

type Tab = "all" | "send" | "receive";

interface PersonRowData {
  peer: string;
  outgoing?: TrustEdge;
  incoming?: TrustEdge;
}

export default function People() {
  const [tab, setTab] = useState<Tab>("all");
  const qc = useQueryClient();

  const trust = useQuery({ queryKey: ["trust"], queryFn: () => api.trust() });
  const invitations = useQuery({
    queryKey: ["invitations"],
    queryFn: () => api.invitations(),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeTrust(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["trust"] }),
  });

  const acceptInvite = useMutation({
    mutationFn: (token: string) => api.acceptInvite(token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trust"] });
      qc.invalidateQueries({ queryKey: ["invitations"] });
    },
  });

  const declineInvite = useMutation({
    mutationFn: (token: string) => api.declineInvite(token),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invitations"] }),
  });

  // Collapse edges into one row per peer.
  const allRows = useMemo<PersonRowData[]>(() => {
    const by: Map<string, PersonRowData> = new Map();
    for (const edge of trust.data?.trust ?? []) {
      const r = by.get(edge.peer) ?? { peer: edge.peer };
      if (edge.direction === "outgoing") r.outgoing = edge;
      else r.incoming = edge;
      by.set(edge.peer, r);
    }
    return [...by.values()];
  }, [trust.data]);

  const rows = useMemo(() => {
    if (tab === "send")    return allRows.filter((r) => !!r.outgoing);
    if (tab === "receive") return allRows.filter((r) => !!r.incoming);
    return allRows;
  }, [tab, allRows]);

  const pendingReceived = (invitations.data?.received ?? []).filter(
    (inv) => inv.status === "pending",
  );

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-2xl font-semibold">Trusted People</h1>
        <InviteDialog>
          <Button>
            <UserPlus className="size-4" /> Invite person
          </Button>
        </InviteDialog>
      </div>

      <div className="mb-4">
        <SegmentedTabs
          options={[
            { value: "all",     label: "All" },
            { value: "send",    label: "Can send" },
            { value: "receive", label: "Can receive" },
          ]}
          value={tab}
          onChange={setTab}
          variant="underline"
        />
      </div>

      {pendingReceived.length > 0 && (
        <div className="mb-5">
          <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-2">
            Pending invitations
          </p>
          <div className="rounded-lg border divide-y">
            {pendingReceived.map((inv) => (
              <InvitationRow
                key={inv.invitation_id}
                inv={inv}
                onAccept={() => acceptInvite.mutate(inv.token)}
                onDecline={() => declineInvite.mutate(inv.token)}
                loading={acceptInvite.isPending || declineInvite.isPending}
              />
            ))}
          </div>
        </div>
      )}

      <div className="rounded-lg border">
        {rows.length ? (
          rows.map((r) => (
            <PersonRow
              key={r.peer}
              row={r}
              tab={tab}
              onRevoke={(id) => revoke.mutate(id)}
            />
          ))
        ) : (
          <div className="px-6 py-10 text-sm text-muted-foreground">
            {trust.isLoading
              ? "Loading…"
              : tab === "send"
              ? "No one you can send dispatches to yet."
              : tab === "receive"
              ? "No one can send dispatches to you yet."
              : "No contacts yet. Invite someone to get started."}
          </div>
        )}
      </div>
    </div>
  );
}

function InvitationRow({
  inv, onAccept, onDecline, loading,
}: {
  inv: Invitation;
  onAccept: () => void;
  onDecline: () => void;
  loading: boolean;
}) {
  return (
    <div className="flex items-center gap-4 px-5 py-4">
      <div className="grid place-items-center size-10 rounded-full bg-muted text-sm font-semibold shrink-0">
        {initials(inv.from_user)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{nameFromEmail(inv.from_user)}</div>
        <div className="text-sm text-muted-foreground truncate">
          {inv.from_user} wants to send you dispatches
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={onAccept} disabled={loading}>
          <Check className="size-3.5" /> Accept
        </Button>
        <Button size="sm" variant="ghost" onClick={onDecline} disabled={loading}>
          <X className="size-3.5" /> Decline
        </Button>
      </div>
    </div>
  );
}

function PersonRow({
  row, tab, onRevoke,
}: {
  row: PersonRowData;
  tab: Tab;
  onRevoke: (trustLinkId: string) => void;
}) {
  const online = row.outgoing?.peer_online ?? row.incoming?.peer_online ?? false;

  return (
    <div className="flex items-center gap-4 px-5 py-4 border-b last:border-b-0">
      <div className="relative shrink-0">
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
        {row.outgoing && (tab === "all" || tab === "send") && (
          <Badge variant="outline">You can dispatch to them</Badge>
        )}
        {row.incoming && (tab === "all" || tab === "receive") && (
          <Badge variant="outline">They can dispatch to you</Badge>
        )}
      </div>
      {row.incoming && (tab === "all" || tab === "receive") && (
        <EditPermissionsDialog edge={row.incoming}>
          <Button variant="ghost" size="sm">
            <Settings className="size-4" /> Edit permissions
          </Button>
        </EditPermissionsDialog>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="text-destructive"
        onClick={() => {
          if (tab === "send" && row.outgoing) {
            onRevoke(row.outgoing.trust_link_id);
          } else if (tab === "receive" && row.incoming) {
            onRevoke(row.incoming.trust_link_id);
          } else {
            if (row.outgoing) onRevoke(row.outgoing.trust_link_id);
            if (row.incoming) onRevoke(row.incoming.trust_link_id);
          }
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
