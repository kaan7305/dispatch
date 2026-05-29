import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Inbox as InboxIcon,
  Users,
  Bookmark,
  History as HistoryIcon,
  Monitor,
  Search,
  Plus,
  LogOut,
} from "lucide-react";

import { api } from "@/lib/api";
import { openEventStream, type EventMessage } from "@/lib/ws";
import { cn } from "@/lib/utils";
import { avatarStyle, initials } from "@/lib/format";
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
    <header className="flex items-center gap-4 border-b border-border/60 px-6 h-14 backdrop-blur bg-background/80">
      <div className="flex items-center gap-2.5 shrink-0">
        <span className="grid place-items-center size-6 rounded-md bg-gradient-to-br from-indigo-500 to-violet-600 text-white text-[10px] font-bold shadow-sm ring-1 ring-black/5">
          D
        </span>
        <span className="text-[17px] font-semibold tracking-tight">Dispatch</span>
        <span className="rounded-full bg-secondary/60 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground ring-1 ring-inset ring-border/60">
          Free
        </span>
      </div>
      <div className="flex-1 max-w-xl">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/70" />
          <input
            type="search"
            placeholder="Search dispatches and people"
            className="w-full rounded-lg border border-border/70 bg-secondary/30 pl-9 pr-3 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-ring/40 focus:bg-background transition-colors"
          />
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3 text-sm shrink-0">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-secondary/50 px-2.5 py-1 text-xs text-muted-foreground ring-1 ring-inset ring-border/60">
          <span className="relative inline-flex">
            <span className={cn("size-1.5 rounded-full", online ? "bg-emerald-500" : "bg-amber-500")} />
            {online && (
              <span className="absolute inset-0 size-1.5 rounded-full bg-emerald-500 animate-ping opacity-75" />
            )}
          </span>
          {online ? "Online" : "Connecting…"}
        </span>
        <AccountMenu email={email} />
      </div>
    </header>
  );
}

function AccountMenu({ email }: { email?: string }) {
  const qc = useQueryClient();
  const signOut = useMutation({
    mutationFn: () => api.signOut(),
    onSuccess: (result) => {
      // Drop the local token + clear cached state, then send the user
      // back to the broker landing for a fresh Clerk sign-in.
      sessionStorage.removeItem("dispatch_local_token");
      qc.clear();
      window.location.href = result.broker || "/";
    },
  });

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="grid place-items-center size-8 rounded-full text-xs font-semibold shadow-sm ring-1 ring-black/5 hover:scale-105 hover:shadow-md transition-all focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
          style={email ? avatarStyle(email) : undefined}
        >
          {email ? initials(email) : "—"}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{email || "Not signed in"}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-destructive focus:text-destructive"
          onSelect={() => signOut.mutate()}
        >
          <LogOut className="size-4" /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Sidebar() {
  return (
    <aside className="w-60 shrink-0 border-r border-border/60 px-3 py-4 flex flex-col gap-0.5 bg-secondary/20">
      <ComposeDialog>
        <Button
          className="w-full justify-center gap-2 mb-4 shadow-sm"
          size="lg"
        >
          <Plus className="size-4" /> Compose
        </Button>
      </ComposeDialog>
      {NAV.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all",
              isActive
                ? "bg-background font-medium text-foreground shadow-sm ring-1 ring-border/40"
                : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
            )
          }
        >
          {({ isActive }) => (
            <>
              <Icon
                className={cn(
                  "size-4 transition-colors",
                  isActive ? "text-foreground" : "text-muted-foreground/80 group-hover:text-foreground",
                )}
              />
              {label}
            </>
          )}
        </NavLink>
      ))}
      <div className="mt-auto rounded-lg border border-border/60 bg-background/60 px-3 py-3 text-xs">
        <div className="flex items-center justify-between">
          <span className="font-medium text-foreground">Free Plan</span>
          <span className="text-muted-foreground">5 / 10</span>
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-secondary">
          <div
            className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500"
            style={{ width: "50%" }}
          />
        </div>
        <div className="mt-1.5 text-muted-foreground">dispatches this month</div>
      </div>
    </aside>
  );
}

