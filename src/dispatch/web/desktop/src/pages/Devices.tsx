import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Plus, Server, Smartphone, Trash2 } from "lucide-react";

import { api, type Device } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { relativeTime } from "@/lib/format";

export default function Devices() {
  const qc = useQueryClient();
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => api.devices(),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeDevice(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["devices"] }),
  });

  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-semibold">Devices</h1>
        <Button>
          <Plus className="size-4" /> Enroll new device
        </Button>
      </div>
      <p className="text-sm text-muted-foreground mb-6">
        Machines authorized to receive dispatches
      </p>

      <div className="space-y-3">
        {devices.data?.devices.length ? (
          devices.data.devices.map((d) => (
            <DeviceCard
              key={d.device_id}
              device={d}
              onRevoke={() => revoke.mutate(d.device_id)}
            />
          ))
        ) : (
          <div className="rounded-lg border px-6 py-10 text-sm text-muted-foreground">
            {devices.isLoading ? "Loading…" : "No devices enrolled yet."}
          </div>
        )}
      </div>
    </div>
  );
}

function DeviceCard({ device, onRevoke }: { device: Device; onRevoke: () => void }) {
  const Icon = pickIcon(device.label);
  const online = device.status === "active" && isRecent(device.last_seen);
  return (
    <div className="rounded-lg border flex items-center gap-4 px-5 py-4">
      <div className="grid place-items-center size-12 rounded-lg bg-muted shrink-0">
        <Icon className="size-5" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{device.label}</span>
          <Badge variant={online ? "success" : "muted"}>
            {online ? "online" : "offline"}
          </Badge>
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          Last seen {device.last_seen ? relativeTime(device.last_seen) : "never"}
        </div>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="text-destructive"
        onClick={onRevoke}
      >
        <Trash2 className="size-4" /> Revoke
      </Button>
    </div>
  );
}

function pickIcon(label: string) {
  const l = label.toLowerCase();
  if (l.includes("iphone") || l.includes("android") || l.includes("phone")) return Smartphone;
  if (l.includes("server") || l.includes("vm") || l.includes("ubuntu") || l.includes("linux")) return Server;
  return Monitor;
}

function isRecent(iso: string | null): boolean {
  if (!iso) return false;
  return Date.now() - new Date(iso).getTime() < 5 * 60_000;
}
