import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Inbox as InboxIcon,
  Users,
  Bookmark,
  History as HistoryIcon,
  Monitor,
  Search,
  Plus,
} from "lucide-react";

import { api } from "@/lib/api";
import { openEventStream, type EventMessage } from "@/lib/ws";
import { cn } from "@/lib/utils";
import { initials } from "@/lib/format";
import { Button } from "./ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { ComposeDialog } from "./ComposeDialog";

const NAV = [
  { to: "/inbox",   label: "Inbox",   Icon: InboxIcon  },
  { to: "/people",  label: "People",  Icon: Users      },
  { to: "/saved",   label: "Saved",   Icon: Bookmark   },
  { to: "/history", label: "History", Icon: HistoryIcon },
  { to: "/devices", label: "Devices", Icon: Monitor    },
];

export function Shell() {
  const qc = useQueryClient();
  const [online, setOnline] = useState(false);

  const { data: session } = useQuery({
    queryKey: ["session"],
    queryFn: () => api.session(),
  });

  // Single global event stream — always open while the shell is mounted.
  // Invalidates every affected query so any page stays current without polling.
  useEffect(() => {
    const close = openEventStream(
      (msg: EventMessage) => {
        if (msg.type === "snapshot") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          return;
        }
        if (msg.type === "inbox_new") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          qc.invalidateQueries({ queryKey: ["history", "received"] });
          return;
        }
        if (msg.type === "dispatch_status" || msg.type === "dispatch_event") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          qc.invalidateQueries({ queryKey: ["sent"] });
          qc.invalidateQueries({ queryKey: ["history", "sent"] });
          qc.invalidateQueries({ queryKey: ["history", "received"] });
          qc.invalidateQueries({ queryKey: ["dispatch", msg.dispatch_id] });
        }
      },
      (connected) => setOnline(connected),
    );
    return () => close();
  }, [qc]);

  return (
    <div className="flex h-full flex-col">
      <Topbar email={session?.user_id} online={online} />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 overflow-y-auto bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Topbar({ email, online }: { email?: string; online: boolean }) {
  return (
    <header className="flex items-center gap-4 border-b px-6 h-14">
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-xl font-semibold tracking-tight">Dispatch</span>
        <span className="rounded-md border px-2 py-0.5 text-xs text-muted-foreground">
          Free
        </span>
      </div>
      <div className="flex-1 max-w-xl">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="search"
            placeholder="Search dispatches and people"
            className="w-full rounded-lg border bg-secondary/40 pl-9 pr-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground shrink-0">
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", online ? "bg-green-500" : "bg-amber-500 animate-pulse")} />
          {online ? "Online" : "Connecting…"}
        </span>
        <AccountMenu email={email} />
      </div>
    </header>
  );
}

function AccountMenu({ email }: { email?: string }) {
  // Sign-out lives only on the broker page (the Railway sign-in landing).
  // The desktop UI just shows the signed-in identity for context.
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="grid place-items-center size-8 rounded-full bg-muted text-xs font-semibold text-foreground hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          {email ? initials(email) : "—"}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{email || "Not signed in"}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => { api.openBroker().catch(() => {}); }}>
          Manage at broker →
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Sidebar() {
  return (
    <aside className="w-60 shrink-0 border-r px-3 py-4 flex flex-col gap-1">
      <ComposeDialog>
        <Button className="w-full justify-center gap-2 mb-3" size="lg">
          <Plus className="size-4" /> Compose
        </Button>
      </ComposeDialog>
      {NAV.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
              isActive
                ? "bg-secondary font-medium text-foreground"
                : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
            )
          }
        >
          <Icon className="size-4" />
          {label}
        </NavLink>
      ))}
      <div className="mt-auto pt-4 text-xs text-muted-foreground border-t">
        <div>Free Plan</div>
        <div>5 / 10 dispatches</div>
      </div>
    </aside>
  );
}

