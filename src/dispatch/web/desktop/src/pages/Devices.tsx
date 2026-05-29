import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Monitor, Pencil, Plus, Server, Smartphone, Trash2, X } from "lucide-react";

import { api, type Device } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EnrollDeviceDialog } from "@/components/EnrollDeviceDialog";
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
        <EnrollDeviceDialog>
          <Button>
            <Plus className="size-4" /> Enroll new device
          </Button>
        </EnrollDeviceDialog>
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
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(device.label);

  const rename = useMutation({
    mutationFn: (label: string) => api.renameDevice(device.device_id, label),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["devices"] });
      setEditing(false);
    },
    onError: () => {
      setDraft(device.label);
      setEditing(false);
    },
  });

  function startEdit() {
    setDraft(device.label);
    setEditing(true);
  }

  function commit() {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === device.label) {
      setEditing(false);
      return;
    }
    rename.mutate(trimmed);
  }

  const Icon = pickIcon(device.label);
  const online = device.online && device.status === "active";

  return (
    <div className="rounded-lg border flex items-center gap-4 px-5 py-4">
      <div className="grid place-items-center size-12 rounded-lg bg-muted shrink-0">
        <Icon className="size-5" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          {editing ? (
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commit();
                if (e.key === "Escape") { setEditing(false); setDraft(device.label); }
              }}
              className="font-semibold bg-transparent border-b border-ring focus:outline-none text-sm w-full max-w-xs"
              disabled={rename.isPending}
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
            />
          ) : (
            <span className="font-semibold">{device.label}</span>
          )}
          <Badge variant={online ? "success" : "muted"}>
            {online ? "online" : "offline"}
          </Badge>
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {online
            ? "Connected now"
            : device.last_seen
            ? `Last seen ${relativeTime(device.last_seen)}`
            : "Never connected"}
        </div>
      </div>

      {editing ? (
        <>
          <Button size="sm" onClick={commit} disabled={rename.isPending}>
            <Check className="size-4" /> {rename.isPending ? "Saving…" : "Save"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => { setEditing(false); setDraft(device.label); }}
            disabled={rename.isPending}
          >
            <X className="size-4" /> Cancel
          </Button>
        </>
      ) : (
        <>
          <Button
            variant="ghost"
            size="sm"
            onClick={startEdit}
            disabled={device.status !== "active"}
          >
            <Pencil className="size-4" /> Rename
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            onClick={onRevoke}
          >
            <Trash2 className="size-4" /> Revoke
          </Button>
        </>
      )}
    </div>
  );
}

function pickIcon(label: string) {
  const l = label.toLowerCase();
  if (l.includes("iphone") || l.includes("android") || l.includes("phone")) return Smartphone;
  if (l.includes("server") || l.includes("vm") || l.includes("ubuntu") || l.includes("linux")) return Server;
  return Monitor;
}
