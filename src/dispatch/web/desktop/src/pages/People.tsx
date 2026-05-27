import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Settings, Trash2, UserPlus } from "lucide-react";

import { api, type TrustEdge } from "@/lib/api";
import { initials } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export default function People() {
  const qc = useQueryClient();
  const trust = useQuery({ queryKey: ["trust"], queryFn: () => api.trust() });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeTrust(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["trust"] }),
  });

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold">Trusted People</h1>
        <Button>
          <UserPlus className="size-4" /> Invite person
        </Button>
      </div>

      <div className="rounded-lg border">
        {trust.data?.trust.length ? (
          trust.data.trust.map((t) => (
            <PersonRow
              key={t.trust_link_id}
              edge={t}
              onRevoke={() => revoke.mutate(t.trust_link_id)}
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

function PersonRow({ edge, onRevoke }: { edge: TrustEdge; onRevoke: () => void }) {
  return (
    <div className="flex items-center gap-4 px-5 py-4 border-b last:border-b-0">
      <div className="relative">
        <div className="grid place-items-center size-10 rounded-full bg-muted text-sm font-semibold">
          {initials(edge.peer)}
        </div>
        <span
          className={
            "absolute -bottom-0.5 -right-0.5 size-2.5 rounded-full ring-2 ring-background " +
            (edge.peer_online ? "bg-green-500" : "bg-amber-500")
          }
        />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{nameFromEmail(edge.peer)}</div>
        <div className="text-sm text-muted-foreground truncate">{edge.peer}</div>
      </div>
      <div className="flex items-center gap-2">
        {edge.direction === "outgoing" && (
          <Badge variant="outline">You can dispatch to them</Badge>
        )}
        {edge.direction === "incoming" && (
          <Badge variant="outline">They can dispatch to you</Badge>
        )}
      </div>
      <Button variant="ghost" size="sm">
        <Settings className="size-4" /> Edit permissions
      </Button>
      <Button variant="ghost" size="sm" className="text-destructive" onClick={onRevoke}>
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
